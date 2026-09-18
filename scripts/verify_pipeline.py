"""파이프라인 동작 확인용 스크립트.
바이낸스 BTC 추세 + 업비트 BTC 스토캐스틱 RSI(단/중/장)를 실제 API로 가져와 계산 결과를 출력한다.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src.btc_trend import is_trend_favorable
from src.exchanges import binance_client, upbit_client
from src.indicators.stoch_rsi import stoch_rsi_all_periods


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        binance_daily = await binance_client.fetch_klines(session, "BTCUSDT", "1d", 100)
        favorable = is_trend_favorable(binance_daily["close"])
        print(f"[BTC 추세] 바이낸스 USDT-BTC 일봉 기준 MA20 위 2일 이상 유지: {favorable}")

        for timeframe in ("day", "4h", "1h"):
            df = await upbit_client.fetch_candles(session, "KRW-BTC", timeframe, count=100)
            results = stoch_rsi_all_periods(df["close"])
            latest = {name: (r["k"].iloc[-1], r["d"].iloc[-1]) for name, r in results.items()}
            print(f"\n[업비트 KRW-BTC {timeframe}] 최신 %K/%D")
            for period_name, (k, d) in latest.items():
                print(f"  {period_name:5s}: %K={k:6.2f}  %D={d:6.2f}")


if __name__ == "__main__":
    asyncio.run(main())
