"""사이클 전략 스캔. 기존 스캔(scanner.py)이 받아 둔 일봉 캔들을 재사용하고, 일봉 조건을 통과한 종목만 4시간(450개)을,
4시간 준비까지 통과한 종목만 1시간을 추가로 받는다 (호출량 절감). 판단 규칙은 cycle_signals.py, 지지 터치는 support_zones.py.

추천 조건: 일봉 · 4시간 · 1시간 구조 조건을 모두 통과하고 1시간 거래량 폭발이 있어야 한다 (B급). 여기에 지지 구간 터치까지
있으면 A급이다. 구조 조건만 통과하고 거래량이 없는 종목은 추천하지 않고 대시보드 '준비 현황'에만 보인다."""

import asyncio
from dataclasses import dataclass, field

import aiohttp
from aiolimiter import AsyncLimiter

from src import config, cycle_signals, support_zones
from src.exchanges import upbit_client
from src.formatting import fmt_price

DAY_CANDLES = 200
H1_CANDLES = 200
TIER_TEXT = {"A": "A급 (거래량 + 지지 터치)", "B": "B급 (거래량)"}


@dataclass
class CycleSignal:
    market: str
    tier: str  # "A" 지지 터치까지 / "B" 거래량까지
    price: float = 0.0
    detail: dict = field(default_factory=dict)


async def _candles(session, market, frame, count, limiter, semaphore, cache, key=None):
    """캐시에 있으면 재사용하고 없으면 받아서 캐시에 넣는다. 캐시 키의 프레임 이름이 같아도 개수가 다르면 따로 둔다."""
    key = (market, key or frame)
    if cache is not None and key in cache:
        return cache[key]
    async with semaphore, limiter:
        df = await upbit_client.fetch_candles(session, market, frame, count=count)
    if cache is not None:
        cache[key] = df
    return df


async def _scan_one(market, session, limiter, semaphore, cache) -> tuple[dict | None, dict | None]:
    """(대시보드용 상태, 추천 후보 정보). 일봉 조건을 못 넘으면 (None, None)."""
    day = await _candles(session, market, "day", DAY_CANDLES, limiter, semaphore, cache)
    if day.empty or len(day) < 40:
        return None, None
    if day["value"].iloc[-1] < config.MIN_DAILY_TRADE_VALUE_KRW:
        return None, None  # 기존 전략과 같은 최소 유동성 필터
    if not cycle_signals.eval_day(day)["ok"][-1]:
        return None, None

    h4 = await _candles(session, market, "4h", config.CYCLE_H4_CANDLES, limiter, semaphore, cache, key="4h-long")
    if h4.empty:
        return None, None
    state = cycle_signals.evaluate_now(day, h4, None)
    if state["stage"] < 2:
        return None, None  # 4시간 상승 체제·여력을 못 넘으면 준비 현황에도 올리지 않는다
    state["price"] = float(day["close"].iloc[-1])
    if state["stage"] < 3:
        return state, None

    h1 = await _candles(session, market, "1h", H1_CANDLES, limiter, semaphore, cache)
    if h1.empty:
        return state, None
    state = cycle_signals.evaluate_now(day, h1=h1, h4=h4)
    state["price"] = float(h1["close"].iloc[-1])
    if state["stage"] == 4 and state.get("h1_vol"):
        return state, {"day": day, "h4": h4, "state": state}
    return state, None


async def scan_cycle(
    session: aiohttp.ClientSession, markets: list[str], cache: dict | None, exclude: set[str]
) -> tuple[list[CycleSignal], dict[str, dict]]:
    """(새 추천 신호 목록, 준비 현황 상태 맵). exclude(최근에 이미 추천한 종목)는 신호에서만 뺀다 — 상태는 그대로 보여준다."""
    limiter = AsyncLimiter(config.UPBIT_RATE_LIMIT_PER_SEC, 1)
    semaphore = asyncio.Semaphore(config.UPBIT_CONCURRENCY)

    async def safe(market):
        try:
            return market, *(await _scan_one(market, session, limiter, semaphore, cache))
        except Exception as exc:  # 한 종목의 조회 실패로 스캔 전체가 죽지 않게 한다
            print(f"  {market} 사이클 스캔 실패(이번 사이클은 건너뜀): {exc}")
            return market, None, None

    results = await asyncio.gather(*[safe(m) for m in markets])
    states = {m: s for m, s, _ in results if s is not None}
    pending = [(m, p) for m, _, p in results if p is not None and m not in exclude]
    if not pending:
        return [], states

    # 캔들은 마감된 것만 쓰므로 마지막 종가가 최대 1시간 전 값이다. 진입가와 지지 터치 판단은 실시간 시세로 한다.
    prices = await upbit_client.fetch_ticker_prices(session, [m for m, _ in pending])
    signals = []
    for market, p in pending:
        price = prices.get(market, states[market]["price"])
        touch, info = support_zones.support_touch(p["h4"], p["day"], price)
        detail = {**p["state"], "price": price, "support": info}
        states[market] = {**detail, "tier": "A" if touch else "B"}
        signals.append(CycleSignal(market, "A" if touch else "B", price, detail))
    signals.sort(key=lambda s: (s.tier, s.market))
    return signals, states


def _kd(pair) -> str:
    k, d = pair if pair else (None, None)
    return "—" if k is None else f"{k:.0f}/{d:.0f}"


def build_entry_message(sig: CycleSignal) -> str:
    d = sig.detail
    lines = [
        f"🌀 사이클 추천 · {TIER_TEXT[sig.tier]}",
        f"{sig.market} @ {fmt_price(sig.price)}",
        f"일봉 단기 {_kd(d.get('day_short'))} · 4시간 단기 {_kd(d.get('h4_short'))} 중기 {_kd(d.get('h4_mid'))} "
        f"장기 {_kd(d.get('h4_long'))} · 1시간 단기 {_kd(d.get('h1_short'))} (K/D)",
    ]
    sup = d.get("support")
    if sup and sig.tier == "A":
        lines.append(f"🧱 지지 구간 {fmt_price(sup['low'])} ~ {fmt_price(sup['high'])} (강도 {sup['strength']})")
    lines.append(
        f"🎯 손절 -{config.CYCLE_STOP_PCT:g}% · 4시간 단기 80+ 거래량 폭발 시 절반 매도 신호 · 나머지는 고점 대비 -{config.CYCLE_TRAIL_PCT:g}%"
    )
    return "\n".join(lines)
