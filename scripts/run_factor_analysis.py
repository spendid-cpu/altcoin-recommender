"""가중치/거래량 배수 튜닝용 분석: 게이트 통과+15분 진입 시점마다의 스코어 구성요소가
실제 이후 수익률과 상관이 있는지 확인한다.

사용법: python scripts/run_factor_analysis.py [마켓개수 제한]
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
import pandas as pd
from aiolimiter import AsyncLimiter

from src import config
from src.backtest import load_market_signals, trades_with_factors
from src.exchanges import upbit_client

FACTOR_COLUMNS = [
    "day_golden_cross", "h4_golden_cross", "h1_golden_cross",
    "day_mid_up", "day_long_up", "h4_mid_up", "h4_long_up", "h1_mid_up", "h1_long_up",
]
VOLUME_THRESHOLDS = [1.5, 2, 3, 4]


def _stats(returns: pd.Series) -> str:
    returns = returns.dropna()
    if len(returns) == 0:
        return "n=0"
    win_rate = (returns > 0).mean() * 100
    return f"n={len(returns):4d}  승률={win_rate:5.1f}%  평균수익률={returns.mean():+6.2f}%"


async def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    async with aiohttp.ClientSession() as session:
        markets = await upbit_client.fetch_markets(session)
        if limit:
            markets = markets[:limit]
        print(f"대상 마켓 {len(markets)}개, 신호 계산 중...")

        limiter = AsyncLimiter(config.UPBIT_RATE_LIMIT_PER_SEC, 1)
        semaphore = asyncio.Semaphore(config.UPBIT_CONCURRENCY)

        async def load(market: str):
            async with semaphore, limiter:
                try:
                    return await load_market_signals(session, market)
                except Exception as e:
                    print(f"  {market} 스킵: {e}")
                    return None

        all_signals = await asyncio.gather(*(load(m) for m in markets))
        all_signals = [s for s in all_signals if s is not None]

        frames = [trades_with_factors(s) for s in all_signals]
        frames = [f for f in frames if not f.empty]
        if not frames:
            print("거래가 하나도 없습니다.")
            return
        df = pd.concat(frames, ignore_index=True)
        print(f"전체 거래 {len(df)}건\n")

        print("=== 스코어 구간별 (현재 가중치 기준) ===")
        df["score_bucket"] = pd.qcut(df["score"], q=4, duplicates="drop")
        for bucket, g in df.groupby("score_bucket", observed=True):
            print(f"  점수 {bucket}: +1d [{_stats(g['ret_1d'])}]  +3d [{_stats(g['ret_3d'])}]")

        print("\n=== 개별 요인별 (True vs False, +3일 기준) ===")
        for col in FACTOR_COLUMNS:
            true_stats = _stats(df.loc[df[col], "ret_3d"])
            false_stats = _stats(df.loc[~df[col], "ret_3d"])
            print(f"  {col:16s} True [{true_stats}]  False [{false_stats}]")

        print("\n=== 거래량 배수 임계값별 (1시간봉 기준, +3일) ===")
        for threshold in VOLUME_THRESHOLDS:
            spike = df["volume_ratio"] >= threshold
            print(f"  배수>={threshold}: 해당 [{_stats(df.loc[spike, 'ret_3d'])}]  미해당 [{_stats(df.loc[~spike, 'ret_3d'])}]")

        print("\n=== 일봉 거래대금(유동성) 5분위별 (+3일) ===")
        df["liquidity_bucket"] = pd.qcut(df["day_trade_value"], q=5, duplicates="drop")
        for bucket, g in df.groupby("liquidity_bucket", observed=True):
            lo, hi = bucket.left, bucket.right
            print(f"  {lo:>18,.0f} ~ {hi:>18,.0f}: [{_stats(g['ret_3d'])}]")


if __name__ == "__main__":
    asyncio.run(main())
