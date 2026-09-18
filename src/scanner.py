"""업비트 KRW 마켓 전체를 프레임 축 게이트(일봉->4시간->1시간)로 훑어 후보를 점수화한다."""

import asyncio

import aiohttp
from aiolimiter import AsyncLimiter

from src import config
from src.btc_trend import is_trend_favorable
from src.exchanges import binance_client, upbit_client
from src.scoring import CandidateResult, FrameResult, check_entry, has_volume_spike, score_frame


async def check_btc_trend(session: aiohttp.ClientSession) -> bool:
    daily = await binance_client.fetch_klines(session, config.BINANCE_SYMBOL, "1d", 100)
    return is_trend_favorable(daily["close"])


async def scan_market(
    market: str,
    session: aiohttp.ClientSession,
    limiter: AsyncLimiter,
    semaphore: asyncio.Semaphore,
) -> CandidateResult | None:
    """일봉 게이트 통과 못하면 4시간/1시간은 아예 조회하지 않는다 (호출량 절감)."""
    frames = []
    total_score = 0.0
    one_hour_df = None

    for frame in config.FRAME_ORDER:
        async with semaphore, limiter:
            candles = await upbit_client.fetch_candles(session, market, frame, count=100)

        if frame == "day" and candles["value"].iloc[-1] < config.MIN_DAILY_TRADE_VALUE_KRW:
            # 체결이 어려운 저유동성 종목은 여기서 걸러낸다 (가산점이 아니라 필터 —
            # 요인 분석 결과 거래대금 자체는 수익률과 뚜렷한 상관이 없었음)
            frames.append(FrameResult(frame=frame, passed_gate=False, score=0.0, detail={"reason": "low_liquidity"}))
            break

        result = score_frame(frame, candles["close"])
        frames.append(result)
        if not result.passed_gate:
            break  # 게이트 실패 -> 하위 프레임 조회 중단
        total_score += result.score
        if frame == "1h":
            one_hour_df = candles

    if not frames or not frames[0].passed_gate:
        return None  # 일봉부터 탈락한 종목은 후보에서 제외

    volume_bonus = False
    if one_hour_df is not None and has_volume_spike(one_hour_df):
        total_score += config.VOLUME_BONUS
        volume_bonus = True

    result = CandidateResult(market=market, total_score=total_score, frames=frames, volume_bonus=volume_bonus)

    if result.full_gate_pass:
        # 일봉/4시간/1시간을 모두 통과한 종목만 15분봉 매수 타점을 확인한다
        # (조건 유효기간이 아직 없으므로 지금은 '이번 스캔 시점에 15분 신호가 있는가'만 본다)
        async with semaphore, limiter:
            candles_15m = await upbit_client.fetch_candles(session, market, "15m", count=100)
        result.entry_ready = check_entry(candles_15m["close"])

    return result


async def scan_all(session: aiohttp.ClientSession) -> list[CandidateResult]:
    markets = await upbit_client.fetch_markets(session)
    limiter = AsyncLimiter(config.UPBIT_RATE_LIMIT_PER_SEC, 1)
    semaphore = asyncio.Semaphore(config.UPBIT_CONCURRENCY)

    tasks = [scan_market(m, session, limiter, semaphore) for m in markets]
    results = await asyncio.gather(*tasks)
    candidates = [r for r in results if r is not None]
    candidates.sort(key=lambda c: c.total_score, reverse=True)
    return candidates
