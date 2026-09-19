"""과거 데이터로 게이트+진입 로직을 재생해 신호별 이후 수익률을 계산한다.
설계 문서의 '조건 유효기간'(일봉/4시간/1시간 게이트가 얼마나 오래 15분 진입의 근거로 유효한가)을
검증하기 위한 백테스트. 데이터는 종목당 한 번만 받아오고, 유효기간 값만 바꿔가며 여러 번 비교할 수 있다.
"""

import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pandas as pd

from src import config
from src.exchanges import upbit_client
from src.indicators.stoch_rsi import stoch_rsi_all_periods
from src.scoring import entry_signal_series, first_touch_series, golden_cross_series, turned_up_series


def entry_series(close: pd.Series) -> pd.Series:
    """15분봉 진입 트리거: 순수 골든크로스 (scoring.entry_signal과 동일 조건)."""
    short = stoch_rsi_all_periods(close)["short"]
    return entry_signal_series(short["k"], short["d"])


def _active_mask(times15: pd.Series, gate_times: pd.Series, validity: pd.Timedelta) -> pd.Series:
    """times15의 각 시점에 대해, validity 기간 내에 gate_times 중 하나라도 있었으면 True."""
    if gate_times.empty:
        return pd.Series(False, index=times15.index)
    left = pd.DataFrame({"time": times15.values}, index=times15.index).sort_values("time")
    right = pd.DataFrame({"time": gate_times.sort_values().values, "gate_time": gate_times.sort_values().values})
    merged = pd.merge_asof(left, right, on="time", direction="backward")
    active = ((merged["time"] - merged["gate_time"]) <= validity).fillna(False)
    active.index = left.index
    return active.reindex(times15.index).fillna(False)


def _asof_series(times15: pd.Series, ref_times: pd.Series, ref_values: pd.Series, default=False) -> pd.Series:
    """times15의 각 시점에 대해, ref_times 중 그 시점 이전 가장 최근 값을 가져온다 (미래 정보 누출 방지)."""
    if ref_times.empty:
        return pd.Series(default, index=times15.index)
    left = pd.DataFrame({"time": times15.values}, index=times15.index).sort_values("time")
    right = pd.DataFrame({"time": ref_times.values, "value": ref_values.values}).sort_values("time")
    merged = pd.merge_asof(left, right, on="time", direction="backward")
    result = merged["value"]
    result.index = left.index
    return result.reindex(times15.index).fillna(default)


@dataclass
class FrameFactors:
    gate_times: pd.Series  # 최초도달 또는 골든크로스 (게이트용)
    golden_cross_times: pd.Series  # 골든크로스만 (보너스 자격용)
    times: pd.Series
    mid_turned_up: pd.Series
    long_turned_up: pd.Series
    passed_window: pd.Series  # 최근 lookback개 캔들 안에 게이트 이벤트가 있었나 (실제 score_frame과 같은 규칙)
    gc_window: pd.Series  # 최근 lookback개 캔들 안에 골든크로스가 있었나


def _compute_frame_factors(df: pd.DataFrame, frame: str, validity_hours: float) -> FrameFactors:
    close = df["close"]
    periods = stoch_rsi_all_periods(close)
    short_k, short_d = periods["short"]["k"], periods["short"]["d"]
    ft = first_touch_series(short_k)
    gc = golden_cross_series(short_k, short_d)
    mid_up = turned_up_series(periods["mid"]["k"], periods["mid"]["d"])
    long_up = turned_up_series(periods["long"]["k"], periods["long"]["d"])
    lookback = max(1, round(validity_hours / config.FRAME_HOURS[frame]))
    gate = (ft | gc).fillna(False)
    return FrameFactors(
        gate_times=df["time"][gate],
        golden_cross_times=df["time"][gc],
        times=df["time"],
        mid_turned_up=mid_up,
        long_turned_up=long_up,
        passed_window=gate.astype(float).rolling(lookback, min_periods=1).max().astype(bool),
        gc_window=gc.astype(float).rolling(lookback, min_periods=1).max().astype(bool),
    )


@dataclass
class MarketSignals:
    market: str
    times15: pd.Series
    close15: pd.Series
    day: FrameFactors
    h4: FrameFactors
    h1: FrameFactors
    h1_volume_ratio_times: pd.Series
    h1_volume_ratio: pd.Series
    day_trade_value: pd.Series  # 일봉 거래대금(원화) — 유동성/거래대금 규모 팩터
    m15_entry: pd.Series
    ret_4h: pd.Series
    ret_1d: pd.Series
    ret_3d: pd.Series

    @property
    def day_gate_times(self) -> pd.Series:
        return self.day.gate_times

    @property
    def h4_gate_times(self) -> pd.Series:
        return self.h4.gate_times

    @property
    def h1_gate_times(self) -> pd.Series:
        return self.h1.gate_times


@dataclass
class Trade:
    market: str
    entry_time: pd.Timestamp
    entry_price: float
    ret_4h: float | None
    ret_1d: float | None
    ret_3d: float | None


CACHE_DIR = Path(__file__).resolve().parent.parent / "backtest_cache"
CACHE_TTL_HOURS = 12


async def load_raw_frames(
    session: aiohttp.ClientSession,
    market: str,
    day_count: int = 200,
    h4_count: int = 200,
    h1_count: int = 500,
    m15_count: int = 2000,
) -> dict[str, pd.DataFrame] | None:
    """종목 하나의 마감된 캔들을 프레임별로 받아온다. 백테스트를 여러 번 돌려도 API를 다시 안 부르도록
    디스크에 캐시한다(CACHE_TTL_HOURS 동안 재사용). 비어 있는 프레임이 있으면 None."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{market}_{day_count}_{h4_count}_{h1_count}_{m15_count}.pkl"
    if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_HOURS * 3600:
        return pd.read_pickle(path)

    frames = {
        "day": await upbit_client.fetch_candles(session, market, "day", day_count),
        "4h": await upbit_client.fetch_candles(session, market, "4h", h4_count),
        "1h": await upbit_client.fetch_candles(session, market, "1h", h1_count),
        "15m": await upbit_client.fetch_candles(session, market, "15m", m15_count),
    }
    result = None if any(df.empty for df in frames.values()) else frames
    pd.to_pickle(result, path)
    return result


async def load_market_signals(
    session: aiohttp.ClientSession, market: str, use_close_time: bool = True, **counts
) -> MarketSignals | None:
    frames = await load_raw_frames(session, market, **counts)
    return build_market_signals(market, frames, use_close_time) if frames else None


def build_market_signals(
    market: str,
    frames: dict[str, pd.DataFrame],
    use_close_time: bool = True,
    validity_hours: float = config.VALIDITY_WINDOW_HOURS,
) -> MarketSignals:
    """캔들 -> 게이트/진입/이후 수익률 시리즈 (validity_window을 여러 값으로 바꿔가며 재사용하려고 조회와 분리).

    use_close_time=True(기본, 올바른 방식): 각 캔들의 시각을 '마감 시각'(시작 + 길이)으로 둔다. 캔들의 종가로
    계산한 신호는 마감돼야 알 수 있으므로, 어느 시점에 쓸 수 있는 신호는 그 시점까지 마감된 캔들 것뿐이다.
    use_close_time=False: 업비트가 주는 '시작 시각' 그대로 쓴다. 이러면 아직 진행 중인 일봉/4시간봉/1시간봉의
    최종 종가를 미리 아는 셈이 되어(미래 정보 누출) 결과가 부풀려진다 — 이전 백테스트가 이 방식이었다.
    비교용으로만 남겨둔다."""
    day_df, h4_df, h1_df, m15_df = (frames[k].copy() for k in ("day", "4h", "1h", "15m"))
    for df, frame in ((day_df, "day"), (h4_df, "4h"), (h1_df, "1h"), (m15_df, "15m")):
        df["time"] = pd.to_datetime(df["time"])
        if use_close_time:
            df["time"] = df["time"] + upbit_client.FRAME_DELTA[frame]

    close15 = m15_df["close"]
    volume_avg = h1_df["value"].shift(1).rolling(config.VOLUME_LOOKBACK).mean()
    volume_ratio = (h1_df["value"] / volume_avg).replace([float("inf"), -float("inf")], None)

    return MarketSignals(
        market=market,
        times15=m15_df["time"],
        close15=close15,
        day=_compute_frame_factors(day_df, "day", validity_hours),
        h4=_compute_frame_factors(h4_df, "4h", validity_hours),
        h1=_compute_frame_factors(h1_df, "1h", validity_hours),
        h1_volume_ratio_times=h1_df["time"],
        h1_volume_ratio=volume_ratio,
        day_trade_value=day_df["value"],
        m15_entry=entry_series(close15),
        ret_4h=(close15.shift(-16) / close15 - 1) * 100,
        ret_1d=(close15.shift(-96) / close15 - 1) * 100,
        ret_3d=(close15.shift(-288) / close15 - 1) * 100,
    )


def score_series(signals: MarketSignals) -> pd.Series:
    """각 15분 결정 시각의 점수를 실제 스캐너(scoring.score_frame + scanner.scan_market)와 같은 규칙으로 계산한다.
    일봉 -> 4시간 -> 1시간 순으로 게이트를 통과한 프레임만 이어서 점수를 더하고(앞 프레임이 탈락하면 거기서 끝),
    프레임마다 '프레임 가중치 + 골든크로스 보너스 + 중기/장기 전환 보너스'를 준다. 최소 유동성 필터도 같다.
    모든 값은 그 시각까지 마감된 캔들 기준이다."""
    t = signals.times15

    def frame_score(frame_key: str, ff: FrameFactors) -> tuple[pd.Series, pd.Series]:
        asof = lambda series: _asof_series(t, ff.times, series, default=False).astype(bool)  # noqa: E731
        passed = asof(ff.passed_window)
        bonus = (
            config.FRAME_WEIGHTS[frame_key]
            + asof(ff.gc_window) * config.GOLDEN_CROSS_BONUS
            + asof(ff.mid_turned_up) * config.PERIOD_BONUS_WEIGHTS["mid"]
            + asof(ff.long_turned_up) * config.PERIOD_BONUS_WEIGHTS["long"]
        )
        return passed, passed * bonus

    day_passed, day_score = frame_score("day", signals.day)
    h4_passed, h4_score = frame_score("4h", signals.h4)
    _, h1_score = frame_score("1h", signals.h1)
    score = day_score + day_passed * (h4_score + h4_passed * h1_score)
    liquid = _asof_series(t, signals.day.times, signals.day_trade_value, default=0.0) >= config.MIN_DAILY_TRADE_VALUE_KRW
    return (score * liquid).astype(float)


def trades_for_validity(signals: MarketSignals, validity_window_hours: float) -> list[Trade]:
    """미리 계산된 신호에 유효기간만 적용해 Trade 목록을 만든다 (재조회 없음)."""
    validity = pd.Timedelta(hours=validity_window_hours)
    active = (
        _active_mask(signals.times15, signals.day_gate_times, validity)
        & _active_mask(signals.times15, signals.h4_gate_times, validity)
        & _active_mask(signals.times15, signals.h1_gate_times, validity)
        & signals.m15_entry
    )

    trades = []
    for i in active[active].index:
        trades.append(
            Trade(
                market=signals.market,
                entry_time=signals.times15.iloc[i],
                entry_price=signals.close15.iloc[i],
                ret_4h=None if pd.isna(signals.ret_4h.iloc[i]) else float(signals.ret_4h.iloc[i]),
                ret_1d=None if pd.isna(signals.ret_1d.iloc[i]) else float(signals.ret_1d.iloc[i]),
                ret_3d=None if pd.isna(signals.ret_3d.iloc[i]) else float(signals.ret_3d.iloc[i]),
            )
        )
    return trades


def btc_filter_asof(
    times15: pd.Series, btc_df: pd.DataFrame, ma_period: int = 20, hold_days: int = config.BTC_HOLD_DAYS
) -> pd.Series:
    """각 15분 결정 시각에, 그 시각까지 마감된 BTC 일봉 기준으로 추천 필터(MA20 위 hold_days일 연속)가
    켜져 있었는지. btc_df는 binance_client.fetch_klines 결과(time = UTC 마감 시각)."""
    above = (btc_df["close"] > btc_df["close"].rolling(ma_period).mean()).astype(float)
    on = above.rolling(hold_days).min().eq(1.0)
    # 바이낸스 일봉은 UTC 00시에 마감 = 한국시간 09시 (업비트 시각은 KST 기준이라 맞춰준다)
    available_at = btc_df["time"] + pd.Timedelta(milliseconds=1) + pd.Timedelta(hours=9)
    return _asof_series(times15, available_at.reset_index(drop=True), on.reset_index(drop=True), default=False).astype(bool)


def trades_with_factors(
    signals: MarketSignals,
    validity_window_hours: float = config.VALIDITY_WINDOW_HOURS,
    btc_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """게이트 통과 + 15분 진입 시점마다 스코어 구성요소(프레임별 골든크로스 보너스 자격,
    중기/장기 전환 상태, 거래량 배수)를 그 시점 기준(미래 정보 없이)으로 계산해 DataFrame으로 반환한다.
    가중치/거래량 배수 값이 실제 수익률과 상관이 있는지 분석하는 용도 (설계 문서: 백테스트 및 검증 계획).
    day_value_ok는 실제 스캐너의 최소 유동성 필터, btc_ok는 BTC 추세 필터를 그 시점에 통과했는지다."""
    validity = pd.Timedelta(hours=validity_window_hours)
    active = (
        _active_mask(signals.times15, signals.day.gate_times, validity)
        & _active_mask(signals.times15, signals.h4.gate_times, validity)
        & _active_mask(signals.times15, signals.h1.gate_times, validity)
        & signals.m15_entry
    )
    idx = active[active].index
    if len(idx) == 0:
        return pd.DataFrame()

    times15 = signals.times15

    def gc_bonus(frame: FrameFactors) -> pd.Series:
        return _active_mask(times15, frame.golden_cross_times, validity)

    def state_asof(frame: FrameFactors, series: pd.Series) -> pd.Series:
        return _asof_series(times15, frame.times, series, default=False)

    rows = pd.DataFrame(
        {
            "market": signals.market,
            "entry_time": times15,
            "day_golden_cross": gc_bonus(signals.day),
            "h4_golden_cross": gc_bonus(signals.h4),
            "h1_golden_cross": gc_bonus(signals.h1),
            "day_mid_up": state_asof(signals.day, signals.day.mid_turned_up),
            "day_long_up": state_asof(signals.day, signals.day.long_turned_up),
            "h4_mid_up": state_asof(signals.h4, signals.h4.mid_turned_up),
            "h4_long_up": state_asof(signals.h4, signals.h4.long_turned_up),
            "h1_mid_up": state_asof(signals.h1, signals.h1.mid_turned_up),
            "h1_long_up": state_asof(signals.h1, signals.h1.long_turned_up),
            "volume_ratio": _asof_series(times15, signals.h1_volume_ratio_times, signals.h1_volume_ratio, default=0.0),
            "day_trade_value": _asof_series(times15, signals.day.times, signals.day_trade_value, default=0.0),
            "btc_ok": btc_filter_asof(times15, btc_df) if btc_df is not None else True,
            "ret_4h": signals.ret_4h,
            "ret_1d": signals.ret_1d,
            "ret_3d": signals.ret_3d,
        }
    )
    df = rows.loc[idx].reset_index(drop=True)
    df["day_value_ok"] = df["day_trade_value"] >= config.MIN_DAILY_TRADE_VALUE_KRW
    bool_cols = [
        "day_golden_cross", "h4_golden_cross", "h1_golden_cross",
        "day_mid_up", "day_long_up", "h4_mid_up", "h4_long_up", "h1_mid_up", "h1_long_up", "btc_ok",
    ]
    for c in bool_cols:
        # merge_asof는 매칭 전 구간에 NaN을 남겨 dtype이 object/float로 바뀔 수 있어 bool로 강제 고정
        # (안 하면 ~df[col]이 논리부정이 아니라 정수 비트반전으로 계산되는 버그가 생김)
        df[c] = df[c].astype(bool)

    df["score"] = (
        config.FRAME_WEIGHTS["day"] + config.FRAME_WEIGHTS["4h"] + config.FRAME_WEIGHTS["1h"]
        + df["day_golden_cross"] * config.GOLDEN_CROSS_BONUS
        + df["h4_golden_cross"] * config.GOLDEN_CROSS_BONUS
        + df["h1_golden_cross"] * config.GOLDEN_CROSS_BONUS
        + df["day_mid_up"] * config.PERIOD_BONUS_WEIGHTS["mid"]
        + df["day_long_up"] * config.PERIOD_BONUS_WEIGHTS["long"]
        + df["h4_mid_up"] * config.PERIOD_BONUS_WEIGHTS["mid"]
        + df["h4_long_up"] * config.PERIOD_BONUS_WEIGHTS["long"]
        + df["h1_mid_up"] * config.PERIOD_BONUS_WEIGHTS["mid"]
        + df["h1_long_up"] * config.PERIOD_BONUS_WEIGHTS["long"]
        + (df["volume_ratio"] >= config.VOLUME_MULTIPLIER) * config.VOLUME_BONUS
    )
    return df


def summarize(trades: list[Trade]) -> dict:
    def stats(returns: list[float]) -> dict:
        if not returns:
            return {"n": 0, "win_rate": None, "avg_return": None}
        wins = sum(1 for r in returns if r > 0)
        return {
            "n": len(returns),
            "win_rate": round(wins / len(returns) * 100, 1),
            "avg_return": round(sum(returns) / len(returns), 2),
        }

    return {
        "trade_count": len(trades),
        "4h": stats([t.ret_4h for t in trades if t.ret_4h is not None]),
        "1d": stats([t.ret_1d for t in trades if t.ret_1d is not None]),
        "3d": stats([t.ret_3d for t in trades if t.ret_3d is not None]),
    }
