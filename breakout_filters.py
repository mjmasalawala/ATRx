"""
Tradability/quality filters applied to a candidate signal window. Each
filter is a standalone function taking the candle slice for the signal
window (oldest-to-newest, ending on the signal day) plus the config, and
returning a (passed: bool, reason: str | None) pair -- `reason` is a
human-readable explanation used only when passed is False, mirroring
screener.py's `_rejection_reasons` pattern so "why wasn't this a
candidate" is always explainable from the breakdown table.

Kept independent of breakout_signal.py: the signal function only decides
whether the staircase pattern holds; these decide whether a stock showing
that pattern is actually tradable/worth counting.
"""

import pandas as pd

from breakout_config import BreakoutConfig


def _window_slice(candles: pd.DataFrame, window_start_idx: int, window_end_idx: int) -> pd.DataFrame:
    return candles.iloc[window_start_idx : window_end_idx + 1]


def filter_min_price(window: pd.DataFrame, cfg: BreakoutConfig) -> tuple[bool, str | None]:
    price = float(window["close"].iloc[-1])
    if price < cfg.min_price:
        return False, f"price ₹{price:.2f} is below the ₹{cfg.min_price:.0f} minimum"
    return True, None


def filter_min_avg_turnover(window: pd.DataFrame, cfg: BreakoutConfig) -> tuple[bool, str | None]:
    if "volume" not in window.columns:
        return True, None  # can't evaluate without volume data; don't block on it
    turnover_cr = (window["close"] * window["volume"]).mean() / 1e7
    if turnover_cr < cfg.min_avg_turnover_cr:
        return False, f"avg daily turnover ₹{turnover_cr:.2f}cr is below the ₹{cfg.min_avg_turnover_cr:.0f}cr minimum"
    return True, None


def filter_circuit_days(window: pd.DataFrame, cfg: BreakoutConfig) -> tuple[bool, str | None]:
    """
    A rough proxy for "locked in a circuit, couldn't actually be filled":
    a day where close == high (no selling pressure could push it down)
    AND the move from the prior close is >= 4.5% (near a 5% circuit band
    without needing to know the stock's actual band, which Kite doesn't
    expose directly).
    """
    if len(window) < 2:
        return True, None
    closes = window["close"]
    highs = window["high"]
    prev_closes = closes.shift(1)
    pct_move = (closes - prev_closes) / prev_closes
    circuit_like = (closes.round(2) == highs.round(2)) & (pct_move.abs() >= 0.045)
    n_circuit_days = int(circuit_like.sum())
    if n_circuit_days > cfg.max_circuit_days:
        return False, f"{n_circuit_days} likely circuit-locked day(s) in the window (max {cfg.max_circuit_days} allowed)"
    return True, None


def filter_window_gain(window: pd.DataFrame, cfg: BreakoutConfig) -> tuple[bool, str | None]:
    first_close = float(window["close"].iloc[0])
    last_close = float(window["close"].iloc[-1])
    if first_close <= 0:
        return False, "invalid window start price"
    gain_pct = (last_close - first_close) / first_close * 100
    if gain_pct < cfg.min_window_gain_pct:
        return False, f"window gain {gain_pct:.1f}% is below the {cfg.min_window_gain_pct:.0f}% minimum"
    if gain_pct > cfg.max_window_gain_pct:
        return False, f"window gain {gain_pct:.1f}% exceeds the {cfg.max_window_gain_pct:.0f}% cap (looks parabolic)"
    return True, None


def filter_breakout_confirm(
    candles: pd.DataFrame, window_end_idx: int, cfg: BreakoutConfig
) -> tuple[bool, str | None]:
    """Only applied when cfg.require_breakout_confirm is True: the signal
    day's close must be the highest close of the trailing breakout_high_days
    window (looking only at data up to and including window_end_idx, so no
    lookahead)."""
    if not cfg.require_breakout_confirm:
        return True, None
    start = max(0, window_end_idx - cfg.breakout_high_days + 1)
    lookback_closes = candles["close"].iloc[start : window_end_idx + 1]
    signal_close = float(candles["close"].iloc[window_end_idx])
    if signal_close < float(lookback_closes.max()):
        return False, f"close is not a new {cfg.breakout_high_days}-day high (breakout confirmation required)"
    return True, None


# Filters that only need the window slice, run in this order (first
# failure wins -- callers should still record every reason if they want
# the full picture, but stopping at the first is fine for the live scan).
WINDOW_FILTERS = (
    filter_min_price,
    filter_min_avg_turnover,
    filter_circuit_days,
    filter_window_gain,
)


def apply_filters(
    candles: pd.DataFrame,
    window_start_idx: int,
    window_end_idx: int,
    cfg: BreakoutConfig,
) -> list[str]:
    """Runs every filter and returns the list of failure reasons (empty
    list == passed everything)."""
    window = _window_slice(candles, window_start_idx, window_end_idx)
    reasons = []
    for fn in WINDOW_FILTERS:
        passed, reason = fn(window, cfg)
        if not passed:
            reasons.append(reason)
    passed, reason = filter_breakout_confirm(candles, window_end_idx, cfg)
    if not passed:
        reasons.append(reason)
    return reasons
