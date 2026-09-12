"""
The "N-of-M block staircase" breakout signal, kept pure and dependency-free
(no Kite, no DB, no config singleton) so it's trivially unit-testable and
reusable from both the live scan and the in-window backtest.

Definition: split the trailing `period_days * n_blocks` trading days into
`n_blocks` consecutive, non-overlapping blocks of `period_days` days each,
the last block ending on the evaluation day. Each block reduces to one
number (`block_value`, e.g. its lowest close). The signal fires if at
least `min_up_periods` of the `n_blocks - 1` block-to-block comparisons are
strictly "up" (block k's value > block k-1's value).

Deliberately does not know about ATR, filters, or persistence -- this file
answers one question only: "given this candle history, on which days (and
with which block breakdown) did the staircase pattern hold?"
"""

from dataclasses import dataclass, field

import pandas as pd

BLOCK_VALUE_FUNCS = {
    "min_close": lambda closes: closes.min(),
    "max_close": lambda closes: closes.max(),
    "last_close": lambda closes: closes.iloc[-1],
    "mean_close": lambda closes: closes.mean(),
}


@dataclass
class SignalResult:
    fired: bool
    n_blocks: int
    up_periods: int          # how many of the n_blocks-1 comparisons were "up"
    required_up_periods: int
    block_values: list[float] = field(default_factory=list)   # oldest block first
    block_end_dates: list = field(default_factory=list)       # same order, Timestamps
    window_start_idx: int = -1   # positional index into the candles frame
    window_end_idx: int = -1


def _block_value(closes: pd.Series, block_value: str) -> float:
    try:
        fn = BLOCK_VALUE_FUNCS[block_value]
    except KeyError:
        raise ValueError(
            f"Unknown block_value '{block_value}', expected one of {sorted(BLOCK_VALUE_FUNCS)}"
        )
    return float(fn(closes))


def evaluate_at(
    candles: pd.DataFrame,
    end_idx: int,
    period_days: int,
    n_blocks: int,
    min_up_periods: int,
    block_value: str = "min_close",
) -> SignalResult | None:
    """
    Evaluates the staircase signal using only data up to and including
    `end_idx` (a positional row index into `candles`, 0-based) -- the
    caller is responsible for choosing end_idx so this never sees future
    rows (no lookahead by construction: nothing past end_idx is ever read).

    Returns None if there isn't enough history before end_idx to fill
    `n_blocks` full blocks.
    """
    window_len = period_days * n_blocks
    start_idx = end_idx - window_len + 1
    if start_idx < 0:
        return None

    closes = candles["close"]
    dates = candles["timestamp"] if "timestamp" in candles.columns else candles.index

    block_values = []
    block_end_dates = []
    for b in range(n_blocks):
        block_start = start_idx + b * period_days
        block_end = block_start + period_days  # exclusive
        block_closes = closes.iloc[block_start:block_end]
        block_values.append(_block_value(block_closes, block_value))
        block_end_dates.append(dates.iloc[block_end - 1])

    up_periods = sum(
        1 for i in range(1, n_blocks) if block_values[i] > block_values[i - 1]
    )
    required = min_up_periods
    fired = up_periods >= required

    return SignalResult(
        fired=fired,
        n_blocks=n_blocks,
        up_periods=up_periods,
        required_up_periods=required,
        block_values=block_values,
        block_end_dates=block_end_dates,
        window_start_idx=start_idx,
        window_end_idx=end_idx,
    )


def scan_all(
    candles: pd.DataFrame,
    period_days: int,
    n_blocks: int,
    min_up_periods: int,
    block_value: str = "min_close",
) -> list[SignalResult]:
    """
    Evaluates the signal at every valid end_idx in `candles` (every row
    once `n_blocks` full blocks fit before it), oldest first. Used by the
    in-window backtest to build the full history of firings, and by the
    live scan (which just looks at the tail of this list) so both paths
    run the exact same evaluation logic.
    """
    window_len = period_days * n_blocks
    results = []
    for end_idx in range(window_len - 1, len(candles)):
        result = evaluate_at(candles, end_idx, period_days, n_blocks, min_up_periods, block_value)
        if result is not None:
            results.append(result)
    return results


def dedupe_consecutive(results: list[SignalResult]) -> list[SignalResult]:
    """
    Collapses a run of consecutive fired days (adjacent end_idx, all
    fired=True) down to just the first day of each run -- re-alignment of
    the trailing window means a staircase can stay "fired" for several
    days in a row as new days roll in; this counts that as one signal
    event, not one per day it stayed true.
    """
    deduped = []
    prev_end_idx = None
    for r in results:
        if not r.fired:
            prev_end_idx = None
            continue
        if prev_end_idx is not None and r.window_end_idx == prev_end_idx + 1:
            prev_end_idx = r.window_end_idx
            continue
        deduped.append(r)
        prev_end_idx = r.window_end_idx
    return deduped
