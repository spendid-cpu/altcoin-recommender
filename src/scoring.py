"""프레임 축(하드 게이트 + 누적) x 주기 축(프레임별 가산점) 점수화 로직.
설계 문서의 '점수화 로직' / '멀티타임프레임 진입 흐름' 섹션을 코드로 옮긴 것.
"""

from dataclasses import dataclass, field

import pandas as pd

from src import config
from src.indicators.stoch_rsi import stoch_rsi_all_periods


# 단기 스토캐스틱 값은 40.0처럼 딱 떨어지는 값이 많고 K와 D가 같은 값에서 만나는 일이 흔하다. 이때 계산에 쓴
# 캔들 수(이력 길이)에 따른 소수점 이하 미세 차이만으로 교차 여부가 뒤집히지 않도록 허용 오차를 둔다.
EPS = 1e-4


def _crossed_up_series(k: pd.Series, d: pd.Series) -> pd.Series:
    """각 캔들에서 직전 대비 %K가 %D를 상향 돌파했는지 (벡터 버전, 전체 이력에 대해 한 번에 계산).
    직전에 같거나 아래였고(<=) 지금 확실히 위(>)면 교차. 둘 다 EPS 허용 오차 안에서 판단한다."""
    prev_k, prev_d = k.shift(1), d.shift(1)
    return ((prev_k <= prev_d + EPS) & (k > d + EPS)).fillna(False)


def turned_up_series(k: pd.Series, d: pd.Series) -> pd.Series:
    """%K가 %D 위에 있는 상태(상승 전환)인지. 같으면 위가 아니다."""
    return (k > d + EPS).fillna(False)


def golden_cross_series(
    k: pd.Series,
    d: pd.Series,
    from_oversold_lookback: int = 5,
    threshold: int = config.OVERSOLD_THRESHOLD,
) -> pd.Series:
    """%K가 %D를 상향 돌파하면서, 최근 from_oversold_lookback개 캔들 안에 %K가 threshold 이하로
    내려갔던 적이 있어야 True. 그냥 오실레이션 중의 흔한 교차와 '바닥 찍고 반등'을 구분하기 위한
    조건이다 (과매도권 밖에서의 크로스는 무시) — 일봉/4시간/1시간 게이트에서 쓴다."""
    crossed = _crossed_up_series(k, d)
    recent_oversold = k.rolling(from_oversold_lookback).min() <= threshold + EPS
    return (crossed & recent_oversold).fillna(False)


def entry_signal_series(k: pd.Series, d: pd.Series) -> pd.Series:
    """15분봉 매수 타점 트리거: 상위 프레임(일/4시간/1시간)이 이미 과매도권 반등 맥락을 확인했으므로,
    여기서는 과매도권 필터 없이 순수 골든크로스(%K가 %D 상향 돌파)만 확인한다."""
    return _crossed_up_series(k, d)


def entry_signal(k: pd.Series, d: pd.Series) -> bool:
    series = entry_signal_series(k, d)
    return bool(series.iloc[-1]) if len(series) else False


def first_touch_series(k: pd.Series, threshold: int = config.OVERSOLD_THRESHOLD) -> pd.Series:
    """직전 캔들은 threshold 초과, 해당 캔들에서 처음 threshold 이하로 진입했는지 (벡터 버전)."""
    prev_k = k.shift(1)
    return ((prev_k > threshold + EPS) & (k <= threshold + EPS)).fillna(False)


def has_volume_spike(df: pd.DataFrame, lookback: int = config.VOLUME_LOOKBACK,
                      multiplier: float = config.VOLUME_MULTIPLIER) -> bool:
    """df는 'value'(거래대금) 컬럼을 가진 캔들 DataFrame. 최근 캔들이 최근 N개 평균의 X배 이상이면 True."""
    if len(df) < lookback + 1:
        return False
    recent = df["value"].iloc[-1]
    avg = df["value"].iloc[-(lookback + 1):-1].mean()
    if avg == 0 or pd.isna(avg):
        return False
    return recent >= avg * multiplier


@dataclass
class FrameResult:
    frame: str
    passed_gate: bool
    score: float
    detail: dict = field(default_factory=dict)


def score_frame(frame: str, close: pd.Series, validity_hours: float | None = None) -> FrameResult:
    """한 프레임(day/4h/1h)의 종가 시리즈로부터 게이트 통과 여부와 점수를 계산한다.
    게이트: 단기 스토가 validity_hours(기본: config.VALIDITY_WINDOW_HOURS) 이내에
    최초 도달 또는 골든크로스한 적이 있으면 통과 (백테스트로 확인된 '조건 유효기간').
    가산점: 골든크로스일 때 TRIGGER 보너스, 중기/장기가 이미 상승 전환(K>D) 상태면 주기 가산점.
    """
    is_low_frame = frame == config.LOW_FRAME
    if validity_hours is None:
        validity_hours = config.LOW_FRAME_LOOKBACK_HOURS if is_low_frame else config.VALIDITY_WINDOW_HOURS
    lookback_bars = max(1, round(validity_hours / config.FRAME_HOURS[frame]))

    periods = stoch_rsi_all_periods(close)
    short_k, short_d = periods["short"]["k"], periods["short"]["d"]

    is_first_touch = bool(first_touch_series(short_k).tail(lookback_bars).any())
    is_golden_cross = bool(golden_cross_series(short_k, short_d).tail(lookback_bars).any())
    # 15분봉(LOW_FRAME)은 '지금 저점권에 있는가'도 통과로 본다 (저점권에 머무는 동안에는 최초 도달이 이미 지나갔어도 저점이다)
    at_low = is_low_frame and bool(short_k.iloc[-1] <= config.OVERSOLD_THRESHOLD + EPS)
    passed = is_first_touch or is_golden_cross or at_low

    if not passed:
        return FrameResult(frame=frame, passed_gate=False, score=0.0)

    score = config.FRAME_WEIGHTS[frame]
    if is_golden_cross:
        score += config.GOLDEN_CROSS_BONUS

    period_state = {}
    for name in ("mid", "long"):
        turned_up = bool(turned_up_series(periods[name]["k"], periods[name]["d"]).iloc[-1])
        period_state[name] = turned_up
        if turned_up:
            score += config.PERIOD_BONUS_WEIGHTS[name]

    return FrameResult(
        frame=frame,
        passed_gate=True,
        score=score,
        detail={
            "first_touch": is_first_touch,
            "golden_cross": is_golden_cross,
            "at_low": at_low,
            "short_k": round(float(short_k.iloc[-1]), 2),
            "mid_turned_up": period_state["mid"],
            "long_turned_up": period_state["long"],
        },
    )


def check_entry(close: pd.Series) -> bool:
    """15분봉 종가 시리즈로부터 매수 타점(단기 스토 골든크로스) 여부를 판단한다."""
    short = stoch_rsi_all_periods(close)["short"]
    return entry_signal(short["k"], short["d"])


def score_to_grade(score: float) -> str:
    """요인 분석(scripts/run_factor_analysis.py) 점수 4분위 백테스트 결과 기준 등급.
    config.SCORE_GRADE_THRESHOLDS 이상이면 그 등급, 전부 미만이면 '-' (승률이 동전던지기 수준)."""
    for grade, threshold in sorted(config.SCORE_GRADE_THRESHOLDS.items(), key=lambda kv: -kv[1]):
        if score >= threshold:
            return grade
    return "-"


@dataclass
class CandidateResult:
    market: str
    total_score: float
    frames: list[FrameResult]
    volume_bonus: bool = False
    entry_ready: bool = False
    current_price: float = 0.0

    @property
    def cleared_frames(self) -> list[str]:
        return [f.frame for f in self.frames if f.passed_gate]

    @property
    def full_gate_pass(self) -> bool:
        """일봉/4시간/1시간 게이트를 모두 통과했는지 (15분 저점 여부와 무관)."""
        return all(f in self.cleared_frames for f in config.FRAME_ORDER)

    def breakdown(self) -> dict:
        """추천 근거: 어떤 프레임에서 왜 통과했고 어떤 항목으로 몇 점을 받았는지 (대시보드 '성과' 탭이 그대로 보여준다)."""
        frames = []
        for f in self.frames:
            if not f.passed_gate:
                continue
            d = f.detail
            if d.get("first_touch"):
                reason = "저점권(%K 20 이하) 최초 도달"
            elif d.get("golden_cross"):
                reason = "저점권에서 골든크로스"
            else:
                reason = "15분 %K가 저점권" if d.get("at_low") else "조건 통과"
            items = [{"name": "프레임 통과", "points": config.FRAME_WEIGHTS[f.frame]}]
            if d.get("golden_cross"):
                items.append({"name": "골든크로스 보너스", "points": config.GOLDEN_CROSS_BONUS})
            if d.get("mid_turned_up") and config.PERIOD_BONUS_WEIGHTS["mid"]:
                items.append({"name": "중기 상승 전환", "points": config.PERIOD_BONUS_WEIGHTS["mid"]})
            if d.get("long_turned_up") and config.PERIOD_BONUS_WEIGHTS["long"]:
                items.append({"name": "장기 상승 전환", "points": config.PERIOD_BONUS_WEIGHTS["long"]})
            frames.append({
                "frame": f.frame, "reason": reason,
                "short_k": d.get("short_k") if d.get("short_k") == d.get("short_k") else None,  # NaN(계산 불가)은 None
                "points": round(f.score, 1), "items": items,
            })
        return {
            "frames": frames,
            "total": round(self.total_score, 1),
            "grade": self.grade,
            "rule": ("일봉·4시간·1시간 조건을 모두 통과하고 15분봉이 저점일 때 추천" if config.RECOMMEND_ONLY_AT_15M_LOW
                     else "일봉 조건을 통과하면 추천"),
            "entry_ready": self.entry_ready,
        }

    @property
    def low_15m(self) -> bool:
        return config.LOW_FRAME in self.cleared_frames

    @property
    def recommendable(self) -> bool:
        """추천 대상인지. 기본은 일봉/4시간/1시간을 모두 통과하고 15분봉이 저점일 때만 True."""
        if config.RECOMMEND_ONLY_AT_15M_LOW:
            return self.cleared_frames == [*config.FRAME_ORDER, config.LOW_FRAME]
        return bool(self.cleared_frames)

    @property
    def grade(self) -> str:
        return score_to_grade(self.total_score)
