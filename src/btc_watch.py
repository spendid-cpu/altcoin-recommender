"""비트코인 관찰 알림: 종목 추천이 아니라 '저점권 관찰 중'과 '투매 발생'을 알려주는 상태 분석.

단계 (7년 BTC 백테스트 결과 매수 신호로는 근거가 없어서 알림·표시용으로만 쓴다):
  - 관찰 중: 일봉과 4시간봉 스토캐스틱 RSI(단기) %K가 모두 저점권(config.OVERSOLD_THRESHOLD 이하).
  - 투매 발생: 관찰 중에 마감된 봉이 4시간 -3%(또는 일봉 -5%) 이하로 빠지고 거래량이 직전 20개 평균의 2배 이상.
백테스트(2019-07~2026-09): 투매 봉 종가에 샀다면 이후 7일 평균 -1.6%, 14일 추가 하락 평균 -12.9%로 대체로 바닥이 아니라 하락 중간이었다.
"""

import numpy as np
import pandas as pd

from src import config
from src.indicators.stoch_rsi import stoch_rsi_all_periods

DROP_4H_PCT = -3.0
DROP_DAY_PCT = -5.0
VOLUME_MULT = 2.0
VOLUME_LOOKBACK = 20
OBS_RECENT_BARS = 6  # 관찰 상태였던 걸로 인정하는 최근 4시간봉 수(24시간)
ACTIVE_HOURS = 72  # 투매 봉이 이 시간 안이면 '투매 발생' 상태로 본다
EVENT_KEEP_DAYS = 14


def _utc_iso(ts) -> str:
    """대시보드가 한국시간으로 바꿔 보여줄 수 있게 UTC 시각을 tz 표기 포함 문자열로 낸다(바이낸스 마감 시각은 밀리초 오차가 있어 분 단위로 맞춘다)."""
    return pd.Timestamp(ts).round("min").tz_localize("UTC").isoformat()


def analyse(day: pd.DataFrame, h4: pd.DataFrame) -> dict:
    """day/h4: binance_client.fetch_ohlcv 결과(마감된 봉만, time=UTC 마감 시각, open/high/low/close/volume)."""
    thr = config.OVERSOLD_THRESHOLD
    kd = stoch_rsi_all_periods(day["close"])["short"]["k"].to_numpy()
    k4 = stoch_rsi_all_periods(h4["close"])["short"]["k"].to_numpy()
    day_k = pd.Series(kd, index=pd.DatetimeIndex(day["time"]))
    kd_at = day_k.reindex(pd.DatetimeIndex(h4["time"]), method="ffill").to_numpy()
    obs = (k4 <= thr) & (kd_at <= thr)  # NaN은 False
    obs_recent = pd.Series(obs).rolling(OBS_RECENT_BARS, min_periods=1).max().to_numpy() > 0

    time_pos = {t: i for i, t in enumerate(h4["time"])}
    raw = []
    for frame_name, frame, drop in (("4h", h4, DROP_4H_PCT), ("day", day, DROP_DAY_PCT)):
        vavg = frame["volume"].shift(1).rolling(VOLUME_LOOKBACK).mean()
        ret = (frame["close"] / frame["open"] - 1) * 100
        mult = frame["volume"] / vavg
        for i in np.flatnonzero(((ret <= drop) & (mult >= VOLUME_MULT)).to_numpy()):
            pos = time_pos.get(frame["time"].iloc[i])
            if pos is not None and obs_recent[pos]:
                raw.append({
                    "time": frame["time"].iloc[i], "frame": frame_name, "drop_pct": round(float(ret.iloc[i]), 2),
                    "volume_x": round(float(mult.iloc[i]), 1), "low": float(frame["low"].iloc[i]),
                    "close": float(frame["close"].iloc[i]),
                })
    raw.sort(key=lambda e: e["time"])
    now = h4["time"].iloc[-1]
    recent = [e for e in raw if now - e["time"] <= pd.Timedelta(days=EVENT_KEEP_DAYS)]
    last = recent[-1] if recent else None
    hours_ago = None if last is None else round((now - last["time"]).total_seconds() / 3600, 1)

    observing = bool(obs[-1])
    since = None
    if observing:
        j = len(obs) - 1
        while j > 0 and obs[j - 1]:
            j -= 1
        since = h4["time"].iloc[j]
    active_event = last is not None and hours_ago <= ACTIVE_HOURS
    mode = "capitulation" if active_event else ("observing" if observing else "normal")

    def clean(v):
        return None if v is None or v != v else round(float(v), 1)

    return {
        "mode": mode,
        "observing": observing,
        "day_k": clean(kd[-1]), "h4_k": clean(k4[-1]), "threshold": thr,
        "since": None if since is None else _utc_iso(since),
        "last_event": None if last is None else {
            "time": _utc_iso(last["time"]), "frame": last["frame"], "drop_pct": last["drop_pct"],
            "volume_x": last["volume_x"], "low": last["low"], "close": last["close"], "hours_ago": hours_ago,
        },
    }
