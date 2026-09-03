"""
For a given level, finds every historical day price traded close to it and
measures what happened over the following few sessions. This is what turns
"here's a line on a chart" into "here's how this line has actually behaved
on this stock historically."
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import CONFIG
from levels import Level


@dataclass
class TouchStats:
    n_touches: int
    hit_rate_short: float | None    # % of touches with positive fwd_ret_short
    hit_rate_long: float | None
    avg_fwd_ret_short: float | None
    avg_fwd_ret_long: float | None


def backtest_level(candles: pd.DataFrame, level: Level, current_atr: float) -> TouchStats:
    """
    A "touch" here is any close within `touch_band_atr` ATRs of the level --
    deliberately broader than the confirmed pivot lows used to define the
    level, so short-lived tests of the level (that never became a full pivot)
    still contribute to the statistics.
    """
    band = current_atr * CONFIG.touch_band_atr
    is_touch = (candles["close"] - level.price).abs() <= band
    touch_positions = np.where(is_touch)[0]

    short_returns, long_returns = [], []
    n = len(candles)

    for pos in touch_positions:
        entry_close = candles["close"].iloc[pos]

        short_idx = pos + CONFIG.forward_days_short
        if short_idx < n:
            fwd = candles["close"].iloc[short_idx]
            short_returns.append((fwd - entry_close) / entry_close)

        long_idx = pos + CONFIG.forward_days_long
        if long_idx < n:
            fwd = candles["close"].iloc[long_idx]
            long_returns.append((fwd - entry_close) / entry_close)

    def summarize(returns: list[float]):
        if not returns:
            return None, None
        arr = np.array(returns)
        hit_rate = float((arr > 0).mean()) * 100
        avg_ret = float(arr.mean()) * 100
        return hit_rate, avg_ret

    hit_short, ret_short = summarize(short_returns)
    hit_long, ret_long = summarize(long_returns)

    return TouchStats(
        n_touches=len(touch_positions),
        hit_rate_short=hit_short, avg_fwd_ret_short=ret_short,
        hit_rate_long=hit_long, avg_fwd_ret_long=ret_long,
    )
