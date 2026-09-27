"""알트코인 사이클 탭: 최근 CYCLE_LOOKBACK_DAYS일 안의 최저 '일봉 종가' 대비 지금 가격이 몇 % 위인지로
전 종목을 5개 구간으로 나눈다. 매수 신호가 아니라 '아직 안 오른 종목'을 찾아보는 참고 자료다.

pipeline이 스캔 중에 이미 받아 둔 일봉 캔들(candle_cache)을 그대로 쓰기 때문에 API를 추가로 부르지 않는다
(일봉은 게이트를 통과하든 못하든 종목마다 항상 먼저 받아 캐시에 들어있다). 현재가만 한 번 배치로 조회한다.
업비트 캔들에는 고가/저가가 없어 '저점'은 종가 기준이다(장중 최저가보다 얕게 잡힐 수 있다).
"""

import json
from datetime import datetime, timezone

import aiohttp
import pandas as pd

from src import config, jsonutil, state_store
from src.exchanges import upbit_client

CYCLE_KEY = "cycle_json"

# (하한%, 상한% 미포함, 이름) — 저점(종가) 대비 상승률로 나눈 구간.
TIERS = [
    (0, 10, "저점권"),
    (10, 25, "초기 상승"),
    (25, 50, "상승 중"),
    (50, 100, "고점 근접"),
    (100, float("inf"), "급등"),
]


def tier_of(pct: float) -> int:
    for i, (lo, hi, _) in enumerate(TIERS):
        if lo <= pct < hi:
            return i
    return 0 if pct < 0 else len(TIERS) - 1


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
        low_pos = window["close"].astype(float).idxmin()
        rows.append({
            "market": market,
            "low": float(window["close"].loc[low_pos]),
            "low_at": str(window["time"].loc[low_pos]),  # 캔들 시작 시각(KST naive) 문자열, 그 날짜만 쓴다
            "last_close": float(day["close"].iloc[-1]),
        })

    now = datetime.now(timezone.utc)
    if not rows:
        return {"generated_at": now.isoformat(), "lookback_days": config.CYCLE_LOOKBACK_DAYS, "items": []}

    prices = await upbit_client.fetch_ticker_prices(session, [r["market"] for r in rows])
    items = []
    for r in rows:
        current = prices.get(r["market"], r["last_close"])
        pct = (current / r["low"] - 1) * 100 if r["low"] else 0.0
        low_date = pd.Timestamp(r["low_at"])
        days_since_low = max(0, (pd.Timestamp.now() - low_date).days)
        items.append({
            "market": r["market"], "low": round(r["low"], 8), "low_date": low_date.strftime("%Y-%m-%d"),
            "days_since_low": days_since_low, "current": round(current, 8),
            "pct_from_low": round(pct, 2), "tier": tier_of(pct),
        })
    items.sort(key=lambda x: x["pct_from_low"])
    return {"generated_at": now.isoformat(), "lookback_days": config.CYCLE_LOOKBACK_DAYS, "items": items}


async def refresh(session: aiohttp.ClientSession, cache: dict, markets: list[str]) -> None:
    data = await build(session, cache, markets)
    state_store.set_meta(CYCLE_KEY, jsonutil.dumps(jsonutil.finite(data), ensure_ascii=False))
    counts = [0] * len(TIERS)
    for it in data["items"]:
        counts[it["tier"]] += 1
    print(f"[사이클] {len(data['items'])}종목 분류: " + " · ".join(f"{name} {n}" for (_, _, name), n in zip(TIERS, counts)))


def load_cached() -> dict | None:
    raw = state_store.get_meta(CYCLE_KEY)
    return json.loads(raw) if raw else None
