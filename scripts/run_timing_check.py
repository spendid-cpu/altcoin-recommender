"""백테스트 시간 기준 점검: 캔들을 '시작 시각'으로 쓴 이전 방식(미래 정보 누출)과 '마감 시각'으로 쓴 올바른 방식의
결과를 나란히 비교한다. 같은 캔들 데이터(디스크 캐시)로 두 번 계산한다.

사용법: python scripts/run_timing_check.py [마켓개수 제한]
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
import pandas as pd
from aiolimiter import AsyncLimiter

from src import config
from src.backtest import build_market_signals, load_raw_frames, summarize, trades_for_validity, trades_with_factors
from src.exchanges import binance_client, upbit_client

VALIDITY_HOURS = [6, 24, 72, 168]
SCORE_CUTS = [0, 11, 13, 16, 100]
SCORE_LABELS = ["<11", "11~13", "13~16", "16+"]


def stats(returns: pd.Series) -> str:
    r = returns.dropna()
    if r.empty:
        return "n=   0"
    return f"n={len(r):4d} 승률={(r > 0).mean() * 100:5.1f}% 평균={r.mean():+6.2f}%"


async def main(limit: int | None) -> None:
    async with aiohttp.ClientSession() as session:
        markets = await upbit_client.fetch_markets(session)
        if limit:
            markets = markets[:limit]
        btc_df = await binance_client.fetch_klines(session, "BTCUSDT", "1d", 200)

        limiter = AsyncLimiter(config.UPBIT_RATE_LIMIT_PER_SEC, 1)
        semaphore = asyncio.Semaphore(config.UPBIT_CONCURRENCY)

        async def load(market: str):
            async with semaphore, limiter:
                try:
                    return market, await load_raw_frames(session, market)
                except Exception as exc:
                    print(f"  {market} 스킵: {exc}")
                    return market, None

        loaded = await asyncio.gather(*(load(m) for m in markets))
    raw = {m: f for m, f in loaded if f}
    print(f"캔들 데이터 {len(raw)}개 마켓\n")

    for use_close, name in ((False, "이전 방식: 캔들 시작 시각 (미래 정보 누출)"), (True, "올바른 방식: 캔들 마감 시각")):
        signals = [build_market_signals(m, f, use_close_time=use_close) for m, f in raw.items()]
        print("=" * 78)
        print(name)
        print("=" * 78)

        print("[유효기간별 — 일봉/4시간/1시간 게이트 + 15분 골든크로스]")
        for hours in VALIDITY_HOURS:
            trades = [t for s in signals for t in trades_for_validity(s, hours)]
            r = summarize(trades)
            print(f"  {hours:3d}시간: 거래 {r['trade_count']:5d}건 | +1일 승률 {r['1d']['win_rate']}% 평균 {r['1d']['avg_return']:+}% "
                  f"| +3일 승률 {r['3d']['win_rate']}% 평균 {r['3d']['avg_return']:+}%")

        frames = [trades_with_factors(s, btc_df=btc_df) for s in signals]
        df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
        df["bucket"] = pd.cut(df["score"], bins=SCORE_CUTS, labels=SCORE_LABELS, right=False)
        views = {
            "전체": df,
            "+유동성 필터(3억원)": df[df["day_value_ok"]],
            "+유동성+BTC 필터": df[df["day_value_ok"] & df["btc_ok"]],
        }
        print(f"\n[점수 구간별 — 유효기간 {config.VALIDITY_WINDOW_HOURS}시간]")
        for label, view in views.items():
            print(f"  ({label}: {len(view)}건)")
            for b in SCORE_LABELS:
                g = view[view["bucket"] == b]
                print(f"    점수 {b:6s} +1일 [{stats(g['ret_1d'])}]  +3일 [{stats(g['ret_3d'])}]")

        base = views["+유동성 필터(3억원)"]
        print("\n[개별 요인 True vs False — 유동성 필터 적용, +3일]")
        for col in ("day_golden_cross", "h4_golden_cross", "h1_golden_cross", "day_long_up", "h4_long_up", "h1_long_up",
                    "day_mid_up"):
            print(f"  {col:17s} True [{stats(base.loc[base[col], 'ret_3d'])}]  False [{stats(base.loc[~base[col], 'ret_3d'])}]")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("limit", nargs="?", type=int, default=None)
    asyncio.run(main(parser.parse_args().limit))
