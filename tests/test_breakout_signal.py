"""
Unit tests for breakout_signal.py, built on synthetic candle frames so
they run with no Kite session, no DB, no network -- pure logic checks for
block alignment, the min_up_periods tolerance, dedupe, and no-lookahead.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from breakout_signal import BLOCK_VALUE_FUNCS, dedupe_consecutive, evaluate_at, scan_all


def make_candles(closes: list[float]) -> pd.DataFrame:
    """One row per close, sequential dates, only the `close`/`timestamp`
    columns the signal logic actually reads."""
    dates = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"timestamp": dates, "close": closes})


def test_perfect_staircase_fires_with_min_close():
    # 3 blocks of 3 days, each block strictly higher than the last, and
    # each block internally rising so min_close == first day's close.
    closes = [10, 11, 12,  13, 14, 15,  16, 17, 18]
    candles = make_candles(closes)
    result = evaluate_at(candles, end_idx=8, period_days=3, n_blocks=3, min_up_periods=2)
    assert result.fired
    assert result.up_periods == 2
    assert result.block_values == [10.0, 13.0, 16.0]


def test_one_down_block_tolerated_at_min_up_periods():
    # Block 2's min_close (13) is LOWER than block 1's min_close (14) --
    # one down comparison out of two -- so this only fires when
    # min_up_periods allows one miss, not when it demands both.
    closes = [12, 13, 14,  11, 12, 13,  15, 16, 17]
    candles = make_candles(closes)
    strict = evaluate_at(candles, end_idx=8, period_days=3, n_blocks=3, min_up_periods=2)
    lenient = evaluate_at(candles, end_idx=8, period_days=3, n_blocks=3, min_up_periods=1)
    assert not strict.fired
    assert strict.up_periods == 1
    assert lenient.fired


def test_min_close_vs_last_close_can_disagree():
    # Block 1 dips mid-block then recovers: last_close (15) > block 0's
    # last_close (12), so last_close scoring says "up". But block 1's
    # min_close (9) is BELOW block 0's min_close (10), so min_close
    # scoring says "down" for the same raw data -- this is exactly the
    # behavioural difference the strategy tweak was meant to produce.
    closes = [10, 11, 12,  9, 13, 15]
    candles = make_candles(closes)
    min_close_result = evaluate_at(
        candles, end_idx=5, period_days=3, n_blocks=2, min_up_periods=1, block_value="min_close"
    )
    last_close_result = evaluate_at(
        candles, end_idx=5, period_days=3, n_blocks=2, min_up_periods=1, block_value="last_close"
    )
    assert not min_close_result.fired
    assert last_close_result.fired


def test_insufficient_history_returns_none():
    closes = [10, 11, 12, 13, 14]  # only 5 days, need 9 for 3x3
    candles = make_candles(closes)
    assert evaluate_at(candles, end_idx=4, period_days=3, n_blocks=3, min_up_periods=2) is None


def test_no_lookahead():
    # Data after end_idx is a rising staircase, but a *falling* pattern
    # sits directly before end_idx. If the function looked past end_idx it
    # would fire; it must not, since only data up to and including
    # end_idx is allowed to influence the result.
    closes = [30, 29, 28,  27, 26, 25,  24, 23, 22,   # falling into end_idx=8
              50, 60, 70,  80, 90, 100, 110, 120, 130]  # rises after, must be ignored
    candles = make_candles(closes)
    result = evaluate_at(candles, end_idx=8, period_days=3, n_blocks=3, min_up_periods=2)
    assert not result.fired
    assert result.window_end_idx == 8
    assert max(result.block_end_dates) == candles["timestamp"].iloc[8]


def test_scan_all_evaluates_every_valid_end_idx():
    closes = list(range(10, 10 + 15))  # steadily rising, 15 days
    candles = make_candles(closes)
    results = scan_all(candles, period_days=3, n_blocks=3, min_up_periods=2)
    # First valid end_idx is 8 (0-indexed, 9 days needed), last is 14.
    assert [r.window_end_idx for r in results] == list(range(8, 15))
    assert all(r.fired for r in results)


def test_dedupe_consecutive_collapses_runs():
    closes = list(range(10, 10 + 15))  # rising throughout -> fires on every valid day
    candles = make_candles(closes)
    results = scan_all(candles, period_days=3, n_blocks=3, min_up_periods=2)
    deduped = dedupe_consecutive(results)
    assert len(deduped) == 1
    assert deduped[0].window_end_idx == 8  # the first day it fired


def test_dedupe_consecutive_keeps_separate_non_adjacent_runs():
    # Fires, breaks (a down day resets the staircase), fires again later.
    closes = [10, 11, 12,  13, 14, 15,  16, 17, 18,   # fires at end_idx=8
              5,                                      # a crash resets things
              6, 7, 8,  9, 10, 11,  12, 13, 14]       # a fresh staircase forms later
    candles = make_candles(closes)
    results = scan_all(candles, period_days=3, n_blocks=3, min_up_periods=2)
    deduped = dedupe_consecutive(results)
    fired_idxs = [r.window_end_idx for r in deduped]
    assert len(fired_idxs) >= 2
    assert fired_idxs == sorted(set(fired_idxs))


def test_unknown_block_value_raises():
    candles = make_candles([10, 11, 12, 13, 14, 15])
    with pytest.raises(ValueError):
        evaluate_at(candles, end_idx=5, period_days=3, n_blocks=2, min_up_periods=1, block_value="bogus")


def test_all_block_value_funcs_registered_are_callable():
    closes = pd.Series([5.0, 3.0, 8.0])
    for name, fn in BLOCK_VALUE_FUNCS.items():
        assert isinstance(fn(closes), float | int)
