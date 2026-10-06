"""주식 후보 종목의 최근 공시와 뉴스 제목 (네이버 증권 모바일 API, 참고용 표시 전용 - 추천 규칙에는 안 쓴다).

- 공시: m.stock.naver.com/api/stock/{code}/disclosure (거래소 공시를 코스콤이 중계한 것). 최근 DISC_DAYS일, '주식선물 가격제한폭' 같은
  시장조치성 잡음은 빼고, 제목 키워드로 유형 태그와 위험도(risk 빨강 / watch 노랑 / info)를 붙인다.
- 뉴스: m.stock.naver.com/api/news/stock/{code}. 종목명이 제목에 있는 기사만 남긴다(다른 종목·시황 기사가 섞여 나오기 때문).
비공식 API라 형식이 바뀌거나 막힐 수 있다. 못 받으면 그 종목은 이전 값을 그대로 둔다."""

import asyncio
import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

KST = ZoneInfo("Asia/Seoul")
DISC_URL = "https://m.stock.naver.com/api/stock/{code}/disclosure?page=1&pageSize=40"
NEWS_URL = "https://m.stock.naver.com/api/news/stock/{code}?page=1&pageSize=15"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; altcoin-recommender dashboard)", "Accept": "application/json"}
DISC_DAYS = 60
MAX_DISC = 8
MAX_NEWS = 3
NOISE = ("주식선물", "가격제한폭 확대요건")  # 거래소 시장조치성 공시 (종목 사정과 무관)

# (제목에 들어 있는 말, 태그, 위험도) - 위에서부터 먼저 맞는 것
RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("상장폐지",), "상장폐지", "risk"),
    (("거래정지",), "거래정지", "risk"),
    (("관리종목",), "관리종목", "risk"),
    (("투자경고", "투자위험"), "투자경고", "risk"),
    (("공매도 과열",), "공매도 과열", "risk"),
    (("횡령", "배임"), "횡령·배임", "risk"),
    (("감사의견",), "감사의견", "risk"),
    (("회생", "부도"), "회생·부도", "risk"),
    (("불성실공시",), "불성실공시", "risk"),
    (("소송",), "소송", "risk"),
    (("투자주의",), "투자주의", "watch"),
    (("유상증자",), "유상증자", "watch"),
    (("무상증자",), "무상증자", "watch"),
    (("감자",), "감자", "watch"),
    (("전환사채", "신주인수권부사채", "교환사채"), "메자닌 발행", "watch"),
    (("최대주주",), "최대주주", "watch"),
    (("대표이사",), "대표이사", "watch"),
    (("임상",), "임상", "watch"),
    (("투자판단 관련 주요경영사항",), "주요경영사항", "watch"),
    (("영업(잠정)실적", "영업실적"), "실적", "watch"),
    (("매출액 또는 손익구조",), "손익 변동", "watch"),
    (("합병", "분할"), "합병·분할", "watch"),
    (("자기주식",), "자사주", "watch"),
    (("단일판매", "공급계약"), "공급계약", "watch"),
]


def classify(title: str) -> tuple[str, str]:
    for words, label, level in RULES:
        if any(w in title for w in words):
            return label, level
    return "공시", "info"


def clean_title(title: str) -> str:
    t = re.sub(r"^\s*(\(주\)\s*\S+|\S+\(주\))\s+", "", html.unescape(title or "")).strip()
    return t[:70]


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


async def _get(session: aiohttp.ClientSession, sem: asyncio.Semaphore, url: str):
    async with sem:
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except Exception:
            return None


def parse_disclosures(rows: list[dict], now: datetime) -> list[list]:
    """-> [[날짜, 태그, 위험도, 제목], ...] 최신 순"""
    cutoff = (now - timedelta(days=DISC_DAYS)).date().isoformat()
    out, seen = [], set()
    for x in rows or []:
        title, day = x.get("title") or "", (x.get("datetime") or "")[:10]
        if not title or day < cutoff or any(n in title for n in NOISE):
            continue
        label, level = classify(title)
        short = clean_title(title)
        if (day, short) in seen:
            continue
        seen.add((day, short))
        out.append([day, label, level, short])
    out.sort(key=lambda r: r[0], reverse=True)
    return out[:MAX_DISC]


def parse_news(groups: list[dict], name: str) -> list[list]:
    """-> [[날짜 시각, 언론사, 제목, 언론사코드, 기사번호], ...] 최신 순, 종목명이 제목에 든 것만"""
    keys = {_norm(name)}
    first = (name or "").split()[0] if name else ""
    if len(_norm(first)) >= 3:
        keys.add(_norm(first))
    keys.discard("")
    out, seen = [], set()
    for g in groups or []:
        for it in (g.get("items") or []):
            title = html.unescape(it.get("title") or "").strip()
            if not title or not any(k in _norm(title) for k in keys) or title in seen:
                continue
            office, art, dt = str(it.get("officeId") or ""), str(it.get("articleId") or ""), str(it.get("datetime") or "")
            if not (office.isdigit() and art.isdigit() and len(dt) >= 12):
                continue
            seen.add(title)
            out.append([f"{dt[:4]}-{dt[4:6]}-{dt[6:8]} {dt[8:10]}:{dt[10:12]}", it.get("officeName") or "", title[:90], office, art])
    out.sort(key=lambda r: r[0], reverse=True)
    return out[:MAX_NEWS]


async def fetch_one(session: aiohttp.ClientSession, sem: asyncio.Semaphore, code: str, name: str, now: datetime | None = None) -> dict | None:
    """-> {"d": 공시, "n": 뉴스} (둘 다 못 받으면 None)"""
    now = now or datetime.now(KST)
    disc, news = await asyncio.gather(_get(session, sem, DISC_URL.format(code=code)), _get(session, sem, NEWS_URL.format(code=code)))
    if disc is None and news is None:
        return None
    return {"d": parse_disclosures(disc, now) if isinstance(disc, list) else [], "n": parse_news(news, name) if isinstance(news, list) else []}
