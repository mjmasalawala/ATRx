"""
Configuration for the breakout ("N-of-M block staircase") screener.

Kept as its own dataclass, separate from config.py's Config -- this is a
different strategy with its own tunables, its own persisted last-used
row (breakout_config, not screener_config), and its own signal function
(breakout_signal.py, not levels.py/backtest.py). Nothing here is read by
the ATRx support-level screener, and nothing there is read by this file.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("atrx.breakout")


@dataclass
class BreakoutConfig:
    # --- Signal (the staircase pattern itself) ---
    period_days: int = 3          # trading days per block
    n_blocks: int = 10            # number of consecutive blocks; lookback
                                   # is always derived as period_days *
                                   # n_blocks -- never entered directly, so
                                   # there's no such thing as a partial block
    min_up_periods: int = 8       # of the (n_blocks - 1) block-to-block
                                   # comparisons, how many must be "up"
    block_value: str = "min_close"  # one of breakout_signal.BLOCK_VALUE_FUNCS
                                   # -- default is the lowest close in the
                                   # block (rising-lows / uptrend-structure
                                   # reading), not the last close

    # --- Data ---
    history_trading_days: int = 180   # how much daily history to fetch
                                       # per symbol from Kite

    # --- Live-scan candidate window ---
    signal_recent_days: int = 3   # a symbol is a "candidate" if the signal
                                   # fired on any of the most recent N days,
                                   # not only strictly today

    # --- In-window backtest ---
    forward_horizons: tuple[int, ...] = (3, 5, 10, 20)   # trading days
                                   # forward to measure return at, for
                                   # every historical signal found in the
                                   # fetched window

    # --- Tradability / quality filters ---
    min_avg_turnover_cr: float = 5.0   # average daily turnover (price *
                                        # volume) over the signal window,
                                        # in crores of rupees
    min_price: float = 20.0
    max_circuit_days: int = 1      # max days in the window where
                                    # close == high AND the move looks
                                    # like a circuit-band lock (proxy for
                                    # "can't actually get filled")
    min_window_gain_pct: float = 5.0   # total close-to-close gain across
                                        # the whole lookback window must be
                                        # at least this much...
    max_window_gain_pct: float = 50.0  # ...and at most this much (too much
                                        # reads as parabolic/exhaustion,
                                        # not a clean staircase)
    require_breakout_confirm: bool = False   # if True, also require the
                                        # signal-day close to be a new
                                        # breakout_high_days-day high
    breakout_high_days: int = 60

    def to_tunable_dict(self) -> dict:
        return {f: getattr(self, f) for f in TUNABLE_FIELDS}

    def signal_config_hash(self) -> str:
        """
        Hash of only the fields that change what counts as a signal
        (not filters/display fields) -- used as the key that groups
        persisted signal events together, so changing a filter setting
        doesn't fragment previously-collected evidence for the same
        underlying signal definition.
        """
        payload = {f: getattr(self, f) for f in SIGNAL_HASH_FIELDS}
        blob = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def lookback_days(self) -> int:
        return self.period_days * self.n_blocks


# Fields the web UI lets the user review/tweak before a run.
TUNABLE_FIELDS = (
    "period_days", "n_blocks", "min_up_periods", "block_value",
    "history_trading_days", "signal_recent_days", "forward_horizons",
    "min_avg_turnover_cr", "min_price", "max_circuit_days",
    "min_window_gain_pct", "max_window_gain_pct",
    "require_breakout_confirm", "breakout_high_days",
)

# The subset of TUNABLE_FIELDS that define "what is a signal" -- see
# signal_config_hash(). Deliberately excludes filters/horizons/display
# knobs, which don't change which days fired, only which fired candidates
# get shown or how forward returns get sliced afterward.
SIGNAL_HASH_FIELDS = (
    "period_days", "n_blocks", "min_up_periods", "block_value",
)


def config_from_dict(data: dict) -> "BreakoutConfig":
    """Builds a BreakoutConfig from a persisted/posted dict, ignoring any
    unknown keys and falling back to defaults for anything missing."""
    kwargs = {}
    for f in TUNABLE_FIELDS:
        if f in data:
            value = data[f]
            if f == "forward_horizons" and isinstance(value, list):
                value = tuple(value)
            kwargs[f] = value
    return BreakoutConfig(**kwargs)


CONFIG = BreakoutConfig()
