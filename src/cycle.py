"""알트코인 사이클 탭: 최근 CYCLE_LOOKBACK_DAYS일 안의 최저가 대비 지금 가격이 몇 % 위인지(상승 사이클),
그리고 최근 최고가 대비 몇 % 아래인지(하락 사이클)로 전 종목을 각각 5개 구간으로 나눈다. 매수 신호가
아니라 '아직 안 오른 종목' / '많이 눌린 종목'을 찾아보는 참고 자료다.

pipeline이 스캔 중에 이미 받아 둔 일봉 캔들(candle_cache)을 그대로 쓰기 때문에 API를 추가로 부르지 않는다
(일봉은 게이트를 통과하든 못하든 종목마다 항상 먼저 받아 캐시에 들어있다). 현재가만 한 번 배치로 조회한다.
'저점/고점'은 일봉의 장중 저가·고가 기준이다 — 종가만 보면 하루 안의 급등락(꼬리)을 놓친다.
시가총액은 src/market_cap.py가 따로 갱신해 둔 캐시를 그대로 붙인다(참고치, 없으면 null).
"""

import json
from datetime import datetime, timezone

import aiohttp
import pandas as pd

from src import config, jsonutil, market_cap, state_store
from src.exchanges import upbit_client

CYCLE_KEY = "cycle_json"

# (하한%, 상한% 미포함, 이름) — 저점 대비 상승률로 나눈 구간(상승 사이클).
UP_TIERS = [
    (0, 10, "저점권"),
    (10, 25, "초기 상승"),
    (25, 50, "상승 중"),
    (50, 100, "고점 근접"),
    (100, float("inf"), "급등"),
]
# (하한%, 상한% 미포함, 이름) — 고점 대비 하락률(항상 0 이상 양수로 잰다)로 나눈 구간(하락 사이클).
DOWN_TIERS = [
    (0, 10, "고점권"),
    (10, 25, "초기 조정"),
    (25, 50, "조정 중"),
    (50, 75, "큰 조정"),
    (75, 100.0001, "폭락"),
]


def _tier_of(pct: float, tiers: list[tuple[float, float, str]]) -> int:
    for i, (lo, hi, _) in enumerate(tiers):
        if lo <= pct < hi:
            return i
    return 0 if pct < 0 else len(tiers) - 1


async def build(session: aiohttp.ClientSession, cache: dict, markets: list[str]) -> dict:
    skip = config.EXCLUDED_MARKETS | config.NO_RECOMMEND_MARKETS
    rows = []
    for market in markets:
        if market in skip:
            continue
        day = cache.get((market, "day"))
        if day is None or day.empty or len(day) < 5:
            continue
        if float(day["value"].iloc[-1]) < config.MIN_DAILY_TRADE_VALUE_KRW:
            continue
        window = day.tail(config.CYCLE_LOOKBACK_DAYS)
        low_col = window["low"] if "low" in window.columns else window["close"]  # 옛 캐시 방어용 폴백
        high_col = window["high"] if "high" in window.columns else window["close"]
        low_pos = low_col.astype(float).idxmin()
        high_pos = high_col.astype(float).idxmax()
        rows.append({
            "market": market,
            "low": float(low_col.loc[low_pos]), "low_at": str(window["time"].loc[low_pos]),
            "high": float(high_col.loc[high_pos]), "high_at": str(window["time"].loc[high_pos]),
            "last_close": float(day["close"].iloc[-1]),
        })

    now = datetime.now(timezone.utc)
    if not rows:
        return {"generated_at": now.isoformat(), "lookback_days": config.CYCLE_LOOKBACK_DAYS, "items": []}

    prices = await upbit_client.fetch_ticker_prices(session, [r["market"] for r in rows])
    items = []
    for r in rows:
        current = prices.get(r["market"], r["last_close"])
        up_pct = (current / r["low"] - 1) * 100 if r["low"] else 0.0
        down_pct = max(0.0, (1 - current / r["high"]) * 100) if r["high"] else 0.0  # 고점을 새로 뚫었으면 0
        low_date = pd.Timestamp(r["low_at"])
        high_date = pd.Timestamp(r["high_at"])
        symbol = r["market"].split("-", 1)[1]
        items.append({
            "market": r["market"],
            "low": round(r["low"], 8), "low_date": low_date.strftime("%Y-%m-%d"),
            "days_since_low": max(0, (pd.Timestamp.now() - low_date).days),
            "high": round(r["high"], 8), "high_date": high_date.strftime("%Y-%m-%d"),
            "days_since_high": max(0, (pd.Timestamp.now() - high_date).days),
            "current": round(current, 8),
            "pct_from_low": round(up_pct, 2), "tier_up": _tier_of(up_pct, UP_TIERS),
            "pct_from_high": round(down_pct, 2), "tier_down": _tier_of(down_pct, DOWN_TIERS),
            "market_cap": market_cap.get(symbol),
        })
    items.sort(key=lambda x: x["pct_from_low"])
    return {"generated_at": now.isoformat(), "lookback_days": config.CYCLE_LOOKBACK_DAYS, "items": items}


async def refresh(session: aiohttp.ClientSession, cache: dict, markets: list[str]) -> None:
    data = await build(session, cache, markets)
    up_counts, down_counts = [0] * len(UP_TIERS), [0] * len(DOWN_TIERS)
    for it in data["items"]:
        up_counts[it["tier_up"]] += 1
        down_counts[it["tier_down"]] += 1
    state_store.set_meta(CYCLE_KEY, jsonutil.dumps(jsonutil.finite(data), ensure_ascii=False))
    fmt = lambda tiers, counts: " · ".join(f"{name} {n}" for (_, _, name), n in zip(tiers, counts))  # noqa: E731
    print(f"[사이클] {len(data['items'])}종목 · 상승: {fmt(UP_TIERS, up_counts)} · 하락: {fmt(DOWN_TIERS, down_counts)}")


def load_cached() -> dict | None:
    raw = state_store.get_meta(CYCLE_KEY)
    return json.loads(raw) if raw else None
