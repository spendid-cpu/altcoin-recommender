"""업비트 공개 시세 API(인증 불필요) 클라이언트."""

import asyncio

import aiohttp
import pandas as pd

BASE_URL = "https://api.upbit.com/v1"

# 프레임 이름 -> 업비트 캔들 엔드포인트 경로
_ENDPOINTS = {
    "day": "candles/days",
    "4h": "candles/minutes/240",
    "1h": "candles/minutes/60",
    "15m": "candles/minutes/15",
}

_MAX_RETRIES = 5
_MAX_PER_REQUEST = 200  # 업비트 캔들 API 1회 최대 개수


async def _get_json(session: aiohttp.ClientSession, path: str, params: dict) -> list:
    for attempt in range(_MAX_RETRIES):
        async with session.get(f"{BASE_URL}/{path}", params=params) as resp:
            if resp.status == 429:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError(f"{path} {params}: rate limit 재시도 {_MAX_RETRIES}회 초과")


async def fetch_candles(
    session: aiohttp.ClientSession, market: str, timeframe: str, count: int = 200
) -> pd.DataFrame:
    """timeframe: day / 4h / 1h / 15m. 오래된 캔들이 먼저 오도록 정렬해 반환한다.
    count가 200을 넘으면 to 파라미터로 과거로 페이지네이션한다 (백테스트용 장기 이력 조회).
    429(rate limit)는 지수 백오프로 재시도한다."""
    path = _ENDPOINTS[timeframe]
    all_rows: list[dict] = []
    to_param: str | None = None
    remaining = count

    while remaining > 0:
        batch_size = min(remaining, _MAX_PER_REQUEST)
        params = {"market": market, "count": batch_size}
        if to_param:
            params["to"] = to_param
        data = await _get_json(session, path, params)
        if not data:
            break
        all_rows.extend(data)
        remaining -= len(data)
        if len(data) < batch_size:
            break  # 더 이상 과거 데이터 없음
        to_param = data[-1]["candle_date_time_utc"]  # 다음 배치: 이 시각 이전으로

    df = pd.DataFrame(all_rows)
    if df.empty:
        return pd.DataFrame(columns=["time", "close", "value"])
    df = df.drop_duplicates(subset="candle_date_time_utc")
    df = df.sort_values("candle_date_time_utc").reset_index(drop=True)  # 오래된순 정렬
    df = df.rename(
        columns={
            "candle_date_time_kst": "time",
            "trade_price": "close",
            "candle_acc_trade_price": "value",
        }
    )
    return df[["time", "close", "value"]]


async def fetch_markets(session: aiohttp.ClientSession) -> list[str]:
    """KRW 마켓 코드 목록만 반환한다 (예: KRW-BTC, KRW-ETH ...)."""
    async with session.get(f"{BASE_URL}/market/all") as resp:
        resp.raise_for_status()
        data = await resp.json()
    return [m["market"] for m in data if m["market"].startswith("KRW-")]


async def fetch_ticker_prices(session: aiohttp.ClientSession, markets: list[str]) -> dict[str, float]:
    """여러 마켓의 현재가를 한 번에 조회한다 (가격 추적용). 빈 목록이면 빈 dict 반환."""
    if not markets:
        return {}
    params = {"markets": ",".join(markets)}
    data = await _get_json(session, "ticker", params)
    return {row["market"]: float(row["trade_price"]) for row in data}
