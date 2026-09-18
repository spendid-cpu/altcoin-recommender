"""바이낸스 공개 시세 API 클라이언트. BTC 추세 판단(USDT-BTC 일봉)에만 사용."""

import aiohttp
import pandas as pd

# api.binance.com은 일부 지역(예: GitHub Actions 러너가 주로 뜨는 미국 클라우드 IP 대역)에서
# 451(지역 차단)을 반환한다. data-api.binance.vision은 바이낸스가 공개 시세 데이터 조회 전용으로
# 제공하는 미러 도메인이라 계정/거래 기능은 없지만 지역 제한이 없다 — 우리는 시세만 읽으므로 이걸 쓴다.
BASE_URL = "https://data-api.binance.vision/api/v3"


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
