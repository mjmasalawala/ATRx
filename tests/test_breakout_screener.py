"""
Smoke tests for the orchestration logic in breakout_screener.py, using
synthetic candle frames -- no Kite session, no DB. Exercises
build_symbol_breakdown end-to-end (signal + filters + control returns)
without touching network or persistence.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from breakout_config import BreakoutConfig
from breakout_screener import _build_symbol_events, build_symbol_breakdown


def make_candles(closes, volumes=None, highs=None):
    n = len(closes)
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "timestamp": dates,
        "open": closes,
        "high": highs if highs is not None else closes,
        "low": closes,
        "close": closes,
        "volume": volumes if volumes is not None else [2_000_000] * n,
    })


SMALL_CFG = BreakoutConfig(period_days=3, n_blocks=3, min_up_periods=2, history_trading_days=20)


def test_build_symbol_breakdown_flags_candidate():
    # Rising staircase for the whole window, ends right at the last day ->
    # should be flagged a live candidate (assuming it clears the filters).
    closes = [100 + i for i in range(9)]  # 9 days, exactly one window
    candles = make_candles(closes)
    history = {"TEST": candles}
    row = build_symbol_breakdown("TEST", candles, history, SMALL_CFG)
    assert row["status"] == "candidate"
    assert row["structural_stop"] == 106.0  # min_close of the last block [106,107,108]


def test_build_symbol_breakdown_insufficient_data():
    candles = make_candles([100, 101, 102])
    history = {"TEST": candles}
    row = build_symbol_breakdown("TEST", candles, history, SMALL_CFG)
    assert row["status"] == "insufficient_data"


def test_build_symbol_breakdown_no_signal_when_falling():
    closes = [100 - i for i in range(9)]
    candles = make_candles(closes)
    history = {"TEST": candles}
    row = build_symbol_breakdown("TEST", candles, history, SMALL_CFG)
    assert row["status"] == "no_signal"


def test_build_symbol_breakdown_filtered_out_on_low_price():
    closes = [10 + i * 0.5 for i in range(9)]  # rising, but under min_price
    candles = make_candles(closes)
    history = {"TEST": candles}
    cfg = BreakoutConfig(period_days=3, n_blocks=3, min_up_periods=2, min_price=50)
    row = build_symbol_breakdown("TEST", candles, history, cfg)
    assert row["status"] == "filtered_out"
    assert any("price" in r for r in row["reasons"])


def test_events_include_forward_and_control_returns():
    # 12 days: signal fires at idx 8 (first full 3x3 window), then 3 more
    # days of history exist to compute the 3-day forward return.
    closes = [100 + i for i in range(12)]
    candles = make_candles(closes)
    other_closes = [50 + 0.5 * i for i in range(12)]  # flat-ish control series
    other = make_candles(other_closes)
    history = {"TEST": candles, "OTHER": other}
    cfg = BreakoutConfig(period_days=3, n_blocks=3, min_up_periods=2, forward_horizons=(3,))
    events = _build_symbol_events("TEST", candles, history, cfg)
    assert len(events) >= 1
    first = events[0]
    assert first["fwd_ret_3d"] is not None
    assert first["control_ret_3d"] is not None
