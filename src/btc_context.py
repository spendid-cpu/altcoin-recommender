"""추천 순간의 비트코인 지표 상태를 기록용으로 만든다 (2026-10-12 사용자 결정: '기록만 우선').

추천 규칙·알림·화면에는 쓰지 않는다. 진입 기록(detail.btc_ctx)에 같이 저장해 두었다가, 추천이 충분히 종료된 뒤
"BTC가 일목구름 위일 때 / 작은 파동이 고점권일 때 나간 추천은 손절이 달랐나"를 같은 기준으로 다시 검증하려는 자료다.
(코인별 지표 18개·BTC 상태 12개 백테스트 스크린은 모두 근거 없음이었고, BTC 일목구름 위 -11~-14%p 손절만 방향이 일관돼 후보로 남았다.)

값은 모두 '마감된' 바이낸스 BTCUSDT 일봉·4시간봉 기준이다 (macro_job.build_macro가 이미 받아 둔 캔들을 그대로 쓴다, 추가 호출 없음).
파라미터는 업계 표준: MACD 12/26/9, 볼린저 20/2, 일목 9/26/52 (백테스트 스크린과 같은 정의 — 정의를 바꾸면 과거 검증과 이어지지 않는다).
  ichi  종가가 구름 위(above)/안(inside)/아래(below)   tk  전환선 > 기준선
  macd_h  MACD 히스토그램 / 종가 x 100 (가격 대비 %)    macd_up  히스토그램이 직전 봉보다 큼    pb  볼린저 %B (0 = 하단, 1 = 상단)
"""

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from src import state_store

MAX_AGE = timedelta(hours=3)  # 이보다 낡은 매크로 분석이면 기록하지 않는다
MIN_BARS = 90  # 일목 선행스팬(52 + 26)과 이평 워밍업에 필요한 최소 캔들 수


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _macd_hist(close: pd.Series) -> pd.Series:
    m = _ema(close, 12) - _ema(close, 26)
    return m - _ema(m, 9)


def _boll_pb(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    return (close - lo) / (up - lo)


def _ichimoku(df: pd.DataFrame):
    hi, lo = df["high"], df["low"]
    tenkan = (hi.rolling(9).max() + lo.rolling(9).min()) / 2
    kijun = (hi.rolling(26).max() + lo.rolling(26).min()) / 2
    a = ((tenkan + kijun) / 2).shift(26)
    b = ((hi.rolling(52).max() + lo.rolling(52).min()) / 2).shift(26)
    return tenkan, kijun, np.fmax(a, b), np.fmin(a, b)  # 지금 봉에 표시되는 구름(26봉 전에 계산한 값)


def frame_state(df: pd.DataFrame) -> dict | None:
    """마감된 캔들 DataFrame(오래된 순, high/low/close 포함) -> 마지막 봉 기준 지표 상태. 캔들이 모자라거나 계산이 안 되면 None."""
    df = df.reset_index(drop=True)
    if len(df) < MIN_BARS:
        return None
    close = df["close"]
    hist = _macd_hist(close)
    pb = _boll_pb(close)
    tenkan, kijun, ctop, cbot = _ichimoku(df)
    vals = [hist.iloc[-1], hist.iloc[-2], pb.iloc[-1], tenkan.iloc[-1], kijun.iloc[-1], ctop.iloc[-1], cbot.iloc[-1]]
    if any(pd.isna(v) for v in vals):
        return None
    last = float(close.iloc[-1])
    ichi = "above" if last > ctop.iloc[-1] else "below" if last < cbot.iloc[-1] else "inside"
    return {
        "ichi": ichi,
        "tk": bool(tenkan.iloc[-1] > kijun.iloc[-1]),
        "macd_h": round(float(hist.iloc[-1]) / last * 100, 4),
        "macd_up": bool(hist.iloc[-1] > hist.iloc[-2]),
        "pb": round(float(pb.iloc[-1]), 3),
    }


def _wave(state: dict | None) -> dict | None:
    if not state:
        return None
    return {"dir": state.get("direction"), "code": state.get("code"), "pos": state.get("position")}


def build(day: pd.DataFrame, h4: pd.DataFrame, waves: dict | None) -> dict:
    """macro_job.build_macro에서 호출: 일봉·4시간봉·파동 상태로 컨텍스트를 만든다 (macro['ctx']로 캐시에 같이 저장)."""
    ctx: dict = {"d": frame_state(day), "h4": frame_state(h4)}
    waves = waves or {}
    ctx["small"], ctx["large"] = _wave(waves.get("small")), _wave(waves.get("large"))
    if len(day) >= 21:
        close = day["close"]
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ctx["ma20"] = {"above": bool(close.iloc[-1] > ma20), "gap_pct": round((float(close.iloc[-1]) / ma20 - 1) * 100, 2)}
    return ctx


def snapshot(now: datetime | None = None) -> dict | None:
    """추천 순간에 진입 기록에 붙일 BTC 컨텍스트: 저장된 매크로 분석의 ctx + MVRV Z-score. 분석이 없거나 낡았으면 None (기록만 생략)."""
    from src import macro_job  # 순환 import 방지

    now = now or datetime.now(timezone.utc)
    macro = macro_job.load_cached()
    if not macro or not macro.get("ctx"):
        return None
    try:
        age = now - datetime.fromisoformat(macro["generated_at"])
    except (KeyError, ValueError):
        return None
    if age > MAX_AGE:
        return None
    ctx = dict(macro["ctx"])
    raw = state_store.get_meta("btc_mvrv_json")
    if raw:
        try:
            m = json.loads(raw)
            ctx["mvrv_z"] = m.get("z")
        except ValueError:
            pass
    ctx["at"] = macro["generated_at"]
    return ctx
