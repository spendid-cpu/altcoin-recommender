"""사이클 전략(사용자 판단 방식) 신호 계산. 백테스트(65일)에서 확정한 규칙 v0.2를 그대로 옮긴 것이다.

모든 판단은 마감된 캔들 기준이고 스토캐스틱 RSI는 사용자의 트레이딩뷰 설정(K3·D3·RSI5·스토5 / 6,6,10,10 / 12,12,20,20,
stoch_rsi.PERIOD_SETS)을 쓴다. 함수는 캔들 DataFrame(time, close, value)을 받아 봉마다의 True/False 배열을 돌려주고,
스캔에서는 마지막 값만, 백테스트/검증에서는 전체 배열을 쓴다.

용어
  - 바닥 구간: K 또는 D가 20 이하 / 고점 구간: K 또는 D가 80 이상
  - 바닥 골든크로스: 골든크로스가 났고, 그 봉 포함 최근 6봉 안에 바닥 구간이 있었음
  - 상승 체제(중기/장기): 마지막 바닥 골든크로스가 마지막 데드크로스보다 뒤
  - 바닥 턴 직전: K<D, D-K<=5, K가 2봉 연속 상승, 최근 6봉 안에 바닥 구간
  - 거래량 폭발: 그 봉 거래대금 >= 직전 20개 봉 평균의 2배
진입: 일봉 단기 OK + 4시간 상승체제(중기·장기)·여력 + 4시간 준비 + 고점 제외 + 1시간 바닥 턴 (+ 1시간 거래량 = B급, + 지지 터치 = A급)
매도: 4시간 단기 K>=80 이고 그 봉 거래량 폭발이면 절반 매도, 나머지는 고점 대비 -5% 트레일링, 처음 -5% 손절, 3일 만기.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.indicators.stoch_rsi import stoch_rsi_all_periods
from src.scoring import EPS

KST = ZoneInfo("Asia/Seoul")
BOTTOM = 20.0
HIGH = 80.0
BURST_MULTIPLE = 2.0
BURST_LOOKBACK = 20
TURN_GAP = 5.0  # 바닥 턴 직전: D-K 격차 상한(포인트)
ARMED_BARS = 4  # 4시간 준비 상태 유효 봉 수 (16시간)


# ---------------------------------------------------------------- 배열 헬퍼
def shift(a: np.ndarray, n: int = 1) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if n < len(a):
        out[n:] = a[:-n]
    return out


def crossed_up(k, d):
    with np.errstate(invalid="ignore"):
        return (shift(k) <= shift(d) + EPS) & (k > d + EPS)


def crossed_down(k, d):
    with np.errstate(invalid="ignore"):
        return (shift(k) >= shift(d) - EPS) & (k < d - EPS)


def bottom_zone(k, d):
    with np.errstate(invalid="ignore"):
        return np.minimum(k, d) <= BOTTOM + EPS


def high_zone(k, d):
    with np.errstate(invalid="ignore"):
        return np.maximum(k, d) >= HIGH - EPS


def roll_bool(b, n: int) -> np.ndarray:
    return pd.Series(np.asarray(b, dtype=float)).rolling(n, min_periods=1).max().astype(bool).to_numpy()


def bottom_turn(k, d):
    with np.errstate(invalid="ignore"):
        rising2 = (k > shift(k)) & (shift(k) > shift(k, 2))
        return (k < d) & ((d - k) <= TURN_GAP) & rising2 & roll_bool(bottom_zone(k, d), 6)


def rising_state(k, d):
    """(상승 체제 여부 배열, 마지막 바닥 골든크로스 봉 번호 배열)."""
    gc = crossed_up(k, d) & roll_bool(bottom_zone(k, d), 6)
    dc = crossed_down(k, d)
    idx = np.arange(len(k))
    last_gc = np.maximum.accumulate(np.where(gc, idx, -1))
    last_dc = np.maximum.accumulate(np.where(dc, idx, -1))
    return last_gc > last_dc, last_gc


def frame_indicators(df: pd.DataFrame):
    """(단기/중기/장기 (K, D) 배열, 거래량 폭발 배열, 상승 봉 배열)."""
    close = df["close"].astype(float).reset_index(drop=True)
    per = stoch_rsi_all_periods(close)
    kd = {name: (per[name]["k"].to_numpy(float), per[name]["d"].to_numpy(float)) for name in ("short", "mid", "long")}
    value = df["value"].astype(float).reset_index(drop=True)
    burst = (value >= BURST_MULTIPLE * value.shift(1).rolling(BURST_LOOKBACK).mean()).fillna(False).to_numpy()
    up = (close > close.shift(1)).fillna(False).to_numpy()
    return kd, burst, up


# ---------------------------------------------------------------- 프레임별 평가 (봉마다의 배열)
def eval_day(df: pd.DataFrame) -> dict:
    """일봉 단기가 바닥 구간이거나 막 턴한 상태(최근 2봉 안 바닥 골든크로스, 또는 바닥 턴 직전)."""
    kd, _, _ = frame_indicators(df)
    k, d = kd["short"]
    gc_bottom = crossed_up(k, d) & roll_bool(bottom_zone(k, d), 6)
    ok = bottom_zone(k, d) | roll_bool(gc_bottom, 2) | bottom_turn(k, d)
    return {"ok": ok, "kd": kd}


def eval_4h(df: pd.DataFrame) -> dict:
    """4시간: 상승 체제·여력(perm), 준비 상태(armed), 고점 제외(veto), 절반 매도 신호(high_sig)."""
    kd, burst, up = frame_indicators(df)
    mid_rise, mid_start = rising_state(*kd["mid"])
    long_rise, long_start = rising_state(*kd["long"])
    start = np.maximum(mid_start, long_start)
    cum = np.cumsum(burst & up)
    since = cum - np.where(start >= 0, cum[np.clip(start, 0, None)], 0)  # 두 체제가 함께 시작된 뒤의 거래량 폭발 상승 봉 수
    perm = mid_rise & long_rise & (start >= 0) & (since == 0)
    armed = roll_bool(bottom_zone(*kd["short"]), ARMED_BARS)
    veto = high_zone(*kd["mid"]) & high_zone(*kd["short"])
    with np.errstate(invalid="ignore"):
        high_sig = (kd["short"][0] >= HIGH - EPS) & burst
    return {"perm": perm, "armed": armed, "veto": veto, "high_sig": high_sig, "mid_rise": mid_rise, "long_rise": long_rise,
            "since": since, "kd": kd, "burst": burst}


def eval_1h(df: pd.DataFrame) -> dict:
    """1시간: 최근 3봉 안에 바닥 구간이 있었고 지금 바닥 턴 직전이거나 골든크로스(trig), 최근 3봉 안 거래량 폭발(vol)."""
    kd, burst, _ = frame_indicators(df)
    k, d = kd["short"]
    trig = roll_bool(bottom_zone(k, d), 3) & (bottom_turn(k, d) | crossed_up(k, d))
    return {"trig": trig, "vol": roll_bool(burst, 3), "kd": kd}


# ---------------------------------------------------------------- 지금 시점(마지막 봉) 판단
def _last_kd(kd: dict, name: str) -> tuple[float | None, float | None]:
    k, d = kd[name]
    f = lambda v: None if not np.isfinite(v) else round(float(v), 1)  # noqa: E731
    return f(k[-1]), f(d[-1])


def evaluate_now(day: pd.DataFrame, h4: pd.DataFrame | None, h1: pd.DataFrame | None) -> dict:
    """각 프레임의 마지막 마감 봉 기준으로 단계별 통과 여부. 앞 단계가 실패하면 뒤 프레임은 None으로 두고 계산하지 않는다.
    stage: 0 미통과 / 1 일봉 OK / 2 +4시간 상승체제·여력 / 3 +준비(고점 제외) / 4 +1시간 바닥 턴 (거래량·지지 터치는 호출하는 쪽에서)."""
    out: dict = {"stage": 0, "day_ok": False}
    dv = eval_day(day)
    out["day_ok"] = bool(dv["ok"][-1])
    out["day_short"] = _last_kd(dv["kd"], "short")
    if not out["day_ok"] or h4 is None:
        return out
    out["stage"] = 1
    v4 = eval_4h(h4)
    out.update({
        "h4_perm": bool(v4["perm"][-1]), "h4_armed": bool(v4["armed"][-1]), "h4_veto": bool(v4["veto"][-1]),
        "h4_mid_rise": bool(v4["mid_rise"][-1]), "h4_long_rise": bool(v4["long_rise"][-1]), "h4_burst_since": int(v4["since"][-1]),
        "h4_short": _last_kd(v4["kd"], "short"), "h4_mid": _last_kd(v4["kd"], "mid"), "h4_long": _last_kd(v4["kd"], "long"),
    })
    if not out["h4_perm"]:
        return out
    out["stage"] = 2
    if not (out["h4_armed"] and not out["h4_veto"]):
        return out
    out["stage"] = 3
    if h1 is None:
        return out
    v1 = eval_1h(h1)
    out.update({"h1_trig": bool(v1["trig"][-1]), "h1_vol": bool(v1["vol"][-1]), "h1_short": _last_kd(v1["kd"], "short")})
    if out["h1_trig"]:
        out["stage"] = 4
    return out


def half_sale_bars(h4: pd.DataFrame, after: datetime) -> list[tuple[pd.Timestamp, float]]:
    """진입 시각(after) 이후에 마감된 4시간 봉 중 절반 매도 신호(단기 K>=80 + 거래량 폭발)가 난 봉의 (마감 시각(KST), 종가).
    h4의 time은 캔들 시작 시각(KST, 타임존 없는 값)이라 마감 시각은 시작 + 4시간이다. after가 타임존이 있는 시각이면
    KST 값으로 바꿔서 비교한다."""
    if after.tzinfo is not None:
        after = after.astimezone(KST).replace(tzinfo=None)
    after = pd.Timestamp(after)
    v4 = eval_4h(h4)
    close_times = pd.to_datetime(h4["time"]) + pd.Timedelta(hours=4)
    out = []
    for i in np.flatnonzero(v4["high_sig"]):
        t = close_times.iloc[i]
        if t > after:
            out.append((t, float(h4["close"].iloc[i])))
    return out
