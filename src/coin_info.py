"""코인 추천 카드의 참고 정보: 최근 주봉 이력(종가·주간 거래대금·RSI)과 시가총액. 추천 규칙에는 쓰지 않는다.

- 주봉: 업비트 candles/weeks (월요일 09:00 KST 시작). 거래대금은 업비트 원화 시장의 실제 체결 금액(candle_acc_trade_price).
  RSI는 주 종가 기준 Wilder RSI(14)이고, 이번 주는 아직 진행 중이라 종가가 바뀌는 대로 같이 바뀐다(대시보드가 '진행 중'으로 표시).
- 시가총액: src/market_cap.py가 받아 둔 CoinGecko 원화 시가총액 상위 코인에서 티커(심볼)로 후보를 찾고, 업비트 현재가와 CoinGecko 가격이 비슷한(PRICE_TOL 안) 것만
  같은 코인으로 본다(같은 티커를 쓰는 다른 코인을 걸러내려고). 맞는 후보가 없으면 시총을 표시하지 않는다. 업비트는 시총을 주지 않는다.
- 공지·뉴스: src/coin_news.py. 업비트 공지는 매 사이클, 시총이 큰 코인의 뉴스는 주봉과 같은 주기로 받는다.
대상은 진행 중인 추천 종목(카드가 붙는 곳)이고, 1시간에 한 번 새로 받는다(새 종목은 바로)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp
from aiolimiter import AsyncLimiter

from src import coin_news, jsonutil, market_cap, price_tracker, state_store
from src.exchanges import upbit_client
from src.indicators.stoch_rsi import rsi

META_KEY = "coin_info_json"
REFRESH_MIN = 55
PRICE_TOL = 0.25               # 업비트 가격 대비 CoinGecko 가격이 +-25% 안이면 같은 코인 (김치 프리미엄 여유)
FETCH_WEEKS = 40               # RSI(14)가 안정되도록 넉넉히 받고
HIST_WEEKS = 26                # 화면에는 최근 26주만 보낸다
_limiter = AsyncLimiter(6, 1)


def needed_markets() -> list[str]:
    """진행 중인 추천 종목(대시보드에서 카드가 붙는 곳)."""
    return sorted(set(price_tracker.active_tracked_markets()))


def match_cap(market: str, price: float | None, gecko: dict[str, list]) -> float | None:
    """업비트 종목의 시가총액: 같은 심볼 후보 중 가격이 업비트와 PRICE_TOL 안에서 가장 가까운 것. 없으면 None."""
    if not price:
        return None
    best = None
    for cap, gp in gecko.get(market[4:].lower(), []):
        if not gp:
            continue
        ratio = gp / price
        if abs(ratio - 1) <= PRICE_TOL and (best is None or abs(ratio - 1) < best[0]):
            best = (abs(ratio - 1), cap)
    return best[1] if best else None


def build_weekly(rows: list[dict], now: datetime | None = None) -> dict | None:
    """업비트 주봉(최신 순 응답) -> {"w": [[주 시작일, 종가, 주간 거래대금(원), RSI, 시가, 고가, 저가], ...오래된 순], "live": 마지막 주가 진행 중인지}."""
    if len(rows) < 3:
        return None
    now = now or datetime.now(timezone.utc)
    rows = sorted(rows, key=lambda x: x["candle_date_time_utc"])
    closes = [float(x["trade_price"]) for x in rows]
    import pandas as pd  # 지연 로딩 (모듈을 가볍게 불러오려고)
    r = rsi(pd.Series(closes), 14)
    out = []
    for x, c, rv in zip(rows, closes, r):
        out.append([x["candle_date_time_kst"][:10], c, round(float(x["candle_acc_trade_price"])), None if rv != rv else round(float(rv), 1),
                    float(x["opening_price"]), float(x["high_price"]), float(x["low_price"])])
    last_start = datetime.fromisoformat(rows[-1]["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
    return {"w": out[-HIST_WEEKS:], "live": last_start + timedelta(days=7) > now}


async def fetch_weekly(session: aiohttp.ClientSession, market: str) -> dict | None:
    try:
        async with _limiter:
            data = await upbit_client._get_json(session, "candles/weeks", {"market": market, "count": FETCH_WEEKS})
        return build_weekly(data)
    except Exception as exc:
        print(f"[코인 주봉] {market} 조회 실패(건너뜀): {exc!r}")
        return None


def export() -> dict | None:
    raw = state_store.get_meta(META_KEY)
    return json.loads(raw) if raw else None


async def refresh(session: aiohttp.ClientSession) -> None:
    """주봉·뉴스는 REFRESH_MIN분마다(새 종목은 바로), 공지와 시가총액 매칭은 매 사이클 반영한다."""
    prev = export() or {}
    now = datetime.now(timezone.utc)
    last = datetime.fromisoformat(prev["at"]) if prev.get("at") else None
    stale = last is None or (now - last).total_seconds() >= REFRESH_MIN * 60
    markets = needed_markets()
    have = prev.get("markets") or {}
    todo = markets if stale else [m for m in markets if m not in have]
    cached = market_cap.load_cached() or {}
    gecko = cached.get("cands") or {}

    try:
        notices = await coin_news.refresh_notices(session)
    except Exception as exc:
        print(f"[업비트 공지] 처리 실패(공지 없이 진행): {exc!r}")
        notices = []
    results = await asyncio.gather(*(fetch_weekly(session, m) for m in todo)) if todo else []
    out = {m: have[m] for m in markets if m in have}
    for m, wk in zip(todo, results):
        if wk:
            wk["news"] = (have.get(m) or {}).get("news") or []  # 뉴스는 아래에서 새로 받을 때까지 이전 값
            out[m] = wk
    for m, v in out.items():
        v.pop("cap", None)
        cap = match_cap(m, v["w"][-1][1], gecko)  # 마지막 주봉 종가 = 업비트 현재가
        if cap:
            v["cap"] = cap
        v["notes"] = coin_news.notes_for(m, notices)
    if stale:  # 시총이 큰 코인만 뉴스를 붙인다 (소형 알트는 무관한 기사가 섞여서)
        big = [m for m, v in out.items() if v.get("cap", 0) >= coin_news.NEWS_MIN_CAP]
        try:
            names = {m: x.get("korean_name") or "" for x in await upbit_client._fetch_market_details(session) for m in [x["market"]] if m in big}
            for m, items in (await coin_news.fetch_news_many(session, names)).items():
                out[m]["news"] = items
        except Exception as exc:
            print(f"[코인 뉴스] 수집 실패(이전 값 유지): {exc!r}")
    payload = {"markets": out, "at": now.isoformat() if stale else prev.get("at"), "cap_at": cached.get("generated_at"), "cap_src": "CoinGecko",
               "notices_at": state_store.get_meta(coin_news.OK_KEY)}
    if not todo and payload == prev:
        return
    state_store.set_meta(META_KEY, jsonutil.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"[코인 참고] 주봉 {len(out)}종목 · 시총 {sum(1 for v in out.values() if 'cap' in v)}종목 · 공지 {sum(1 for v in out.values() if v.get('notes'))}종목 · 뉴스 {sum(1 for v in out.values() if v.get('news'))}종목 (주봉 새로 받음 {len(todo)}종목)")
