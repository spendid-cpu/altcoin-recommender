"""거래 매물대(거래량 프로파일): 캔들 거래량을 가격대별로 나눠 어느 가격에서 거래가 많이 체결됐는지 본다.
각 봉의 거래량은 그 봉의 고가~저가 구간에 균등하게 퍼졌다고 가정한다(표준적인 근사). 이미 체결된 기록이지, 지금 걸려 있는 주문이 아니다.
참고 표시이고 추천 규칙에는 쓰지 않는다 (매물대 비중을 추천 필터로 쓰는 것은 2026-09 백테스트에서 우위가 확인되지 않았다)."""

import numpy as np
import pandas as pd

N_BINS = 48
SPAN_PCT = 0.35  # 현재가 위아래 이 비율까지만 보여 준다
VALUE_AREA = 0.70  # 거래량 70%가 모인 구간(가치 구간)


def profile(df: pd.DataFrame, bars: int, price: float) -> dict | None:
    """df: high/low/volume 컬럼이 있는 캔들(오래된 것 먼저). 최근 bars개로 계산한다."""
    d = df.tail(bars)
    if d.empty or price <= 0:
        return None
    # 보여 줄 가격 범위: 그 기간에 실제로 거래된 범위(현재가 포함), 단 현재가에서 SPAN_PCT 넘게 먼 곳은 자른다
    lo = max(price * (1 - SPAN_PCT), min(float(d["low"].min()), price))
    hi = min(price * (1 + SPAN_PCT), max(float(d["high"].max()), price))
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, N_BINS + 1)
    vol = np.zeros(N_BINS)
    for high, low, v in zip(d["high"].to_numpy(float), d["low"].to_numpy(float), d["volume"].to_numpy(float)):
        if v <= 0 or high < lo or low > hi:
            continue
        if high <= low:
            i = int(np.clip(np.searchsorted(edges, low, side="right") - 1, 0, N_BINS - 1))
            vol[i] += v
            continue
        overlap = np.clip(np.minimum(edges[1:], high) - np.maximum(edges[:-1], low), 0, None)
        vol += v * overlap / (high - low)
    total = float(vol.sum())
    if total <= 0:
        return None
    poc = int(vol.argmax())
    # 가치 구간: POC에서 시작해 양옆 중 거래량이 큰 쪽으로 넓혀 가며 70%를 채운다
    lo_i = hi_i = poc
    acc = vol[poc]
    while acc < total * VALUE_AREA and (lo_i > 0 or hi_i < N_BINS - 1):
        left = vol[lo_i - 1] if lo_i > 0 else -1.0
        right = vol[hi_i + 1] if hi_i < N_BINS - 1 else -1.0
        if left >= right:
            lo_i -= 1
            acc += vol[lo_i]
        else:
            hi_i += 1
            acc += vol[hi_i]
    return {
        "bars": int(len(d)),
        "step": float(edges[1] - edges[0]),
        "bins": [{"lo": round(float(edges[i]), 2), "hi": round(float(edges[i + 1]), 2), "vol": round(float(vol[i]), 2)} for i in range(N_BINS)],
        "poc": round(float((edges[poc] + edges[poc + 1]) / 2), 2),
        "val": round(float(edges[lo_i]), 2),
        "vah": round(float(edges[hi_i + 1]), 2),
        "share_in_view": round(float(total / max(d["volume"].sum(), 1e-9)), 3),
    }


def build(h4: pd.DataFrame, price: float) -> dict:
    """4시간봉(약 180일치)에서 7일·30일·90일 거래 매물대를 만든다. 4시간봉 6개 = 1일."""
    out = {}
    for days in (7, 30, 90):
        p = profile(h4, days * 6, price)
        if p:
            p["days"] = days
            out[str(days)] = p
    return out
