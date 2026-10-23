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
    target_round_trips: int = 4         # "done for the day" once this many round trips are closed...
    max_tickers: int = 3                # ...across (up to) this many tickers

    # --- Risk limits (see the "High Volatility Strategy Risk Review" doc) ---
    # 1R = the planned loss on one trade = capital x risk_pct. The day/week/
    # month loss limits are % of capital, so their R value doubles while
    # risk_pct is 0.25 (the first 20 trades).
    capital: float = 200000.0
    risk_pct: float = 0.25              # 1R as % of capital (0.5 once 20 trades are in)
    daily_loss_pct: float = 2.0         # flatten and stop for the day
    weekly_loss_pct: float = 4.0        # half size for the next week
    monthly_dd_pct: float = 6.0         # stop live trading, review the journal
    giveback_trigger_r: float = 4.0     # once the day reaches +this R...
    giveback_frac: float = 0.5          # ...giving back this share of the peak ends the day
    max_open_positions: int = 2
    max_correlated_r: float = 1.5       # combined risk of same-direction open positions
    gross_exposure_x: float = 2.0       # total notional <= capital x this
    liquidity_pct: float = 1.0          # position <= this % of the avg 5-min traded value
    cost_pct: float = 0.2               # round-trip charges + slippage assumed in sizing / R:R
    min_net_rr: float = 1.2
    max_trips_per_stock: int = 4
    max_trips_per_day: int = 8
    cooloff_after_losses: int = 2       # this many losses in a row -> a break...
    cooloff_minutes: int = 10
    stop_after_losses: int = 3          # ...this many -> done for the day
    entry_start: str = "09:20"
    entry_end: str = "10:15"
    flat_by: str = "10:45"
    time_stop_minutes: int = 30         # exit if not at +0.5R within this
    band_no_entry_pct: float = 3.0      # no entries this close to a circuit limit
    band_exit_pct: float = 1.5          # exit at once this close to it
    min_band_pct: float = 5.0           # stocks with a price band this narrow are never traded
    max_spread_pct: float = 0.3
    event_size_mult: float = 0.5        # size on a results / news day
    longs_only: bool = True

    # --- Setup detection (long side) ---
    stretch_sigma: float = 1.5          # price at least this many VWAP bands below VWAP...
    stretch_atr: float = 0.4            # ...or this many daily ATRs below the session high
    rsi_period: int = 5
    rsi_max: float = 20.0
    rsi_exit: float = 70.0
    volume_spike_x: float = 2.0         # vs the stock's normal volume at that time of day
    wick_frac: float = 0.5              # lower wick >= this share of the bar's range
    level_atr: float = 0.1              # "at a level" = within this many ATRs of one
    gap_atr: float = 0.7                # gap down bigger than this -> wait for a VWAP reclaim
    entry_valid_bars: int = 2           # an entry suggestion lapses after this many 1-min bars
    strip_size: int = 6                 # shortlist stocks scanned besides the focus list

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
    poll_seconds: int = 30
    target_pcts: tuple = (1.0, 2.0, 3.0)   # net-of-charges profit targets shown on each live card
    dip_alert_pct: float = 1.0             # browser alert when price is this far below the open
    bounce_alert_pct: float = 1.0          # ...or this far above the day's low

    def to_dict(self) -> dict:
        d = asdict(self)
        d["target_pcts"] = list(self.target_pcts)
        return d

    @property
    def one_r(self) -> float:
        return self.capital * self.risk_pct / 100

    @property
    def daily_loss_limit(self) -> float:
        return self.capital * self.daily_loss_pct / 100


TUNABLE_FIELDS = tuple(f.name for f in fields(HVConfig))

# Locked from 9:15 to 15:30 IST on weekdays: a limit is never moved during
# the session it covers.
RISK_FIELDS = (
    "capital", "risk_pct", "daily_loss_pct", "weekly_loss_pct", "monthly_dd_pct", "giveback_trigger_r",
    "giveback_frac", "max_open_positions", "max_correlated_r", "gross_exposure_x", "liquidity_pct",
    "cost_pct", "min_net_rr", "max_trips_per_stock", "max_trips_per_day", "cooloff_after_losses",
    "cooloff_minutes", "stop_after_losses", "entry_start", "entry_end", "flat_by", "time_stop_minutes",
    "band_no_entry_pct", "band_exit_pct", "min_band_pct", "max_spread_pct", "event_size_mult", "longs_only",
)


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
        elif isinstance(default, str):
            value = str(value)
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
