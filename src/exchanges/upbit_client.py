"""업비트 공개 시세 API(인증 불필요) 클라이언트."""

import asyncio

import aiohttp
import pandas as pd
from aiolimiter import AsyncLimiter

from src import config

BASE_URL = "https://api.upbit.com/v1"

# 프레임 이름 -> 업비트 캔들 엔드포인트 경로
_ENDPOINTS = {
    "day": "candles/days",
    "4h": "candles/minutes/240",
    "1h": "candles/minutes/60",
    "15m": "candles/minutes/15",
    "5m": "candles/minutes/5",
}

# 프레임 이름 -> 캔들 1개의 길이 (마감 여부 판단, 백테스트에서 '정보가 확정되는 시각' 계산에 쓴다)
FRAME_DELTA = {
    "day": pd.Timedelta(days=1),
    "4h": pd.Timedelta(hours=4),
    "1h": pd.Timedelta(hours=1),
    "15m": pd.Timedelta(minutes=15),
    "5m": pd.Timedelta(minutes=5),
}

_MAX_RETRIES = 5
_MAX_PER_REQUEST = 200  # 업비트 캔들 API 1회 최대 개수

# 모든 요청에 걸리는 전역 속도 제한 (기본 꺼짐). 한 종목이 캔들을 수십 번 이어서 받는 백테스트처럼, 호출하는 쪽의 동시성 제한만으로는
# 초당 요청 수를 못 지키는 경우에 켠다.
_throttle: AsyncLimiter | None = None


def set_throttle(requests_per_second: float | None) -> None:
    global _throttle
    _throttle = AsyncLimiter(requests_per_second, 1) if requests_per_second else None


async def _get_json(session: aiohttp.ClientSession, path: str, params: dict) -> list:
    for attempt in range(_MAX_RETRIES):
        if _throttle is not None:
            await _throttle.acquire()
        async with session.get(f"{BASE_URL}/{path}", params=params) as resp:
            if resp.status == 429:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError(f"{path} {params}: rate limit 재시도 {_MAX_RETRIES}회 초과")


async def fetch_candles(
    session: aiohttp.ClientSession, market: str, timeframe: str, count: int = 200, closed_only: bool = True
) -> pd.DataFrame:
    """timeframe: day / 4h / 1h / 15m. 오래된 캔들이 먼저 오도록 정렬해 반환한다.
    count가 200을 넘으면 to 파라미터로 과거로 페이지네이션한다 (백테스트용 장기 이력 조회).
    429(rate limit)는 지수 백오프로 재시도한다.

    closed_only=True(기본)면 아직 마감되지 않은 마지막 캔들은 버린다. 진행 중인 캔들은 값이 계속 바뀌어서
    (repainting) 스토캐스틱 신호와 점수가 스캔마다 흔들리기 때문이다 — 설계 문서의 '캔들 마감 확정 원칙'.
    현재가가 필요하면 fetch_ticker_prices를 쓴다."""
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
    if closed_only:
        # candle_date_time_utc는 캔들 '시작' 시각(UTC). 시작 + 길이가 지금보다 뒤면 아직 진행 중이다.
        ends = pd.to_datetime(df["candle_date_time_utc"]) + FRAME_DELTA[timeframe]
        df = df[ends <= pd.Timestamp.now(tz="UTC").tz_localize(None)].reset_index(drop=True)
    df = df.rename(
        columns={
            "candle_date_time_kst": "time",
            "trade_price": "close",
            "candle_acc_trade_price": "value",
        }
    )
    return df[["time", "close", "value"]]


async def fetch_markets(session: aiohttp.ClientSession) -> list[str]:
    """KRW 마켓 코드 목록만 반환한다 (예: KRW-BTC, KRW-ETH ...). 스테이블코인(config.EXCLUDED_MARKETS)은 뺀다 —
    이 목록을 두 전략(scan_all/original_scanner)이 공유하므로 여기서 한 번만 걸러내면 전체에 적용된다."""
    async with session.get(f"{BASE_URL}/market/all") as resp:
        resp.raise_for_status()
        data = await resp.json()
    return [m["market"] for m in data if m["market"].startswith("KRW-") and m["market"] not in config.EXCLUDED_MARKETS]


async def _ticker_batch(session: aiohttp.ClientSession, markets: list[str]) -> dict[str, float]:
    data = await _get_json(session, "ticker", {"markets": ",".join(markets)})
    return {row["market"]: float(row["trade_price"]) for row in data}


async def fetch_ticker_prices(session: aiohttp.ClientSession, markets: list[str]) -> dict[str, float]:
    """여러 마켓의 현재가를 한 번에 조회한다 (가격 추적용). 빈 목록이면 빈 dict 반환.
    한 번에 묶어서 조회하다 실패하면(그중 한 종목이 상장폐지됐거나 코드가 잘못돼 업비트가 요청 전체를
    오류로 응답하는 경우 등) 종목별로 나눠 다시 시도한다 — 이미 추적 중인 종목 여러 개가 문제 있는 종목
    하나 때문에 이번 사이클에서 통째로 처리되지 못하면 안 되기 때문이다(익절/손절 판단이 밀린다)."""
    if not markets:
        return {}
    try:
        return await _ticker_batch(session, markets)
    except Exception as exc:
        print(f"  시세 일괄 조회 실패({exc!r}), 종목별로 다시 시도합니다")
    prices: dict[str, float] = {}
    for market in markets:
        try:
            prices.update(await _ticker_batch(session, [market]))
        except Exception as exc:
            print(f"  {market} 시세 조회 실패(이번 사이클은 건너뜀): {exc!r}")
    return prices
