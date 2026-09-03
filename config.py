"""
Configuration for the ATRx screener.

This is a scan-and-review tool, not an execution engine: it produces a
ranked list for you to look at and manually decide on, it never places
orders. All the tunable numbers live here so the detection/scoring logic
never needs to change when you want to experiment with different settings.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("atrx.config")


@dataclass
class Config:
    # --- Broker / auth ---
    api_key: str = os.getenv("KITE_API_KEY", "")
    api_secret: str = os.getenv("KITE_API_SECRET", "")
    access_token_file: Path = BASE_DIR / "state" / "access_token.txt"
    exchange: str = "NSE"

    # --- Universe ---
    universe_file: Path = BASE_DIR / "universe.json"

    # --- Volatility ---
    atr_period: int = 7          # per your feedback: ATR7, not ATR14

    # --- Support-level detection ---
    pivot_window: int = 5        # w: a day is a pivot low if its low is the
                                  # minimum among the w days before AND after it
    lookback_days: int = 60      # how far back (calendar days of trading
                                  # history) to look for pivots and breaches
    cluster_atr_multiple: float = 1.0   # pivots within this many ATRs of
                                  # each other are treated as the same level
    breach_buffer_atr: float = 0.25     # a close must be at least this many
                                  # ATRs below the level to count as a real
                                  # breach (filters out negligible undershoots)
    min_touches: int = 3         # minimum pivots in a cluster to call it a
                                  # validated level
    max_breaches: int = 1        # maximum closing breaches tolerated
    recency_decay_days: float = 60.0    # half-life-style decay constant for
                                  # weighting older touches less

    # --- "Near the level" filter (this is ATRx) ---
    atrx_lower: float = -0.3     # allow a small poke below the level...
    atrx_upper: float = 1.0      # ...but not too far above it either

    # --- Historical touch backtest ---
    touch_band_atr: float = 0.5  # a close within this many ATRs of the level
                                  # counts as a "touch" for the backtest, even
                                  # if it wasn't a confirmed pivot low
    forward_days_short: int = 3
    forward_days_long: int = 5

    # --- Volatility pre-filter ---
    min_atr_percentile: float = 50.0    # only keep stocks whose current
                                  # ATR% ranks at/above this percentile
                                  # within your universe (i.e. "volatile"
                                  # is defined relative to your own list)

    # --- Ranking score weights ---
    # Combined into: score = w_return*ret_component*hit_rate
    #                      + w_proximity*proximity_component
    #                      + w_recency*recency_component
    #                      - breach_penalty*breaches
    # All three components are normalized to roughly 0-1 across the
    # candidate list before weighting, so these weights are directly
    # comparable to each other.
    score_w_return: float = 0.5      # historical forward-return edge
    score_w_proximity: float = 0.3   # how close to the level right now
    score_w_recency: float = 0.2     # how recently the level was validated
    score_breach_penalty: float = 0.15  # subtracted per closing breach

    # --- Output ---
    output_dir: Path = BASE_DIR / "output"
    top_n: int = 25

    def load_universe(self, tier: str = "large_cap") -> list[str]:
        # Universe now lives in Neon (screener_universes, one row per
        # market-cap tier) so it can be updated from the web deployment;
        # universe.json is kept only as a fallback for local dev without
        # DATABASE_URL configured -- and only for the default tier, since
        # it predates tiering and only ever held the large-cap list.
        try:
            import db_store
            symbols = db_store.load_universe(tier)
            if symbols:
                return [s.upper().strip() for s in symbols]
        except Exception as e:
            logger.warning("Could not load universe (tier=%s) from DB, falling back to file: %s", tier, e)

        if tier != "large_cap":
            raise RuntimeError(
                f"No universe found for tier '{tier}' -- the database is unavailable or has no row "
                "for this tier, and there's no local-file fallback for non-default tiers."
            )

        import json
        try:
            with open(self.universe_file, "r") as f:
                data = json.load(f)
            symbols = data.get("symbols", [])
            if not symbols:
                raise ValueError("universe.json has no symbols")
            return [s.upper().strip() for s in symbols]
        except FileNotFoundError:
            raise FileNotFoundError(
                f"{self.universe_file} not found, and no universe is set in the database. "
                'Create it with your trading universe, e.g. {"symbols": ["RELIANCE", "TCS"]}'
            )
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"universe.json is invalid: {e}")

    def to_tunable_dict(self) -> dict:
        return {f: getattr(self, f) for f in TUNABLE_FIELDS}


# Screening parameters the web UI lets the user review/tweak before a run.
# Deliberately excludes broker/auth/path fields -- those aren't screening
# knobs and shouldn't be user-editable from the browser.
TUNABLE_FIELDS = (
    "atr_period", "pivot_window", "lookback_days", "cluster_atr_multiple",
    "breach_buffer_atr", "min_touches", "max_breaches", "recency_decay_days",
    "atrx_lower", "atrx_upper", "touch_band_atr", "forward_days_short",
    "forward_days_long", "min_atr_percentile", "score_w_return",
    "score_w_proximity", "score_w_recency", "score_breach_penalty", "top_n",
)


CONFIG = Config()
