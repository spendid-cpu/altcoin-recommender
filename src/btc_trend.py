"""BTC 추세 필터: 바이낸스 USDT-BTC 일봉 종가가 MA20 위에서 2일 이상 유지되는지 판단."""

import pandas as pd


def is_trend_favorable(daily_close: pd.Series, ma_period: int = 20, hold_days: int = 2) -> bool:
    """daily_close: 오래된순 일봉 종가. 최근 hold_days일 연속으로 종가가 MA20 위였으면 True."""
    ma = daily_close.rolling(ma_period).mean()
    above = daily_close > ma
    return bool(above.tail(hold_days).all())
