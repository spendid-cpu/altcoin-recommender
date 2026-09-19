"""알트코인 지지 구간 — 사이클 전략의 '지지 터치' 조건.

비트코인 분석(btc_macro)과 같은 방식(반등한 저점/꺾인 고점 + 파동의 피보나치 라인 겹침)으로 4시간봉·일봉 구간을 만들고,
현재가가 강도 3 이상의 지지 구간 안이거나 그 바로 위(변동성 보정 거리 1.0 이내)면 '터치'로 본다.
저장된 캔들에는 고가/저가가 없어 종가로 극점을 잡고(근사), 종목마다 변동성이 달라 비트코인 기준 임계값에
변동성 배율 k(4시간 종가 변동 중앙값 / 0.45%, 1~4로 제한)를 곱한다. 백테스트(scripts 외 support_backtest)와 같은 계산이다."""

import numpy as np
import pandas as pd

from src import btc_macro as m

BTC_MEDIAN_4H_MOVE = 0.45  # 비트코인 4시간 종가 변동(%)의 대략적인 중앙값
MIN_STRENGTH = 3
NEAR_LIMIT = 1.0  # 구간 상단까지의 변동성 보정 거리(%)


def _price_frame(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)
    d["time"] = pd.to_datetime(d["time"])
    d["high"] = d["close"]
    d["low"] = d["close"]
    d["open"] = d["close"]
    return d


def _legs(df: pd.DataFrame, n: int, min_pct: float, wave: str) -> list:
    piv = m.add_provisional(df, m.find_pivots(df, n, min_pct), min_pct)
    return [m.Leg(wave, a, b) for a, b in zip(piv, piv[1:])]


def build_zones(h4: pd.DataFrame, day: pd.DataFrame, price: float) -> tuple[float, list[dict]] | tuple[None, None]:
    """(변동성 배율 k, 지지·저항 구간 목록). 캔들이 부족하면 (None, None)."""
    d4 = _price_frame(h4).tail(m.CANDLES_4H).reset_index(drop=True)
    dd = _price_frame(day).tail(m.DAILY_CANDLES).reset_index(drop=True)
    if len(d4) < 120 or len(dd) < 40:
        return None, None
    move = d4["close"].pct_change().abs().tail(200).median() * 100
    k = float(np.clip(move / BTC_MEDIAN_4H_MOVE, 1.0, 4.0))
    sw4 = m.swing_pivots(d4, m.SWING_PIVOT_BARS, m.SWING_MIN_MOVE_PCT * k)
    legs4 = {"large": _legs(d4, m.PIVOT_BARS["large"], m.MIN_LEG_PCT["large"] * k, "large"),
             "small": _legs(d4, m.PIVOT_BARS["small"], m.MIN_LEG_PCT["small"] * k, "small")}
    z4 = m.build_zones(m._level_points(legs4, sw4, 4), price, m.CLUSTER_TOL_PCT * k, m.LEVEL_RANGE_PCT * k)
    swd = m.swing_pivots(dd, m.DAILY_SWING_BARS, m.DAILY_SWING_MIN_MOVE_PCT * k)
    legsd = {"long": _legs(dd, m.PIVOT_BARS["long"], m.MIN_LEG_PCT["long"] * k, "long")}
    zd = m.build_zones(m._level_points(legsd, swd, 24), price, m.LONG_CLUSTER_TOL_PCT * k, min(m.LONG_RANGE_PCT * k, 90.0))
    return k, z4 + zd


def support_touch(h4: pd.DataFrame, day: pd.DataFrame, price: float) -> tuple[bool, dict | None]:
    """(지지 터치 여부, 가장 가까운 강한 지지 구간 정보). 구간 정보: low, high, strength, gap(변동성 보정 %)."""
    k, zones = build_zones(h4, day, price)
    if zones is None:
        return False, None
    best, best_zone = np.inf, None
    for z in zones:
        if z["strength"] < MIN_STRENGTH or z["low"] > price:
            continue
        gap = max(0.0, price - z["high"]) / price * 100 / k
        if gap < best:
            best, best_zone = gap, z
    if best_zone is None:
        return False, None
    info = {"low": float(best_zone["low"]), "high": float(best_zone["high"]), "strength": int(best_zone["strength"]),
            "gap": round(float(best), 2)}
    return best <= NEAR_LIMIT, info
