"""업비트 KRW 마켓 전체를 프레임 축 게이트(일봉->4시간->1시간->15분)로 훑어 후보를 점수화한다."""

import asyncio

import aiohttp
from aiolimiter import AsyncLimiter

from src import config
from src.btc_trend import days_above_ma, is_trend_favorable
from src.exchanges import binance_client, upbit_client
from src.scoring import CandidateResult, FrameResult, check_entry, has_volume_spike, find_support_lows, max_runup_pct, score_frame

# 업비트 1회 요청 최대치. RSI는 지수이동평균이라 앞쪽 이력이 짧으면 값이 조금씩 달라지므로 충분히 길게 받는다
# (백테스트가 쓰는 이력 길이 200 이상과 맞춘다).
LOOKBACK_CANDLES = 200


async def check_btc_trend(session: aiohttp.ClientSession) -> bool:
    daily = await binance_client.fetch_klines(session, config.BINANCE_SYMBOL, "1d", 100)
    return is_trend_favorable(daily["close"])


async def btc_days_above(session: aiohttp.ClientSession) -> int:
    """비트코인 일봉 종가가 MA20 위였던 연속 일수 (최초 알고리즘은 2일 이상을 요구한다)."""
    daily = await binance_client.fetch_klines(session, config.BINANCE_SYMBOL, "1d", 100)
    return days_above_ma(daily["close"])


async def scan_market(
    market: str,
    session: aiohttp.ClientSession,
    limiter: AsyncLimiter,
    semaphore: asyncio.Semaphore,
    cache: dict | None = None,
) -> CandidateResult | None:
    """일봉 게이트 통과 못하면 4시간/1시간은 아예 조회하지 않는다 (호출량 절감).
    cache를 주면 받은 캔들을 (종목, 프레임) 키로 넣어 둔다 — 같은 스캔에서 사이클 전략이 재사용한다."""
    frames = []
    total_score = 0.0
    one_hour_df = None
    latest_price = 0.0

    for frame in config.FRAME_ORDER:
        async with semaphore, limiter:
            candles = await upbit_client.fetch_candles(session, market, frame, count=LOOKBACK_CANDLES)
        if candles.empty:
            break  # 방금 상장돼 마감된 캔들이 아직 없는 경우 등
        if cache is not None:
            cache[(market, frame)] = candles
        latest_price = float(candles["close"].iloc[-1])

        if frame == "day" and candles["value"].iloc[-1] < config.MIN_DAILY_TRADE_VALUE_KRW:
            # 체결이 어려운 저유동성 종목은 여기서 걸러낸다 (가산점이 아니라 필터 —
            # 요인 분석 결과 거래대금 자체는 수익률과 뚜렷한 상관이 없었음)
            frames.append(FrameResult(frame=frame, passed_gate=False, score=0.0, detail={"reason": "low_liquidity"}))
            break

        require_regime = config.DIRECTION_FILTER_ENABLED and frame == config.DIRECTION_FILTER_FRAME
        result = score_frame(frame, candles["close"], require_rising_regime=require_regime)
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

    # 진입 직전 급등 이력: 1시간 마감 종가로 최근 RUNUP_LOOKBACK_HOURS시간의 최대 상승폭을 잰다
    runup = max_runup_pct(one_hour_df["close"].tail(config.RUNUP_LOOKBACK_HOURS)) if one_hour_df is not None else 0.0
    result = CandidateResult(
        market=market, total_score=total_score, frames=frames, volume_bonus=volume_bonus, current_price=latest_price,
        recent_runup_pct=runup,
        support_lows=find_support_lows(one_hour_df["close"].tail(config.PAPER_SUPPORT_WINDOW_HOURS)) if one_hour_df is not None else [],
    )

    if result.full_gate_pass:
        # 일봉/4시간/1시간/15분을 모두 통과한 종목만 LOW_FRAME(5분봉)을 본다. LOW_FRAME은 마지막 관문(저점인가)이면서
        # 점수에도 들어간다. entry_ready는 그 위에서 '이번 캔들에 골든크로스가 났는가'를 따로 보는 표시다.
        async with semaphore, limiter:
            candles_low = await upbit_client.fetch_candles(session, market, config.LOW_FRAME, count=LOOKBACK_CANDLES)
        if not candles_low.empty:
            low = score_frame(config.LOW_FRAME, candles_low["close"])
            result.frames.append(low)
            if low.passed_gate:
                result.total_score += low.score
            result.entry_ready = check_entry(candles_low["close"])
            result.current_price = float(candles_low["close"].iloc[-1])  # 더 최신 가격으로 갱신

    return result


async def _scan_market_safe(
    market: str, session: aiohttp.ClientSession, limiter: AsyncLimiter, semaphore: asyncio.Semaphore,
    cache: dict | None = None,
) -> CandidateResult | None:
    """한 종목의 조회 실패(429 재시도 초과, 일시적 네트워크 오류 등) 때문에 스캔 전체가 죽지 않게 한다."""
    try:
        return await scan_market(market, session, limiter, semaphore, cache)
    except Exception as exc:
        print(f"  {market} 스캔 실패(이번 사이클은 건너뜀): {exc}")
        return None


async def scan_all(
    session: aiohttp.ClientSession, markets: list[str] | None = None, cache: dict | None = None
) -> list[CandidateResult]:
    if markets is None:
        markets = await upbit_client.fetch_markets(session)
    limiter = AsyncLimiter(config.UPBIT_RATE_LIMIT_PER_SEC, 1)
    semaphore = asyncio.Semaphore(config.UPBIT_CONCURRENCY)

    tasks = [_scan_market_safe(m, session, limiter, semaphore, cache) for m in markets]
    results = await asyncio.gather(*tasks)
    candidates = [r for r in results if r is not None and r.market not in config.NO_RECOMMEND_MARKETS]
    candidates.sort(key=lambda c: c.total_score, reverse=True)

    # 캔들은 마감된 것만 쓰기 때문에 마지막 종가가 최대 하루 전 값이다. 발굴가·현재가는 실시간 시세로 채운다.
    if candidates:
        prices = await upbit_client.fetch_ticker_prices(session, [c.market for c in candidates])
        for c in candidates:
            c.current_price = prices.get(c.market, c.current_price)
    return candidates
