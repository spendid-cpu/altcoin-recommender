"""바이낸스 공개 시세 API 클라이언트. BTC 추세 판단(USDT-BTC 일봉)에만 사용."""

import aiohttp
import pandas as pd

BASE_URL = "https://api.binance.com/api/v3"


async def fetch_klines(
    session: aiohttp.ClientSession, symbol: str = "BTCUSDT", interval: str = "1d", limit: int = 200
) -> pd.DataFrame:
    """오래된 캔들이 먼저 오도록 반환한다 (바이낸스는 원래 오래된순이라 별도 정렬 불필요)."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    async with session.get(f"{BASE_URL}/klines", params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    df = pd.DataFrame(
        data,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
        ],
    )
    df["close"] = df["close"].astype(float)
    df["time"] = pd.to_datetime(df["close_time"], unit="ms")
    return df[["time", "close"]]
