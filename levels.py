"""
Turns a price history into a list of validated support levels.

Pipeline: find swing lows -> cluster nearby ones into a single level ->
score each level by how many times it held vs. how many times it broke.
"""

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import CONFIG

logger = logging.getLogger("atrx.levels")


@dataclass
class Level:
    price: float
    touches: int
    breaches: int
    recency_weight: float
    pivot_dates: list = field(default_factory=list)


def find_pivot_lows(candles: pd.DataFrame, window: int) -> pd.DataFrame:
    """
    A day is a pivot low if its 'low' is the minimum among the `window`
    days before AND after it. This means the most recent `window` days can
    never be confirmed as pivots yet -- there's no way around that without
    seeing the future, so treat very recent price action as unconfirmed.
    """
    lows = candles["low"].values
    n = len(lows)
    pivot_idx = []
    for i in range(window, n - window):
        segment = lows[i - window: i + window + 1]
        if lows[i] == segment.min() and (segment == lows[i]).sum() == 1:
            pivot_idx.append(i)
    return candles.iloc[pivot_idx]


def _regroup(sorted_pivots: pd.DataFrame, threshold: float) -> list[list[int]]:
    """
    Groups pivot rows (already sorted by 'low' price ascending) whose prices
    sit within `threshold` of each other into the same support level.
    Returns groups as lists of DataFrame index labels.
    """
    groups, current_group = [], [sorted_pivots.index[0]]
    for i in range(1, len(sorted_pivots)):
        idx = sorted_pivots.index[i]
        prev_idx = current_group[-1]
        if sorted_pivots.loc[idx, "low"] - sorted_pivots.loc[prev_idx, "low"] <= threshold:
            current_group.append(idx)
        else:
            groups.append(current_group)
            current_group = [idx]
    groups.append(current_group)
    return groups


def count_breaches(candles: pd.DataFrame, level_price: float, current_atr: float,
                    breach_buffer_atr: float) -> int:
    """
    A breach is a CLOSING price meaningfully below the level -- an intraday
    wick below it doesn't count, since wicks are noise and closes are where
    conviction actually shows up.
    """
    buffer = current_atr * breach_buffer_atr
    return int((candles["close"] < (level_price - buffer)).sum())


def build_levels(candles: pd.DataFrame, current_atr: float) -> list[Level]:
    """Full pipeline: pivots -> clusters -> scored, validated levels."""
    pivots = find_pivot_lows(candles, CONFIG.pivot_window)
    if pivots.empty:
        return []

    groups = _regroup(
        pivots.sort_values("low"), current_atr * CONFIG.cluster_atr_multiple
    )

    levels = []
    latest_date = pd.to_datetime(candles["timestamp"]).max()

    for group_idx in groups:
        group = pivots.loc[group_idx]
        level_price = float(group["low"].median())
        breaches = count_breaches(candles, level_price, current_atr, CONFIG.breach_buffer_atr)

        ages_days = (latest_date - pd.to_datetime(group["timestamp"])).dt.days
        recency_weight = float(np.exp(-ages_days / CONFIG.recency_decay_days).sum())

        levels.append(Level(
            price=level_price,
            touches=len(group),
            breaches=breaches,
            recency_weight=recency_weight,
            pivot_dates=pd.to_datetime(group["timestamp"]).tolist(),
        ))

    return [lvl for lvl in levels
            if lvl.touches >= CONFIG.min_touches and lvl.breaches <= CONFIG.max_breaches]
