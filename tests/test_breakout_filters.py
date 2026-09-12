import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from breakout_config import BreakoutConfig
from breakout_filters import apply_filters


def make_window(closes, highs=None, volumes=None):
    n = len(closes)
    highs = highs if highs is not None else closes
    volumes = volumes if volumes is not None else [1_000_000] * n
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"timestamp": dates, "close": closes, "high": highs, "volume": volumes})


def test_clean_staircase_passes_all_filters():
    closes = [100, 102, 105, 108, 112, 115, 118, 120, 124]
    candles = make_window(closes)
    cfg = BreakoutConfig()
    reasons = apply_filters(candles, 0, len(closes) - 1, cfg)
    assert reasons == []


def test_low_price_rejected():
    closes = [5, 6, 7, 8, 9, 10, 11, 12, 13]
    candles = make_window(closes)
    cfg = BreakoutConfig(min_price=20)
    reasons = apply_filters(candles, 0, len(closes) - 1, cfg)
    assert any("price" in r for r in reasons)


def test_excessive_gain_rejected_as_parabolic():
    closes = [100, 120, 140, 160, 180, 200, 220, 240, 260]  # +160%
    candles = make_window(closes)
    cfg = BreakoutConfig(max_window_gain_pct=50)
    reasons = apply_filters(candles, 0, len(closes) - 1, cfg)
    assert any("parabolic" in r for r in reasons)


def test_flat_window_rejected_as_too_little_gain():
    closes = [100, 100.5, 101, 100.8, 101.2, 101, 101.5, 101.3, 101.6]
    candles = make_window(closes)
    cfg = BreakoutConfig(min_window_gain_pct=5)
    reasons = apply_filters(candles, 0, len(closes) - 1, cfg)
    assert any("minimum" in r and "gain" in r for r in reasons)


def test_circuit_like_days_rejected():
    # Day 1: close == high, +5% from prior close -> circuit-like.
    closes = [100, 105, 105.2, 105.5]
    highs = [100, 105, 106, 107]
    candles = make_window(closes, highs=highs)
    cfg = BreakoutConfig(max_circuit_days=0)
    reasons = apply_filters(candles, 0, len(closes) - 1, cfg)
    assert any("circuit" in r for r in reasons)


def test_breakout_confirm_off_by_default_does_not_block():
    # Gentle moves only (well under the circuit-like +-4.5% threshold),
    # and never a new high vs. the window's own start -- would fail a
    # breakout-confirmation check if it were enabled.
    closes = [100, 96, 92, 94, 97, 99, 102, 104, 106]
    candles = make_window(closes)
    cfg = BreakoutConfig()  # require_breakout_confirm defaults to False
    reasons = apply_filters(candles, 0, len(closes) - 1, cfg)
    assert reasons == []
