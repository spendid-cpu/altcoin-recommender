"""백테스트 실행: 업비트 KRW 마켓 과거 데이터로 게이트+진입 로직을 재생해
'조건 유효기간'을 몇 시간으로 두는 게 좋은지, 전체적으로 승률/평균수익률이 어떤지 확인한다.

사용법: python scripts/run_backtest.py [마켓개수 제한]
  예) python scripts/run_backtest.py 20   -> 처음 20개 마켓만 (빠른 확인용)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiolimiter import AsyncLimiter

from src import config
from src.backtest import load_market_signals, summarize, trades_for_validity
from src.exchanges import upbit_client

VALIDITY_WINDOWS_HOURS = [6, 24, 72, 168]  # 6시간 / 1일 / 3일 / 7일


async def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    async with aiohttp.ClientSession() as session:
        markets = await upbit_client.fetch_markets(session)
        if limit:
            markets = markets[:limit]
        print(f"대상 마켓 {len(markets)}개, 신호 계산 중... (시간이 좀 걸립니다)")

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
        print(f"신호 계산 완료: {len(all_signals)}개 마켓\n")

        for hours in VALIDITY_WINDOWS_HOURS:
            trades = []
            for signals in all_signals:
                trades.extend(trades_for_validity(signals, hours))
            result = summarize(trades)
            print(f"[유효기간 {hours}시간] 거래 {result['trade_count']}건")
            for horizon in ("4h", "1d", "3d"):
                s = result[horizon]
                if s["n"] == 0:
                    print(f"  +{horizon}: 데이터 없음")
                else:
                    print(f"  +{horizon}: n={s['n']:4d}  승률={s['win_rate']:5.1f}%  평균수익률={s['avg_return']:+6.2f}%")
            print()


if __name__ == "__main__":
    asyncio.run(main())
