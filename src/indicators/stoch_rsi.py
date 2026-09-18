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
PERIOD_SETS = {
    "short": {"rsi_period": 3, "stoch_period": 3, "smooth_k": 5, "smooth_d": 5},
    "mid": {"rsi_period": 6, "stoch_period": 6, "smooth_k": 10, "smooth_d": 10},
    "long": {"rsi_period": 12, "stoch_period": 12, "smooth_k": 20, "smooth_d": 20},
}


def stoch_rsi_all_periods(close: pd.Series) -> dict[str, pd.DataFrame]:
    """단기/중기/장기 세 세트를 한 번에 계산."""
    return {name: stoch_rsi(close, **params) for name, params in PERIOD_SETS.items()}
