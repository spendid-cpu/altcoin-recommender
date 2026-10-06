"""코인 추천 카드의 공지·뉴스 (참고용 표시 전용 - 추천 규칙에는 안 쓴다).

- 업비트 공지: api-manager.upbit.com/api/v1/announcements. 입출금 중단, 거래 유의 종목 지정, 거래지원 종료, 리브랜딩, 신규 상장, 에어드랍 등이
  올라온다. 제목의 '(티커)'로 종목을 맞춘다(정확). 최근 KEEP_DAYS일치를 저장해 두고 SHOW_DAYS일 안의 것만 카드에 보여준다.
- 뉴스: 시가총액이 큰 코인(NEWS_MIN_CAP 이상)만 구글 뉴스 RSS에서 한국어 이름으로 검색하고, 제목에 이름이 든 기사만 남긴다.
  소형 알트는 무관한 기사가 섞여 나와서 뉴스를 붙이지 않는다.
공시·공지 제목은 외부 글이라 화면에서는 항상 글자로만 그린다."""

import asyncio
import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import aiohttp

from src import jsonutil, state_store

KST = ZoneInfo("Asia/Seoul")
NOTICE_URL = "https://api-manager.upbit.com/api/v1/announcements"
NEWS_URL = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; altcoin-recommender dashboard)", "Accept": "application/json, text/xml"}
META_KEY = "upbit_notices_json"
KEEP_DAYS = 14
SHOW_DAYS = 7
PER_PAGE = 30           # 업비트가 받아 주는 최대치 근처 (50은 400)
MAX_PAGES = 5
MAX_SHOW = 8
NEWS_MIN_CAP = 1e12     # 시가총액 1조 원 이상인 코인만 뉴스를 붙인다
NEWS_DAYS = 14
MAX_NEWS = 3
_STOP_TICKERS = {"KRW", "USDT", "UTC", "KST"}
# 구글 뉴스는 출처가 잡다해서(스팸성 사이트가 섞임) 이름이 알려진 국내 언론·코인 전문 매체만 남긴다
OUTLETS = ("연합뉴스", "뉴스1", "뉴시스", "이데일리", "머니투데이", "한국경제", "한경", "매일경제", "매경", "서울경제", "조선비즈", "조선일보", "중앙일보", "동아일보", "한겨레",
           "경향신문", "블록미디어", "토큰포스트", "코인데스크", "블로터", "이투데이", "아시아경제", "헤럴드경제", "파이낸셜뉴스", "전자신문", "디지털데일리", "더블록",
           "코인니스", "디센터", "지디넷코리아", "it조선", "아이뉴스24", "비즈니스워치", "머니s", "ytn", "sbs", "kbs", "mbc", "jtbc", "coindesk", "cointelegraph")


def classify(title: str, category: str = "") -> tuple[str, str]:
    """제목 키워드 -> (태그, 위험도 risk/watch/info)"""
    if "유의" in title and "해제" in title:
        return "유의 해제", "info"
    if "유의 종목" in title or "거래 유의" in title:
        return "유의 종목", "risk"
    if "거래지원 종료" in title or "상장폐지" in title:
        return "거래지원 종료", "risk"
    if re.search(r"(입출금|입금|출금)\s*(일시\s*)?(중단|정지)", title):
        return ("입출금 재개", "info") if ("(완료)" in title or "재개" in title) else ("입출금 중단", "watch")
    if any(w in title for w in ("리브랜딩", "명칭 변경", "심볼 변경")):
        return "명칭 변경", "watch"
    if any(w in title for w in ("네트워크 업그레이드", "하드포크", "스왑", "컨트랙트", "메인넷")):
        return "네트워크", "watch"
    if "마켓 디지털 자산 추가" in title:
        return "신규 상장", "info"
    if "에어드랍" in title:
        return "에어드랍", "info"
    return category or "공지", "info"


def tickers_of(title: str) -> set[str]:
    return {t for t in re.findall(r"\(([A-Z0-9]{1,12})\)", title or "") if re.search(r"[A-Z]", t) and t not in _STOP_TICKERS}


def load_notices() -> list[list]:
    raw = state_store.get_meta(META_KEY)
    return json.loads(raw) if raw else []


async def _page(session: aiohttp.ClientSession, page: int) -> list[dict]:
    async with session.get(NOTICE_URL, params={"os": "web", "page": page, "per_page": PER_PAGE, "category": "all"},
                           headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        j = await resp.json(content_type=None)
    return (j.get("data") or {}).get("notices") or []


async def refresh_notices(session: aiohttp.ClientSession) -> list[list]:
    """[[id, 최근 갱신 시각(ISO), 분류, 제목], ...] 최신 순. 새 공지만 받아 합친다(첫 실행은 KEEP_DAYS일치). 실패하면 저장본 그대로."""
    prev = load_notices()
    known = {n[0] for n in prev}
    cutoff = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).isoformat()
    got: dict[int, list] = {}
    try:
        for page in range(1, MAX_PAGES + 1):
            rows = await _page(session, page)
            if not rows:
                break
            for x in rows:
                got[x["id"]] = [x["id"], x.get("listed_at") or x.get("first_listed_at") or "", x.get("category") or "", html.unescape(x.get("title") or "")]
            oldest = min((x.get("listed_at") or x.get("first_listed_at") or "9") for x in rows)  # 목록은 최근 갱신 순이라 이 시각으로 멈춘다
            if (known and {x["id"] for x in rows} & known) or oldest < cutoff:
                break
    except Exception as exc:
        print(f"[업비트 공지] 조회 실패(저장본 유지): {exc!r}")
        if not got:
            return prev
    merged = {n[0]: n for n in prev}
    merged.update(got)
    out = sorted((n for n in merged.values() if n[1] >= cutoff), key=lambda n: n[1], reverse=True)
    if out != prev:
        state_store.set_meta(META_KEY, jsonutil.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return out


def notes_for(market: str, notices: list[list], now: datetime | None = None) -> list[list]:
    """종목의 최근 SHOW_DAYS일 공지 -> [[시각 'YYYY-MM-DD HH:MM', 태그, 위험도, 제목, 공지 id], ...] 최신 순"""
    now = now or datetime.now(KST)
    cutoff = (now - timedelta(days=SHOW_DAYS)).isoformat()
    sym = market[4:] if market.startswith("KRW-") else market
    out = []
    for nid, at, cat, title in notices:
        if at < cutoff or sym not in tickers_of(title):
            continue
        label, level = classify(title, cat)
        out.append([at[:16].replace("T", " "), label, level, title[:90], nid])
    return out[:MAX_SHOW]


def parse_news_rss(xml_text: str, korean_name: str, now: datetime | None = None) -> list[list]:
    """구글 뉴스 RSS -> [[시각 KST, 언론사, 제목, 링크], ...] 최신 순. 제목에 한국어 이름이 든 기사만."""
    now = now or datetime.now(timezone.utc)
    try:
        items = ET.fromstring(xml_text).findall(".//item")
    except ET.ParseError:
        return []
    key = re.sub(r"\s", "", korean_name or "")
    out, seen = [], set()
    for it in items:
        title = html.unescape(it.findtext("title") or "")
        outlet = it.findtext("source") or ""
        if outlet and title.endswith(f" - {outlet}"):
            title = title[: -len(outlet) - 3]
        link = (it.findtext("link") or "").strip()
        try:
            at = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if not key or key not in re.sub(r"\s", "", title) or title in seen or now - at > timedelta(days=NEWS_DAYS) or not link.startswith("https://"):
            continue
        if not any(o in outlet.lower() for o in OUTLETS):
            continue
        seen.add(title)
        out.append([at.astimezone(KST).strftime("%Y-%m-%d %H:%M"), outlet, title[:90], link])
    out.sort(key=lambda r: r[0], reverse=True)
    return out[:MAX_NEWS]


async def fetch_news(session: aiohttp.ClientSession, korean_name: str) -> list[list] | None:
    if not korean_name:
        return None
    url = NEWS_URL.format(q=urllib.parse.quote(f'"{korean_name}" 코인'))
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            return parse_news_rss(await resp.text(), korean_name)
    except Exception as exc:
        print(f"[코인 뉴스] {korean_name} 조회 실패(건너뜀): {exc!r}")
        return None


async def fetch_news_many(session: aiohttp.ClientSession, names: dict[str, str]) -> dict[str, list[list]]:
    """{market: 한국어 이름} -> {market: 기사들} (못 받은 종목은 빠진다). 구글에 부담이 가지 않게 천천히."""
    out = {}
    for market, name in names.items():
        got = await fetch_news(session, name)
        if got is not None:
            out[market] = got
        await asyncio.sleep(0.6)
    return out
