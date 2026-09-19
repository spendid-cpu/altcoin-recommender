"""Stochastic RSI 계산. TradingView의 Stoch RSI 정의(Wilder RSI + Stochastic + SMA 스무딩)를 따른다."""

import pandas as pd


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def stoch_rsi(
    close: pd.Series,
    rsi_period: int,
    stoch_period: int,
    smooth_k: int,
    smooth_d: int,
) -> pd.DataFrame:
    """close 시리즈(오래된 순)로부터 %K, %D를 계산해 DataFrame으로 반환한다."""
    r = rsi(close, rsi_period)
    lowest = r.rolling(stoch_period).min()
    highest = r.rolling(stoch_period).max()
    stoch = (r - lowest) / (highest - lowest) * 100
    k = stoch.rolling(smooth_k).mean()
    d = k.rolling(smooth_d).mean()
    return pd.DataFrame({"rsi": r, "k": k, "d": d})


# 설계 문서(스토캐스틱 RSI 설정)에서 정한 단기/중기/장기 파라미터 세트
# 사용자가 트레이딩뷰에서 쓰는 설정이다. 트레이딩뷰 스토캐스틱 RSI의 입력 순서는 (K, D, RSI 길이, 스토캐스틱 길이)라서
# 단기 '3,3,5,5' = K 3 · D 3 · RSI 5 · 스토 5, 중기 '6,6,10,10' = K 6 · D 6 · RSI 10 · 스토 10, 장기 '12,12,20,20' = K 12 · D 12 ·
# RSI 20 · 스토 20 이다. (처음에는 이 숫자를 RSI/스토/K/D 순서로 거꾸로 읽어 트레이딩뷰 차트와 신호가 정반대로 나왔다.)
PERIOD_SETS = {
    "short": {"rsi_period": 5, "stoch_period": 5, "smooth_k": 3, "smooth_d": 3},
    "mid": {"rsi_period": 10, "stoch_period": 10, "smooth_k": 6, "smooth_d": 6},
    "long": {"rsi_period": 20, "stoch_period": 20, "smooth_k": 12, "smooth_d": 12},
}


def stoch_rsi_all_periods(close: pd.Series) -> dict[str, pd.DataFrame]:
    """단기/중기/장기 세 세트를 한 번에 계산."""
    return {name: stoch_rsi(close, **params) for name, params in PERIOD_SETS.items()}
