"""ATR calculation, kept isolated so the period is trivially testable/changeable."""

import pandas as pd


def average_true_range_series(candles: pd.DataFrame, period: int) -> pd.Series:
    """
    Returns the full rolling ATR series (not just the latest value) since
    the screener needs "ATR as of each historical day" for breach detection
    and the touch backtest, not only today's ATR.
    """
    high, low, prev_close = candles["high"], candles["low"], candles["close"].shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.rolling(window=period).mean()
