"""비트코인 매크로 분석 (대시보드 '비트코인 분석' 탭과 매일 아침 텔레그램 브리핑의 원본).

1) 지지·저항 중첩: 4시간봉 최근 3개월의 큰 파동/작은 파동(고점-저점 쌍)마다 피보나치 라인을 긋고, 여기에 실제로 가격이
   반등했던 저점과 꺾였던 고점(가격 반응 자리)을 전부 모아 같이 묶는다. 현재가 아래(지지)/위(저항)에서 서로 다른 근거가
   몇 개나 겹치는지로 강조하고, 피보나치 라인과 가격 반응 자리가 겹치는 구간(◆)은 한 단계 더 강조한다.
   겹친 곳은 한 점이 아니라 겹친 범위(구간)로 표기한다.
   라인은 모두 '저점=0, 고점=1'로 놓고 계산한다 (TradingView 피보나치 도구를 저점→고점으로 그은 값과 같다).
   상승 파동 규칙: 0.618 라인 위를 지키면 상승 지속, 0.382 라인 아래로 종가가 내려가면 상승 무마.
   하락 파동은 이를 거울로 뒤집는다 (반등이 0.382 아래에 머물면 하락 지속, 0.618 위로 오르면 하락 무마).
2) 일봉/4시간/1시간 x 단기/중기/장기 스토캐스틱 RSI: 저점권/고점권, 골든/데드크로스, 상승/하락 상태와 해석.

네트워크 호출 없이 캔들 DataFrame(time, open, high, low, close)만 받아 계산한다 (macro_job이 조회/저장/발송 담당).
판단은 모두 마감된 캔들 기준이고, 현재가는 거리 표시에만 쓴다.
"""

from dataclasses import dataclass

import pandas as pd

from src import config
from src.btc_trend import days_above_ma, is_trend_favorable
from src.indicators.stoch_rsi import stoch_rsi_all_periods
from src.scoring import EPS, _crossed_up_series

# ---------------- 피보나치 설정 ----------------
FIB_RATIOS = (0.382, 0.5, 0.618)  # 0.236/0.786까지 늘리면 라인이 너무 촘촘해져 겹침 개수가 변별력을 잃는다
FIB_EXTENSIONS = (1.272, 1.618)  # 가장 최근 파동의 확장 라인 (고점 위/저점 아래에 라인이 없을 때를 위해)
CANDLES_4H = 540  # 4시간봉 540개 = 90일 = 최근 3개월
PIVOT_BARS = {"large": 12, "small": 4}  # 좌우 몇 개 캔들보다 높/낮아야 고점/저점으로 보는지 (12개 = 2일, 4개 = 16시간)
MIN_LEG_PCT = {"large": 10.0, "small": 3.0}  # 직전 극점에서 이만큼 움직이지 못한 반대 방향 움직임은 파동으로 치지 않는다
LEGS_USED = {"large": 3, "small": 4}  # 라인을 그을 최근 파동 수
CLUSTER_TOL_PCT = 0.5  # 이 폭 안에 들어온 라인들은 '겹친' 것으로 본다
SWING_PIVOT_BARS = 6  # 가격 반응 자리: 좌우 이만큼(6개 = 24시간)의 캔들보다 높/낮은 캔들을 고점/저점으로 모은다 (지그재그로 걸러내지 않고 전부)
SWING_MIN_MOVE_PCT = 1.0  # 그 고점/저점에서 반대로 최소 이만큼 움직였어야 '반응'으로 친다 (사소한 흔들림 제외)
LEVEL_RANGE_PCT = 15.0  # 현재가에서 이 범위 밖의 라인은 다루지 않는다
STRENGTH_LABEL = {1: "약함", 2: "중간", 3: "강함", 4: "매우 강함", 5: "핵심"}
WAVE_LABEL = {"large": "큰 파동", "small": "작은 파동"}

# ---------------- 스토캐스틱 RSI 설정 ----------------
STOCH_FRAMES = ("day", "4h", "1h")
STOCH_PERIODS = ("short", "mid", "long")
FRAME_LABEL = {"day": "일봉", "4h": "4시간", "1h": "1시간"}
PERIOD_LABEL = {"short": "단기", "mid": "중기", "long": "장기"}
CROSS_RECENT_BARS = 3  # 최근 이 캔들 수 안에 교차가 있었으면 골든/데드크로스로 안내
EXTREME_LOOKBACK = 5  # 교차 직전 이 캔들 수 안에 저점권/고점권을 찍었는지 (과매도 반등/과열 하락 구분)
OVERSOLD = float(config.OVERSOLD_THRESHOLD)
OVERBOUGHT = 100.0 - OVERSOLD

CHART_SHOWN_BARS = CANDLES_4H


# =====================================================================================
# 피보나치
# =====================================================================================
@dataclass
class Pivot:
    idx: int
    kind: str  # "H" 고점 / "L" 저점
    price: float
    time: pd.Timestamp
    provisional: bool = False  # 아직 좌우 확인 캔들이 부족한 진행 중 고점/저점


@dataclass
class Leg:
    wave: str
    start: Pivot
    end: Pivot

    @property
    def up(self) -> bool:
        return self.end.price > self.start.price

    @property
    def low(self) -> float:
        return min(self.start.price, self.end.price)

    @property
    def high(self) -> float:
        return max(self.start.price, self.end.price)

    @property
    def range(self) -> float:
        return self.high - self.low

    def at(self, ratio: float) -> float:
        """저점=0, 고점=1일 때 ratio 위치의 가격."""
        return self.low + ratio * self.range


def find_pivots(df: pd.DataFrame, n: int, min_leg_pct: float) -> list[Pivot]:
    """좌우 n개 캔들 중 가장 높은(낮은) 캔들을 고점(저점)으로 잡는다. 같은 종류가 연달아 나오면 더 극단적인 쪽만 남기고,
    직전 극점에서 min_leg_pct만큼 움직이지 못한 반대 방향 극점은 잡음으로 무시한다 (지그재그 방식 — 파동이 끊김 없이 이어진다).
    마지막 n개 캔들은 확인이 안 돼 여기서는 잡히지 않는다 (add_provisional이 진행 중인 극점을 따로 붙인다)."""
    win = 2 * n + 1
    is_high = df["high"] == df["high"].rolling(win, center=True).max()
    is_low = df["low"] == df["low"].rolling(win, center=True).min()

    pivots: list[Pivot] = []
    for i in df.index[is_high | is_low]:
        kinds = [k for k, flag in (("H", is_high[i]), ("L", is_low[i])) if flag]
        if len(kinds) == 2:  # 한 캔들이 고점이자 저점(장대 캔들): 직전과 반대 종류만 인정
            if not pivots:
                continue
            kinds = ["H" if pivots[-1].kind == "L" else "L"]
        kind = kinds[0]
        price = float(df.at[i, "high" if kind == "H" else "low"])
        pivot = Pivot(int(i), kind, price, df.at[i, "time"])
        if pivots and pivots[-1].kind == kind:
            better = price > pivots[-1].price if kind == "H" else price < pivots[-1].price
            if better:
                pivots[-1] = pivot
        elif not pivots or abs(price / pivots[-1].price - 1) * 100 >= min_leg_pct:
            pivots.append(pivot)
    return pivots


def add_provisional(df: pd.DataFrame, pivots: list[Pivot], min_leg_pct: float) -> list[Pivot]:
    """마지막으로 확정된 극점 이후의 진행 중인 움직임을 임시 극점으로 붙인다.
    (확정된 저점 뒤에 이미 반등이 나왔다면, 그 반등의 최고점이 '직전 고점'이다.)"""
    if not pivots:
        return pivots
    last = pivots[-1]
    tail = df.iloc[last.idx + 1:]
    if tail.empty:
        return pivots
    # 확정된 극점보다 더 극단적인 가격이 이미 나왔다면 그 극점을 진행 중인 것으로 갈아 끼운다
    if last.kind == "L" and tail["low"].min() < last.price:
        j = int(tail["low"].idxmin())
        last = Pivot(j, "L", float(df.at[j, "low"]), df.at[j, "time"], provisional=True)
        pivots = pivots[:-1] + [last]
        tail = df.iloc[j + 1:]
    elif last.kind == "H" and tail["high"].max() > last.price:
        j = int(tail["high"].idxmax())
        last = Pivot(j, "H", float(df.at[j, "high"]), df.at[j, "time"], provisional=True)
        pivots = pivots[:-1] + [last]
        tail = df.iloc[j + 1:]
    if tail.empty:
        return pivots
    if last.kind == "L":
        j = int(tail["high"].idxmax())
        cand = Pivot(j, "H", float(df.at[j, "high"]), df.at[j, "time"], provisional=True)
    else:
        j = int(tail["low"].idxmin())
        cand = Pivot(j, "L", float(df.at[j, "low"]), df.at[j, "time"], provisional=True)
    if abs(cand.price / last.price - 1) * 100 >= min_leg_pct:
        pivots = pivots + [cand]
    return pivots


def build_legs(df: pd.DataFrame, wave: str) -> list[Leg]:
    pivots = add_provisional(df, find_pivots(df, PIVOT_BARS[wave], MIN_LEG_PCT[wave]), MIN_LEG_PCT[wave])
    return [Leg(wave, a, b) for a, b in zip(pivots, pivots[1:])]


def _leg_desc(leg: Leg) -> str:
    return f"{'상승' if leg.up else '하락'} {leg.start.price:,.0f}→{leg.end.price:,.0f}"


def wave_state(leg: Leg, close: float) -> dict:
    """가장 최근 파동에서 지금 종가가 피보나치 라인 어디에 있는지 판단한다 (규칙은 모듈 설명 참고)."""
    f = round((close - leg.low) / leg.range, 6)  # 저점=0, 고점=1 기준 종가 위치 (라인 위에 정확히 걸친 값이 부동소수 오차로 뒤집히지 않게 반올림)
    p382, p500, p618 = leg.at(0.382), leg.at(0.5), leg.at(0.618)
    if leg.up:
        if f >= 1:
            code, bias, label = "new_high", "bull", "고점 갱신 중"
            text = f"상승 파동의 고점({leg.high:,.0f})을 넘어 계속 오르는 중이에요."
        elif f >= 0.618:
            code, bias, label = "continue", "bull", "상승 지속"
            text = f"0.618 라인({p618:,.0f}) 위를 지키고 있어 상승이 이어지는 흐름이에요."
        elif f >= 0.382:
            code, bias, label = "pullback", "neutral", "조정 구간"
            text = (f"0.618 라인({p618:,.0f}) 아래로 눌렸고 0.382 라인({p382:,.0f})은 지키는 중이에요. "
                    f"종가가 0.382 라인 아래로 내려가면 상승이 무마돼요.")
        else:
            code, bias, label = "invalid", "bear", "상승 무마"
            text = f"종가가 0.382 라인({p382:,.0f}) 아래로 내려와 직전 상승 파동이 되돌려졌어요."
    else:
        if f <= 0:
            code, bias, label = "new_low", "bear", "저점 갱신 중"
            text = f"하락 파동의 저점({leg.low:,.0f})을 깨고 계속 내리는 중이에요."
        elif f <= 0.382:
            code, bias, label = "continue", "bear", "하락 지속"
            text = f"반등이 0.382 라인({p382:,.0f}) 아래에 머물러 하락이 이어지는 흐름이에요."
        elif f < 0.618:
            code, bias, label = "rebound", "neutral", "반등 구간"
            text = (f"0.382 라인({p382:,.0f}) 위로 반등했지만 0.618 라인({p618:,.0f}) 아래예요. "
                    f"종가가 0.618 라인 위로 오르면 하락이 무마돼요.")
        else:
            code, bias, label = "invalid", "bull", "하락 무마"
            text = f"종가가 0.618 라인({p618:,.0f}) 위로 올라 직전 하락 파동이 되돌려졌어요."
    return {
        "direction": "up" if leg.up else "down",
        "code": code, "bias": bias, "label": label, "text": text, "position": round(float(f), 3),
        "start": {"price": leg.start.price, "time": leg.start.time.isoformat(), "provisional": leg.start.provisional},
        "end": {"price": leg.end.price, "time": leg.end.time.isoformat(), "provisional": leg.end.provisional},
        "lines": {"0.382": round(p382, 1), "0.5": round(p500, 1), "0.618": round(p618, 1)},
    }


def swing_pivots(df: pd.DataFrame) -> list[Pivot]:
    """가격이 반등했던 저점과 꺾였던 고점을 지그재그 없이 전부 모은다 (같은 자리에서 여러 번 반응했으면 각각 센다).
    좌우 SWING_PIVOT_BARS개 캔들 중 가장 높은(낮은) 캔들이고, 그 뒤 SWING_PIVOT_BARS개 캔들 안에 반대 방향으로
    SWING_MIN_MOVE_PCT 이상 움직였어야 한다 (고점은 그만큼 꺾여야, 저점은 그만큼 반등해야 '반응'으로 친다)."""
    n = SWING_PIVOT_BARS
    win = 2 * n + 1
    is_high = df["high"] == df["high"].rolling(win, center=True).max()
    is_low = df["low"] == df["low"].rolling(win, center=True).min()
    out: list[Pivot] = []
    last_idx = {"H": -10**9, "L": -10**9}
    for i in df.index[is_high | is_low]:
        hi_i = min(len(df), i + n + 1)  # 그 캔들부터 뒤로 n개 (반응이 나온 쪽)
        for kind, flag in (("H", is_high[i]), ("L", is_low[i])):
            if not flag or i - last_idx[kind] <= n:  # 같은 값이 이어지는 고원은 첫 캔들만
                continue
            if kind == "H":
                price = float(df.at[i, "high"])
                move = (price / float(df["low"].iloc[i:hi_i].min()) - 1) * 100
            else:
                price = float(df.at[i, "low"])
                move = (float(df["high"].iloc[i:hi_i].max()) / price - 1) * 100
            if move < SWING_MIN_MOVE_PCT:
                continue
            out.append(Pivot(int(i), kind, price, df.at[i, "time"]))
            last_idx[kind] = i
    return out


def _kst_day(ts: pd.Timestamp) -> str:
    """4시간봉 마감 시각(UTC) -> 그 캔들이 시작된 한국시간 '월-일'."""
    return (ts - pd.Timedelta(hours=4) + pd.Timedelta(hours=9)).strftime("%m-%d")


def _level_points(legs_by_wave: dict[str, list[Leg]], swings: list[Pivot] | None = None) -> list[dict]:
    points = []
    for pv in swings or []:
        points.append({
            "price": pv.price, "ratio": None, "kind": "swing", "wave": "swing", "leg": f"s{pv.idx}", "type": pv.kind,
            "desc": f"{_kst_day(pv.time)} {'고점에서 꺾임' if pv.kind == 'H' else '저점에서 반등'}",
        })
    for wave, legs in legs_by_wave.items():
        used = legs[-LEGS_USED[wave]:]
        for k, leg in enumerate(used):
            leg_id = f"{leg.start.idx}-{leg.end.idx}"  # 큰/작은 파동이 같은 구간이면 한 파동으로 센다
            desc = f"{WAVE_LABEL[wave]} {_leg_desc(leg)}"
            for r in FIB_RATIOS:
                points.append({"price": leg.at(r), "ratio": r, "kind": "ret", "wave": wave, "leg": leg_id, "desc": desc})
            if k == len(used) - 1:  # 가장 최근 파동만 확장 라인을 둔다
                for e in FIB_EXTENSIONS:
                    price = leg.low + e * leg.range if leg.up else leg.high - e * leg.range
                    points.append({"price": price, "ratio": e, "kind": "ext", "wave": wave, "leg": leg_id, "desc": desc})
    return points


def build_zones(points: list[dict], price: float) -> list[dict]:
    """가격이 가까운 라인/자리끼리 묶어 '겹침 구간'을 만든다. 근거는 두 종류다 — 피보나치 라인(서로 다른 파동의 수로 센다.
    한 파동의 0.5와 0.618이 붙어 있는 것은 겹침으로 세지 않는다)과 가격 반응 자리(반등한 저점/꺾인 고점, 하나가 1회).
    겹침 개수는 두 근거를 합친 수이고, 피보나치와 가격 반응이 함께 있는 구간(confluence)은 강도를 한 단계 더 올린다."""
    lo_bound, hi_bound = price * (1 - LEVEL_RANGE_PCT / 100), price * (1 + LEVEL_RANGE_PCT / 100)
    pts = sorted((p for p in points if lo_bound <= p["price"] <= hi_bound), key=lambda p: p["price"])
    clusters: list[list[dict]] = []
    for p in pts:
        if clusters:
            mean = sum(m["price"] for m in clusters[-1]) / len(clusters[-1])
            if abs(p["price"] - mean) <= mean * CLUSTER_TOL_PCT / 100:
                clusters[-1].append(p)
                continue
        clusters.append([p])

    zones = []
    for members in clusters:
        prices = [m["price"] for m in members]
        center = sum(prices) / len(prices)
        fib_count = len({m["leg"] for m in members if m["kind"] != "swing"})
        swing_count = len({m["leg"] for m in members if m["kind"] == "swing"})
        count = fib_count + swing_count
        confluence = fib_count > 0 and swing_count > 0
        strength = min(count + (1 if confluence else 0), 5)
        zones.append({
            "price": round(center, 1),
            "low": round(min(prices), 1),
            "high": round(max(prices), 1),
            "count": count,
            "fib_count": fib_count,
            "swing_count": swing_count,
            "confluence": confluence,
            "strength": strength,
            "strength_label": STRENGTH_LABEL[strength],
            "side": "support" if center < price else "resistance",
            "distance_pct": round((center / price - 1) * 100, 2),
            "members": [
                {"desc": m["desc"], "ratio": m["ratio"], "kind": m["kind"], "wave": m["wave"], "type": m.get("type")}
                for m in sorted(members, key=lambda m: m["price"])
            ],
        })
    return zones


def analyse_fib(df4h: pd.DataFrame, price: float) -> dict:
    df = df4h.reset_index(drop=True)
    legs = {wave: build_legs(df, wave) for wave in ("large", "small")}
    close = float(df["close"].iloc[-1])  # 마지막으로 마감된 4시간봉 종가

    waves = {wave: wave_state(lg[-1], close) for wave, lg in legs.items() if lg}
    swings = swing_pivots(df)
    # 아직 확정 전인 최근 고점/저점(작은 파동의 진행 중인 끝)도 반응 자리로 본다 — 지금 막 꺾이는 자리가 저항이 되기 때문이다
    if legs["small"] and legs["small"][-1].end.provisional:
        tip = legs["small"][-1].end
        if all(abs(tip.idx - pv.idx) > SWING_PIVOT_BARS or pv.kind != tip.kind for pv in swings):
            swings.append(tip)
    zones = build_zones(_level_points(legs, swings), price)
    supports = sorted((z for z in zones if z["side"] == "support"), key=lambda z: -z["price"])
    resistances = sorted((z for z in zones if z["side"] == "resistance"), key=lambda z: z["price"])

    all_pivots = {
        wave: [
            {"time": lg.start.time.isoformat(), "price": lg.start.price, "kind": lg.start.kind}
            for lg in legs[wave][-LEGS_USED[wave]:]
        ] + ([{"time": legs[wave][-1].end.time.isoformat(), "price": legs[wave][-1].end.price,
               "kind": legs[wave][-1].end.kind}] if legs[wave] else [])
        for wave in legs
    }
    return {
        "basis_close": close,
        "basis_time": df["time"].iloc[-1].isoformat(),
        "waves": waves,
        "read": _fib_read(waves),
        "supports": supports,
        "resistances": resistances,
        "pivots": all_pivots,
        "swings": [{"time": pv.time.isoformat(), "price": pv.price, "kind": pv.kind} for pv in swings],
        "params": {
            "candles": len(df), "ratios": list(FIB_RATIOS), "cluster_tol_pct": CLUSTER_TOL_PCT,
            "swing_pivot_bars": SWING_PIVOT_BARS, "swing_min_move_pct": SWING_MIN_MOVE_PCT,
            "legs_used": LEGS_USED, "range_pct": LEVEL_RANGE_PCT,
        },
    }


def _fib_read(waves: dict) -> str:
    large, small = waves.get("large"), waves.get("small")
    if not large or not small:
        return "파동을 충분히 찾지 못했어요."
    lb, sb = large["bias"], small["bias"]
    if lb == "bull" and sb == "bull":
        return "큰 파동과 작은 파동 모두 상승 쪽이 우세해요."
    if lb == "bull" and sb == "neutral":
        return "큰 상승 흐름은 유지되는 가운데 작은 파동이 조정 중이에요. 아래 지지 구간에서 받쳐주는지가 관건이에요."
    if lb == "bull":
        return "큰 상승 흐름은 남아 있지만 작은 파동은 약해졌어요. 단기 변동성에 주의하세요."
    if lb == "neutral" and sb == "bull":
        return "큰 파동은 조정 중인데 작은 파동은 반등하고 있어요. 위쪽 저항 구간을 넘는지 확인이 필요해요."
    if lb == "neutral" and sb == "bear":
        return "큰 파동 조정 중에 작은 파동도 약해요. 아래 지지 구간이 무너지는지 지켜보세요."
    if lb == "neutral":
        return "큰 파동이 조정 구간에 있어 방향이 정해지지 않았어요."
    if lb == "bear" and sb == "bull":
        return "큰 하락 흐름 속의 작은 반등이에요. 위쪽 저항 구간에서 막히는지가 관건이에요."
    return "큰 파동과 작은 파동 모두 하락 쪽이 우세해요."


# =====================================================================================
# 스토캐스틱 RSI
# =====================================================================================
def _crossed_down_series(k: pd.Series, d: pd.Series) -> pd.Series:
    prev_k, prev_d = k.shift(1), d.shift(1)
    return ((prev_k >= prev_d - EPS) & (k < d - EPS)).fillna(False)


def _cross_bars_ago(series: pd.Series, recent: int) -> int | None:
    """가장 최근 True가 마지막 캔들로부터 몇 개 전인지 (0 = 마지막 캔들). recent개 안에 없으면 None."""
    tail = series.tail(recent).tolist()
    for ago, flag in enumerate(reversed(tail)):
        if flag:
            return ago
    return None


def analyse_cell(k: pd.Series, d: pd.Series) -> dict | None:
    if len(k.dropna()) < EXTREME_LOOKBACK + 1 or pd.isna(k.iloc[-1]) or pd.isna(d.iloc[-1]):
        return None
    kv, dv = float(k.iloc[-1]), float(d.iloc[-1])
    zone = "oversold" if kv <= OVERSOLD else "overbought" if kv >= OVERBOUGHT else "middle"

    golden_ago = _cross_bars_ago(_crossed_up_series(k, d), CROSS_RECENT_BARS)
    dead_ago = _cross_bars_ago(_crossed_down_series(k, d), CROSS_RECENT_BARS)
    recent_k = k.tail(EXTREME_LOOKBACK + CROSS_RECENT_BARS)
    if golden_ago is not None and (dead_ago is None or golden_ago <= dead_ago):
        state, bars_ago = "golden", golden_ago
        from_extreme = bool(recent_k.min() <= OVERSOLD + EPS)
    elif dead_ago is not None:
        state, bars_ago = "dead", dead_ago
        from_extreme = bool(recent_k.max() >= OVERBOUGHT - EPS)
    else:
        state, bars_ago, from_extreme = ("up" if kv > dv + EPS else "down"), None, False

    return {
        "k": round(kv, 1), "d": round(dv, 1),
        "zone": zone, "state": state, "bars_ago": bars_ago, "from_extreme": from_extreme,
        "bias": "bull" if state in ("golden", "up") else "bear",
        "rising": bool(kv > float(k.iloc[-2])),
    }


ZONE_TEXT = {"oversold": "저점권", "overbought": "고점권", "middle": "중간권"}
STATE_TEXT = {"golden": "골든크로스", "dead": "데드크로스", "up": "상승 중", "down": "하락 중"}


def cell_text(cell: dict) -> str:
    """예: '골든크로스 · 저점권 반등', '하락 중 · 중간권'."""
    state = STATE_TEXT[cell["state"]]
    if cell["state"] == "golden" and cell["from_extreme"]:
        state += "(저점권 반등)"
    elif cell["state"] == "dead" and cell["from_extreme"]:
        state += "(고점권 하락)"
    return f"{state} · {ZONE_TEXT[cell['zone']]}"


def frame_read(cells: dict[str, dict | None]) -> str:
    s, m, lg = cells.get("short"), cells.get("mid"), cells.get("long")
    if not (s and m and lg):
        return "데이터가 부족해서 해석할 수 없어요."
    bulls = [c["bias"] == "bull" for c in (s, m, lg)]
    hot = " 다만 단기가 고점권이라 과열에 주의하세요." if s["zone"] == "overbought" else ""
    cold = " 단기는 저점권이라 기술적 반등을 노려볼 자리예요." if s["zone"] == "oversold" else ""
    if all(bulls):
        return "단기·중기·장기가 모두 상승 정렬이에요." + hot
    if not any(bulls):
        return "단기·중기·장기가 모두 하락 정렬이에요." + cold
    if lg["bias"] == "bull" and s["bias"] == "bear":
        if s["zone"] == "oversold":
            return "장기는 상승 쪽인데 단기가 저점권까지 눌렸어요. 눌림이 마무리되면 반등 후보예요."
        return "장기는 상승 쪽인데 단기가 눌리는 중이에요. 저점권에 닿는지 지켜보세요."
    if lg["bias"] == "bear" and s["bias"] == "bull":
        extra = " 단기 골든크로스가 막 나왔어요." if s["state"] == "golden" else ""
        return "장기는 하락 쪽인데 단기가 반등을 시도 중이에요. 하락 속 기술적 반등일 수 있어요." + extra
    parts = ", ".join(f"{PERIOD_LABEL[n]} {'상승' if c['bias'] == 'bull' else '하락'}" for n, c in
                      (("short", s), ("mid", m), ("long", lg)))
    return f"주기별 신호가 엇갈려요 ({parts})."


def analyse_stoch(frames: dict[str, pd.DataFrame]) -> dict:
    out_frames = {}
    bull = total = 0
    highlights = []
    for frame in STOCH_FRAMES:
        periods = stoch_rsi_all_periods(frames[frame]["close"])
        cells = {name: analyse_cell(periods[name]["k"], periods[name]["d"]) for name in STOCH_PERIODS}
        for name, cell in cells.items():
            if not cell:
                continue
            cell["text"] = cell_text(cell)
            total += 1
            bull += cell["bias"] == "bull"
            label = f"{FRAME_LABEL[frame]} {PERIOD_LABEL[name]}"
            if cell["state"] == "golden":
                highlights.append(f"{label} 골든크로스" + ("(저점권 반등)" if cell["from_extreme"] else ""))
            elif cell["state"] == "dead":
                highlights.append(f"{label} 데드크로스" + ("(고점권 하락)" if cell["from_extreme"] else ""))
            elif cell["zone"] == "oversold":
                highlights.append(f"{label} 저점권")
            elif cell["zone"] == "overbought":
                highlights.append(f"{label} 고점권")
        out_frames[frame] = {"cells": cells, "read": frame_read(cells)}

    if total == 0:
        overall = "데이터가 부족해요."
    else:
        overall = f"{total}개 신호 중 {bull}개가 상승(K>D) 쪽이에요."
    return {"frames": out_frames, "bull_count": bull, "total": total, "overall": overall, "highlights": highlights}


# =====================================================================================
# 종합
# =====================================================================================
def _chart(df4h: pd.DataFrame) -> dict:
    """차트용 4시간봉. 저장 용량을 줄이려고 '첫 마감 시각 + 간격 + 정수 시/고/저/종가'로 압축한다."""
    shown = df4h.tail(CHART_SHOWN_BARS)
    return {
        "first_close_ts": int(shown["time"].iloc[0].timestamp()),
        "step_seconds": 4 * 3600,
        "ohlc": [[round(r.open), round(r.high), round(r.low), round(r.close)] for r in shown.itertuples()],
    }


def analyse(day: pd.DataFrame, h4: pd.DataFrame, h1: pd.DataFrame, price: float) -> dict:
    """day/h4/h1은 fetch_ohlcv(closed_only=True) 결과, price는 실시간 현재가."""
    day_close = day["close"]
    prev24 = float(h1["close"].iloc[-25]) if len(h1) >= 25 else None
    return {
        "price": price,
        "change_24h_pct": round((float(h1["close"].iloc[-1]) / prev24 - 1) * 100, 2) if prev24 else None,
        "trend": {
            "favorable": is_trend_favorable(day_close),
            "days_above": days_above_ma(day_close),
            "ma20": round(float(day_close.rolling(20).mean().iloc[-1]), 1),
            "close": float(day_close.iloc[-1]),
        },
        "fib": analyse_fib(h4, price),
        "stoch": analyse_stoch({"day": day, "4h": h4, "1h": h1}),
        "chart": _chart(h4),
    }
