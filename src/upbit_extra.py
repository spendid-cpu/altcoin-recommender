"""업비트 공개 API로 얻는 추가 참고 정보 세 가지: 체결강도(실제 체결 중 사는 쪽 비중), 업비트 공식 시장 경고 신호, BTC 김치 프리미엄.

- 체결강도: trades/ticks의 ask_bid(BID=매수 체결, ASK=매도 체결)로 최근 FLOW_WINDOW_MIN분(최대 500건) 체결금액 중 매수 체결 비중을 본다.
  호가와 달리 실제로 체결된 거래라 허수 주문이 없다. 추천 순간의 값은 추천 기록(detail.flow)에 남겨 나중에 사전 등록 검증에 쓴다.
- 시장 경고: market/all?is_details=true의 market_event.caution (가격 급등락·거래량 급증·입금량 급증·해외 가격 차이·소수 계좌 집중)과
  투자유의 지정. 과거 상태는 업비트가 안 줘서 추천 순간부터 기록(detail.caution)한다.
- 김프: 업비트 KRW-BTC / (바이낸스 BTCUSDT x 업비트 KRW-USDT) - 1. 알트는 같은 티커를 다른 프로젝트가 쓰는 경우가 많아(예: 수천 % 오차)
  종목별 김프는 계산하지 않고 공식 '해외 가격 차이' 경고로 대신한다.
전부 참고 표시이고 추천 규칙에는 쓰지 않는다."""

import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp
from aiolimiter import AsyncLimiter

from src import jsonutil, price_tracker, state_store
from src.exchanges import binance_client, upbit_client

META_KEY = "upbit_extra_json"
FLOW_WINDOW_MIN = 60     # 체결강도를 재는 최근 구간
FLOW_TICKS = 500         # 한 번에 받는 체결 수 (업비트 최대)
FLOW_EVERY_MIN = 15      # 추적 종목 체결강도를 새로 받는 간격 (요청이 종목 수만큼이라 매 사이클은 안 한다)
MIN_TRADES = 10          # 구간 안 체결이 이보다 적으면 비중을 계산하지 않는다
KIMCHI_STEP_MIN = 30
KIMCHI_POINTS = 48       # 30분 x 48 = 24시간
FLAG_LABEL = {
    "WARNING": "투자유의", "PRICE_FLUCTUATIONS": "가격 급등락", "TRADING_VOLUME_SOARING": "거래량 급증",
    "DEPOSIT_AMOUNT_SOARING": "입금량 급증", "GLOBAL_PRICE_DIFFERENCES": "해외 가격 차이", "CONCENTRATION_OF_SMALL_ACCOUNTS": "소수 계좌 집중",
}
_limiter = AsyncLimiter(8, 1)  # 업비트 시세 API 초당 10회 제한 안쪽


def flag_text(flags: list[str]) -> str:
    return " · ".join(FLAG_LABEL.get(f, f) for f in flags)


def summarize_flow(ticks: list[dict], now_ms: int | None = None) -> dict | None:
    """체결 틱(최신 순) -> {buy: 체결금액 기준 매수 비중 0~1, n: 건수, span_min: 실제 구간(분)}. 체결이 너무 적으면 None."""
    now_ms = now_ms or int(time.time() * 1000)
    win = [t for t in ticks if t["timestamp"] >= now_ms - FLOW_WINDOW_MIN * 60_000]
    if len(win) < MIN_TRADES:
        return None
    total = sum(t["trade_price"] * t["trade_volume"] for t in win)
    if total <= 0:
        return None
    buy = sum(t["trade_price"] * t["trade_volume"] for t in win if t["ask_bid"] == "BID")
    span = (now_ms - min(t["timestamp"] for t in win)) / 60_000
    return {"buy": round(buy / total, 3), "n": len(win), "span_min": round(min(span, FLOW_WINDOW_MIN), 1)}


async def fetch_flow(session: aiohttp.ClientSession, markets: list[str]) -> dict[str, dict]:
    async def one(m: str):
        try:
            async with _limiter:
                data = await upbit_client._get_json(session, "trades/ticks", {"market": m, "count": FLOW_TICKS})
            return m, summarize_flow(data)
        except Exception as exc:
            print(f"[체결] {m} 조회 실패(건너뜀): {exc!r}")
            return m, None
    results = await asyncio.gather(*(one(m) for m in markets))
    return {m: s for m, s in results if s}


async def fetch_flags(session: aiohttp.ClientSession) -> dict[str, list[str]]:
    """경고 신호가 하나라도 켜진 KRW 마켓 -> 켜진 신호 이름 목록 (신호가 없는 마켓은 빠진다)."""
    data = await upbit_client._get_json(session, "market/all", {"is_details": "true"})
    out: dict[str, list[str]] = {}
    for x in data:
        if not x["market"].startswith("KRW-"):
            continue
        ev = x.get("market_event") or {}
        fl = [k for k, v in (ev.get("caution") or {}).items() if v]
        if ev.get("warning"):
            fl.insert(0, "WARNING")
        if fl:
            out[x["market"]] = fl
    return out


async def entry_snapshot(session: aiohttp.ClientSession, markets: list[str]) -> dict[str, dict]:
    """추천 순간에 남길 값: 종목별 {flow: 체결강도 또는 None, caution: 켜진 경고 신호 목록}. 일부가 실패해도 받은 것만 돌려준다."""
    flow, flags = {}, None
    try:
        flow = await fetch_flow(session, markets)
    except Exception as exc:
        print(f"[체결] 진입 시점 체결 조회 실패(기록만 생략): {exc!r}")
    try:
        flags = await fetch_flags(session)
    except Exception as exc:
        print(f"[경고] 진입 시점 경고 조회 실패(기록만 생략): {exc!r}")
    out = {}
    for m in markets:
        row = {}
        if m in flow:
            row["flow"] = flow[m]
        if flags is not None:
            row["caution"] = flags.get(m, [])
        out[m] = row
    return out


async def fetch_kimchi_btc(session: aiohttp.ClientSession) -> dict | None:
    prices = await upbit_client.fetch_ticker_prices(session, ["KRW-BTC", "KRW-USDT"])
    btc_krw, usdt_krw = prices.get("KRW-BTC"), prices.get("KRW-USDT")
    if not btc_krw or not usdt_krw:
        return None
    btc_usdt = await binance_client.fetch_price(session, "BTCUSDT")
    if not btc_usdt:
        return None
    return {"premium_pct": round((btc_krw / (btc_usdt * usdt_krw) - 1) * 100, 3), "btc_krw": btc_krw, "btc_usdt": btc_usdt, "usdt_krw": usdt_krw}


def export() -> dict | None:
    raw = state_store.get_meta(META_KEY)
    return json.loads(raw) if raw else None


async def refresh(session: aiohttp.ClientSession) -> None:
    """매 사이클: 경고 신호·BTC 김프를 새로 받고, 추적 종목 체결강도는 FLOW_EVERY_MIN분마다 받아 저장한다."""
    prev = export() or {}
    now_ms = int(time.time() * 1000)
    payload = {"flags": prev.get("flags") or {}, "flow": prev.get("flow") or {}, "flow_at_ms": prev.get("flow_at_ms", 0),
               "kimchi": prev.get("kimchi"), "kimchi_hist": prev.get("kimchi_hist") or []}
    try:
        payload["flags"] = await fetch_flags(session)
    except Exception as exc:
        print(f"[경고] 시장 경고 조회 실패(이전 값 유지): {exc!r}")
    try:
        k = await fetch_kimchi_btc(session)
        if k:
            payload["kimchi"] = k
            hist = payload["kimchi_hist"]
            if not hist or now_ms - hist[-1][0] >= KIMCHI_STEP_MIN * 60_000 - 90_000:
                hist.append([now_ms, k["premium_pct"]])
            payload["kimchi_hist"] = hist[-KIMCHI_POINTS:]
    except Exception as exc:
        print(f"[김프] 계산 실패(이전 값 유지): {exc!r}")
    if now_ms - payload["flow_at_ms"] >= FLOW_EVERY_MIN * 60_000 - 90_000:
        try:
            markets = price_tracker.active_tracked_markets()
            payload["flow"] = await fetch_flow(session, markets)
            payload["flow_at_ms"] = now_ms
        except Exception as exc:
            print(f"[체결] 추적 종목 체결 조회 실패(이전 값 유지): {exc!r}")
    payload["at"] = datetime.now(timezone.utc).isoformat()
    state_store.set_meta(META_KEY, jsonutil.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"[업비트 참고] 경고 {len(payload['flags'])}종목 · 체결강도 {len(payload['flow'])}종목 · 김프 {payload['kimchi']['premium_pct'] if payload['kimchi'] else '—'}%")
