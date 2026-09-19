"""바이낸스 공개 시세 API 클라이언트. BTC 추세 판단(USDT-BTC 일봉)에만 사용."""

import aiohttp
import pandas as pd

# api.binance.com은 일부 지역(예: GitHub Actions 러너가 주로 뜨는 미국 클라우드 IP 대역)에서
# 451(지역 차단)을 반환한다. data-api.binance.vision은 바이낸스가 공개 시세 데이터 조회 전용으로
# 제공하는 미러 도메인이라 계정/거래 기능은 없지만 지역 제한이 없다 — 우리는 시세만 읽으므로 이걸 쓴다.
BASE_URL = "https://data-api.binance.vision/api/v3"


async def fetch_ohlcv(
    session: aiohttp.ClientSession, symbol: str = "BTCUSDT", interval: str = "1d", limit: int = 200,
    closed_only: bool = True,
) -> pd.DataFrame:
    """시가/고가/저가/종가/거래량까지 포함한 캔들. 오래된 캔들이 먼저 오고, time은 UTC 마감 시각이다.
    closed_only=True(기본)면 아직 마감되지 않은 마지막 캔들은 버린다."""
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
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["time"] = pd.to_datetime(df["close_time"], unit="ms")
    if closed_only:
        df = df[df["time"] <= pd.Timestamp.now(tz="UTC").tz_localize(None)].reset_index(drop=True)
    return df[["time", "open", "high", "low", "close", "volume"]]


async def fetch_klines(
    session: aiohttp.ClientSession, symbol: str = "BTCUSDT", interval: str = "1d", limit: int = 200,
    closed_only: bool = True,
) -> pd.DataFrame:
    """종가만 필요한 곳(BTC 추세 필터, 백테스트)에서 쓰는 축약판: time, close 두 컬럼."""
    df = await fetch_ohlcv(session, symbol, interval, limit, closed_only)
    return df[["time", "close"]]


async def fetch_price(session: aiohttp.ClientSession, symbol: str = "BTCUSDT") -> float:
    """실시간 현재가 (마감 캔들이 아니라 지금 체결 가격)."""
    async with session.get(f"{BASE_URL}/ticker/price", params={"symbol": symbol}) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return float(data["price"])
