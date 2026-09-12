"""
Orchestrates one breakout-screener run: fetch history for a universe tier,
run the staircase signal + filters across every symbol, build the live
candidate list, run the in-window backtest, persist signal events to
Neon, and return everything the web page needs to render.

Mirrors screener.py's shape (resolve_tokens/fetch_history/rate limiting/
per-symbol breakdown/config-persistence) deliberately, but is otherwise
fully independent -- different config object, different signal logic,
different persisted tables.
"""

import logging
import time as time_module
from contextlib import contextmanager
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect, exceptions as kite_exceptions

from breakout_config import CONFIG, TUNABLE_FIELDS, BreakoutConfig
from breakout_filters import apply_filters
from breakout_signal import dedupe_consecutive, evaluate_at, scan_all

logger = logging.getLogger("atrx.breakout")

# Same 3 req/sec Kite historical-data limit the ATRx screener rate-limits
# against -- see screener.py's _run_screener for why request *starts* are
# spaced rather than a flat sleep-after being used.
_MIN_REQUEST_INTERVAL = 1.0 / 3.0

STATUS_ORDER = {
    "candidate": 0, "signal_in_window": 1, "no_signal": 2,
    "filtered_out": 3, "insufficient_data": 4, "fetch_failed": 5, "symbol_not_found": 6,
}


@contextmanager
def _config_overrides(overrides: dict | None):
    """Same pattern as screener.py's _config_overrides -- temporarily
    mutates the module-level CONFIG singleton for the duration of one run,
    restored after (even on error)."""
    original = {}
    if overrides:
        for key, value in overrides.items():
            if key not in TUNABLE_FIELDS:
                raise ValueError(f"Unknown breakout config parameter: {key}")
            original[key] = getattr(CONFIG, key)
            if key == "forward_horizons" and isinstance(value, list):
                value = tuple(value)
            setattr(CONFIG, key, value)
    try:
        yield
    finally:
        for key, value in original.items():
            setattr(CONFIG, key, value)


def resolve_tokens(kite: KiteConnect, symbols: list[str], exchange: str = "NSE") -> dict[str, int]:
    try:
        instruments = kite.instruments(exchange)
    except kite_exceptions.KiteException as e:
        raise RuntimeError(f"Could not fetch instrument list: {e}") from e

    lookup = {row["tradingsymbol"]: row["instrument_token"] for row in instruments}
    tokens = {}
    for sym in symbols:
        token = lookup.get(sym)
        if token is None:
            logger.warning("Symbol not found on %s, skipping: %s", exchange, sym)
            continue
        tokens[sym] = token
    return tokens


def fetch_history(kite: KiteConnect, token: int, trading_days: int) -> pd.DataFrame | None:
    # Trading days -> calendar days: weekends/holidays mean ~1.45x buffer
    # comfortably covers it without over-fetching by much.
    calendar_days = int(trading_days * 1.45) + 10
    end = datetime.now()
    start = end - timedelta(days=calendar_days)
    try:
        raw = kite.historical_data(
            instrument_token=token, from_date=start, to_date=end, interval="day"
        )
    except kite_exceptions.KiteException as e:
        logger.error("Historical data fetch failed: %s", e)
        return None
    except Exception as e:
        logger.error("Unexpected error fetching history: %s", e)
        return None

    df = pd.DataFrame(raw)
    if df.empty:
        return None
    df = df.rename(columns={"date": "timestamp"})
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    # Keep only the trailing `trading_days` rows -- the calendar-day buffer
    # above intentionally overshoots, so trim back to an exact, comparable
    # window across symbols regardless of each one's own holiday pattern.
    if len(df) > trading_days:
        df = df.iloc[-trading_days:].reset_index(drop=True)
    return df


def fetch_universe_history(
    kite: KiteConnect, tokens: dict[str, int], trading_days: int
) -> dict[str, pd.DataFrame]:
    history: dict[str, pd.DataFrame] = {}
    next_request_at = time_module.monotonic()
    for sym, token in tokens.items():
        wait = next_request_at - time_module.monotonic()
        if wait > 0:
            time_module.sleep(wait)
        next_request_at = time_module.monotonic() + _MIN_REQUEST_INTERVAL

        df = fetch_history(kite, token, trading_days)
        if df is not None:
            history[sym] = df
    return history


def _pct_return(candles: pd.DataFrame, from_idx: int, to_idx: int) -> float | None:
    if to_idx >= len(candles):
        return None
    entry = float(candles["close"].iloc[from_idx])
    exit_ = float(candles["close"].iloc[to_idx])
    if entry <= 0:
        return None
    return (exit_ - entry) / entry * 100


def _control_return(history: dict[str, pd.DataFrame], signal_idx: int, horizon: int) -> float | None:
    """Equal-weighted mean return of every OTHER fetched symbol in the
    same tier, over the same positional window -- needs no extra data
    since every symbol was fetched with the same trailing window length."""
    rets = []
    for sym, candles in history.items():
        to_idx = signal_idx + horizon
        if to_idx < len(candles) and signal_idx < len(candles):
            r = _pct_return(candles, signal_idx, to_idx)
            if r is not None:
                rets.append(r)
    return float(np.mean(rets)) if rets else None


def _build_symbol_events(
    symbol: str, candles: pd.DataFrame, history: dict[str, pd.DataFrame], cfg: BreakoutConfig
) -> list[dict]:
    """Every historical firing of the signal for one symbol, deduped, with
    forward/control returns computed wherever the horizon has elapsed
    within the fetched window, and filter reasons recorded either way."""
    results = scan_all(candles, cfg.period_days, cfg.n_blocks, cfg.min_up_periods, cfg.block_value)
    fired = dedupe_consecutive(results)

    events = []
    for r in fired:
        signal_idx = r.window_end_idx
        signal_date = candles["timestamp"].iloc[signal_idx]
        entry_price = float(candles["close"].iloc[signal_idx])
        window_gain_pct = (
            (r.block_values[-1] - r.block_values[0]) / r.block_values[0] * 100
            if r.block_values[0] else None
        )
        reasons = apply_filters(candles, r.window_start_idx, r.window_end_idx, cfg)

        event = {
            "symbol": symbol,
            "signal_date": signal_date.date() if hasattr(signal_date, "date") else signal_date,
            "entry_price": entry_price,
            "window_gain_pct": window_gain_pct,
            "up_periods": r.up_periods,
            "n_blocks": r.n_blocks,
            "block_values": r.block_values,
            "filter_reasons": reasons,
            "_signal_idx": signal_idx,  # stripped before persistence, used below
        }
        for h in cfg.forward_horizons:
            event[f"fwd_ret_{h}d"] = _pct_return(candles, signal_idx, signal_idx + h)
            event[f"control_ret_{h}d"] = _control_return(history, signal_idx, h)
        events.append(event)
    return events


def build_symbol_breakdown(
    symbol: str, candles: pd.DataFrame, history: dict[str, pd.DataFrame], cfg: BreakoutConfig
) -> dict:
    """One row per symbol for the results table: current signal state,
    latest block breakdown, structural stop, and this symbol's own
    historical events found in the fetched window."""
    window_len = cfg.period_days * cfg.n_blocks
    if len(candles) < window_len:
        return {
            "symbol": symbol, "status": "insufficient_data",
            "reasons": [f"only {len(candles)} trading day(s) of history, need {window_len}"],
            "events": [],
        }

    events = _build_symbol_events(symbol, candles, history, cfg)
    latest_idx = len(candles) - 1
    latest_result = evaluate_at(candles, latest_idx, cfg.period_days, cfg.n_blocks, cfg.min_up_periods, cfg.block_value)

    current_price = float(candles["close"].iloc[-1])
    structural_stop = latest_result.block_values[-1] if latest_result else None

    recent_fired_events = [
        e for e in events
        if e["_signal_idx"] >= latest_idx - cfg.signal_recent_days + 1
    ]

    if recent_fired_events:
        most_recent = max(recent_fired_events, key=lambda e: e["_signal_idx"])
        is_candidate = not most_recent["filter_reasons"]
        status = "candidate" if is_candidate else "filtered_out"
        reasons = most_recent["filter_reasons"]
    elif latest_result and latest_result.fired:
        # Fired but outside the "recent" window used for the candidate tag
        # (can happen right at the boundary) -- surfaced, not hidden.
        status, reasons = "signal_in_window", []
    else:
        status, reasons = "no_signal", []

    for e in events:
        e.pop("_signal_idx", None)

    return {
        "symbol": symbol,
        "status": status,
        "reasons": reasons,
        "current_price": current_price,
        "up_periods": latest_result.up_periods if latest_result else None,
        "required_up_periods": cfg.min_up_periods,
        "block_values": latest_result.block_values if latest_result else [],
        "structural_stop": structural_stop,
        "distance_to_stop_pct": (
            (current_price - structural_stop) / structural_stop * 100
            if structural_stop else None
        ),
        "events": events,
        "n_historical_signals": len(events),
    }


def _persist_config_used(cfg: BreakoutConfig) -> None:
    try:
        import db_store
        db_store.save_breakout_config(cfg.to_tunable_dict())
    except Exception as e:
        logger.warning("Could not persist breakout config to DB: %s", e)


def _persist_events(breakdown: list[dict], universe_tier: str, cfg: BreakoutConfig) -> tuple[int, str | None]:
    signal_hash = cfg.signal_config_hash()
    rows = []
    for row in breakdown:
        for e in row.get("events", []):
            rows.append({
                "symbol": row["symbol"],
                "signal_date": e["signal_date"],
                "signal_config_hash": signal_hash,
                "universe_tier": universe_tier,
                **e,
            })
    if not rows:
        return 0, None
    try:
        import db_store
        n = db_store.upsert_signal_events(rows)
        return n, None
    except Exception as e:
        logger.warning("Could not persist breakout signal events to DB: %s", e)
        return 0, str(e)


def run_breakout_screener(
    kite: KiteConnect, overrides: dict | None = None, universe_tier: str = "large_cap"
) -> dict:
    """Full breakout-screener run against an authenticated Kite session.
    Returns data only (no file I/O), reusable from the CLI or the web API."""
    with _config_overrides(overrides):
        result = _run_breakout_screener(kite, universe_tier)
        _persist_config_used(CONFIG)
        return result


def _run_breakout_screener(kite: KiteConnect, universe_tier: str) -> dict:
    import db_store

    symbols = db_store.load_universe(universe_tier)
    if not symbols:
        raise RuntimeError(f"No universe found for tier '{universe_tier}'.")

    tokens = resolve_tokens(kite, symbols)
    if not tokens:
        raise RuntimeError("No valid symbols to analyze.")

    history = fetch_universe_history(kite, tokens, CONFIG.history_trading_days)
    if not history:
        raise RuntimeError("Could not fetch history for any symbol.")

    breakdown = []
    for sym in tokens:
        if sym not in history:
            breakdown.append({"symbol": sym, "status": "fetch_failed", "reasons": [], "events": []})
            continue
        breakdown.append(build_symbol_breakdown(sym, history[sym], history, CONFIG))

    for sym in symbols:
        if sym not in tokens:
            breakdown.append({"symbol": sym, "status": "symbol_not_found", "reasons": [], "events": []})

    n_persisted, persist_error = _persist_events(breakdown, universe_tier, CONFIG)

    breakdown.sort(key=lambda r: STATUS_ORDER.get(r["status"], 99))

    return {
        "generated_at": datetime.now().isoformat(),
        "universe_tier": universe_tier,
        "universe_size": len(symbols),
        "fetched_count": len(history),
        "signal_config_hash": CONFIG.signal_config_hash(),
        "config_used": CONFIG.to_tunable_dict(),
        "n_events_this_run": sum(len(r.get("events", [])) for r in breakdown),
        "n_events_persisted": n_persisted,
        "persist_error": persist_error,
        "breakdown": breakdown,
    }
