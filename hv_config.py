"""
Configuration for the High Volatility page -- a manual first-hour
trading aid (pre-open watchlist, live monitor, trade journal). Its own
dataclass, separate from config.py / breakout_config.py, with its own
persisted last-used row (hv_settings). Nothing here places orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass
class HVConfig:
    # --- Your trading routine ---
    amount_per_trade: float = 35000.0   # rupees; quantity = floor(amount / price)
    daily_loss_limit: float = 1500.0    # rupees; display-only warning, never blocks anything
    target_round_trips: int = 4         # "done for the day" once this many round trips are closed...
    max_tickers: int = 3                # ...across (up to) this many tickers

    # --- Pre-open shortlist ---
    shortlist_size: int = 40            # top-N symbols by ATR% that get the (slower) intraday analysis
    min_atr_pct: float = 2.0            # ATR% floor (from the latest Find ATRx Opps run)
    min_avg_turnover_cr: float = 5.0    # avg daily turnover floor, crores of rupees
    min_circuit_band_pct: float = 10.0  # drop 2%/5% price-band stocks -- they can't move far enough
    history_calendar_days: int = 25     # how far back the 5-minute candles go (~15 trading days)
    first_hour_minutes: int = 60
    swing_pct: float = 1.5              # a "tradable swing" = a move of at least this many % (zigzag on 5-min closes)

    # --- Ranking weights (normalised components; they only need to be relative) ---
    w_first_hour_range: float = 0.30
    w_two_swing_days: float = 0.30
    w_atr: float = 0.15
    w_whipsaw: float = 0.10
    w_gap: float = 0.10
    w_volume_surge: float = 0.05
    tag_bonus: float = 0.05             # added to the score per pre-open clue ticked (results/news/broker call)

    # --- Live page ---
    poll_seconds: int = 60
    target_pcts: tuple = (1.0, 2.0, 3.0)   # net-of-charges profit targets shown on each live card
    dip_alert_pct: float = 1.0             # browser alert when price is this far below the open
    bounce_alert_pct: float = 1.0          # ...or this far above the day's low

    def to_dict(self) -> dict:
        d = asdict(self)
        d["target_pcts"] = list(self.target_pcts)
        return d


TUNABLE_FIELDS = tuple(f.name for f in fields(HVConfig))


def config_from_dict(overrides: dict | None) -> HVConfig:
    """Defaults + any persisted/overridden values. Unknown keys are
    ignored (a persisted row may predate a field being removed), values
    are coerced to each field's default type."""
    cfg = HVConfig()
    for key, value in (overrides or {}).items():
        if key not in TUNABLE_FIELDS or value is None:
            continue
        default = getattr(cfg, key)
        if isinstance(default, tuple):
            value = tuple(float(v) for v in value)
        elif isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        elif isinstance(default, float):
            value = float(value)
        setattr(cfg, key, value)
    return cfg


# Zerodha equity-intraday (MIS) charges on NSE. These are published rates
# (verify against Zerodha's brokerage calculator -- they get revised, e.g.
# when SEBI/exchange fees change), kept as constants rather than tunables.
CHARGES = {
    "brokerage_pct": 0.03,       # % of turnover per executed order...
    "brokerage_cap": 20.0,       # ...capped at this many rupees per order
    "stt_sell_pct": 0.025,       # securities transaction tax, sell side only
    "exchange_txn_pct": 0.00297,  # NSE transaction charge, both sides
    "sebi_pct": 0.0001,          # Rs 10 per crore, both sides
    "stamp_buy_pct": 0.003,      # buy side only
    "gst_pct": 18.0,             # on brokerage + exchange txn + SEBI
}
