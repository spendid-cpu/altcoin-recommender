"""추천 종료(익절·손절·되돌림·신호 소멸) 규칙 백테스트.

실제 스캐너가 추천을 내는 시점(점수가 기준 이상으로 새로 올라온 첫 15분 캔들)을 진입으로 보고,
이후 3일(추적 기간) 동안의 15분 종가 경로에 종료 규칙을 적용해 결과를 비교한다.
점수는 backtest.score_series로 실제 스캐너와 똑같이 재현하고, 모든 정보는 마감된 캔들 기준이다.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src import price_tracker
from src.backtest import MarketSignals, btc_filter_asof, score_series

BARS_PER_DAY = 96
HORIZON_BARS = price_tracker.TRACK_DAYS * BARS_PER_DAY  # 추적 기간 (3일)


@dataclass
class Entry:
    market: str
    time: pd.Timestamp
    price: float
    score: float
    path_ret: np.ndarray  # 진입 후 k=1..HORIZON 15분 종가의 진입가 대비 수익률(%)
    path_score: np.ndarray  # 같은 시점들의 점수


def find_entries(
    signals: MarketSignals,
    threshold: float,
    btc_df: pd.DataFrame | None = None,
    cooldown_bars: int = HORIZON_BARS,
) -> list[Entry]:
    """점수가 threshold 미만이다가 이상으로 올라온 첫 15분 캔들을 추천 시점으로 본다. 한 종목은 추적 기간 동안
    다시 추천하지 않고(cooldown), 앞으로 3일치 가격이 다 있는 진입만 쓴다(경로가 잘린 진입은 편향을 만든다)."""
    # 점수/BTC 필터 계산은 무거워서 종목당 한 번만 하고 signals 객체에 붙여 재사용한다
    if not hasattr(signals, "_score_np"):
        signals._score_np = score_series(signals).to_numpy()
    score = signals._score_np
    close = signals.close15.to_numpy(dtype=float)
    n = len(score)
    ok = score >= threshold
    if btc_df is not None:
        if not hasattr(signals, "_btc_np"):
            signals._btc_np = btc_filter_asof(signals.times15, btc_df).to_numpy()
        ok = ok & signals._btc_np
    fresh = ok & ~np.concatenate([[False], ok[:-1]])

    entries: list[Entry] = []
    last = -10**9
    for i in np.flatnonzero(fresh):
        if i - last < cooldown_bars:
            continue
        if i + HORIZON_BARS >= n:
            continue
        last = i
        price = close[i]
        entries.append(Entry(
            market=signals.market, time=signals.times15.iloc[i], price=price, score=float(score[i]),
            path_ret=(close[i + 1:i + 1 + HORIZON_BARS] / price - 1) * 100,
            path_score=score[i + 1:i + 1 + HORIZON_BARS],
        ))
    return entries


@dataclass(frozen=True)
class Rule:
    name: str
    time_bars: int = HORIZON_BARS  # 시간 만료
    take_profit: float | None = None  # +A% 도달 시 종료
    stop_loss: float | None = None  # -C% 도달 시 종료
    trail_arm: float | None = None  # 최고 수익이 +A%를 넘으면 되돌림 감시 시작
    trail_dd: float | None = None  # 고점 대비 -B% 되돌리면 종료
    score_drop: float | None = None  # 점수가 진입 때보다 D 이상 내려가면 종료
    gate_lost: bool = False  # 점수가 0이 되면(일봉 조건 이탈) 종료
    extra: dict = field(default_factory=dict, compare=False)


def simulate(entry: Entry, rule: Rule) -> tuple[float, int, str]:
    """(종료 수익률 %, 보유 15분봉 수, 종료 사유). 15분 종가로만 판단하므로 봉 안의 고저는 반영하지 않는다."""
    T = min(rule.time_bars, len(entry.path_ret))
    r = entry.path_ret[:T]
    s = entry.path_score[:T]
    hits: list[tuple[int, int, str]] = []  # (봉 인덱스, 같은 봉에서의 우선순위, 사유)

    def first(mask: np.ndarray, priority: int, label: str) -> None:
        idx = np.flatnonzero(mask)
        if idx.size:
            hits.append((int(idx[0]), priority, label))

    if rule.stop_loss is not None:
        first(r <= -rule.stop_loss, 0, "손절")
    if rule.trail_dd is not None and rule.trail_arm is not None:
        peak = np.maximum.accumulate(np.maximum(r, 0.0))
        armed = peak >= rule.trail_arm
        drawdown = ((1 + r / 100) / (1 + peak / 100) - 1) * 100
        first(armed & (drawdown <= -rule.trail_dd), 1, "되돌림")
    if rule.take_profit is not None:
        first(r >= rule.take_profit, 2, "익절")
    if rule.score_drop is not None:
        first(s <= entry.score - rule.score_drop, 3, "점수하락")
    if rule.gate_lost:
        first(s <= 0, 4, "조건이탈")

    if hits:
        k, _, label = min(hits)
        return float(r[k]), k + 1, label
    return float(r[T - 1]), T, "시간만료"


def bootstrap_ci(values: np.ndarray, iterations: int = 2000, seed: int = 11) -> tuple[float, float]:
    """평균의 95% 신뢰구간 (표본을 다시 뽑는 부트스트랩)."""
    if len(values) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = rng.choice(values, size=(iterations, len(values)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def evaluate(entries: list[Entry], rule: Rule, baseline: Rule | None = None) -> dict:
    results = [simulate(e, rule) for e in entries]
    rets = np.array([r[0] for r in results])
    holds = np.array([r[1] for r in results])
    lo, hi = bootstrap_ci(rets)
    out = {
        "rule": rule.name, "n": len(rets), "mean": rets.mean(), "ci_lo": lo, "ci_hi": hi,
        "median": float(np.median(rets)), "win": float((rets > 0).mean() * 100),
        "hold_h": holds.mean() * 0.25, "p10": float(np.percentile(rets, 10)), "worst": rets.min(),
        "reasons": pd.Series([r[2] for r in results]).value_counts().to_dict(),
    }
    if baseline is not None:
        base = np.array([simulate(e, baseline)[0] for e in entries])
        diff = rets - base
        d_lo, d_hi = bootstrap_ci(diff)
        out.update({"vs_base": diff.mean(), "vs_lo": d_lo, "vs_hi": d_hi})
    return out
