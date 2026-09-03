"""
Run with: python screener.py

For every symbol in universe.json:
  1. Fetch daily history.
  2. Compute ATR, keep only stocks volatile enough relative to the rest
     of the universe.
  3. Detect validated support levels (pivot clusters with few breaches).
  4. Keep levels price is currently near (ATRx within range).
  5. Backtest each level's historical touches for a forward-return edge.
  6. Score, rank, and write a CSV + print a summary table.

This produces a list to manually review -- it places no orders.
"""

import csv
import logging
import sys
import time as time_module
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect, exceptions as kite_exceptions

from config import CONFIG
from kite_auth import get_kite_session
from indicators import average_true_range_series
from levels import build_levels
from backtest import backtest_level
from report import generate_html_report

logger = logging.getLogger("atrx.screener")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def resolve_tokens(kite: KiteConnect, symbols: list[str]) -> dict[str, int]:
    try:
        instruments = kite.instruments(CONFIG.exchange)
    except kite_exceptions.KiteException as e:
        raise RuntimeError(f"Could not fetch instrument list: {e}") from e

    lookup = {row["tradingsymbol"]: row["instrument_token"] for row in instruments}
    tokens = {}
    for sym in symbols:
        token = lookup.get(sym)
        if token is None:
            logger.warning("Symbol not found on %s, skipping: %s", CONFIG.exchange, sym)
            continue
        tokens[sym] = token
    return tokens


def fetch_history(kite: KiteConnect, token: int, calendar_days: int) -> pd.DataFrame | None:
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
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def analyze_symbol(symbol: str, candles: pd.DataFrame):
    """Returns (atr_pct, candidate_rows) or (None, []) if not enough data."""
    atr_series = average_true_range_series(candles, CONFIG.atr_period)
    candles = candles.assign(atr=atr_series).dropna(subset=["atr"]).reset_index(drop=True)
    if candles.empty:
        return None, []

    current_atr = float(candles["atr"].iloc[-1])
    current_price = float(candles["close"].iloc[-1])
    if current_atr <= 0 or current_price <= 0:
        return None, []

    atr_pct = current_atr / current_price
    levels = build_levels(candles, current_atr)

    rows = []
    for lvl in levels:
        atrx = (current_price - lvl.price) / current_atr
        if not (CONFIG.atrx_lower <= atrx <= CONFIG.atrx_upper):
            continue

        stats = backtest_level(candles, lvl, current_atr)
        rows.append({
            "symbol": symbol,
            "current_price": round(current_price, 2),
            "level": round(lvl.price, 2),
            "atrx": round(atrx, 2),
            "atr_pct": round(atr_pct * 100, 2),
            "touches": lvl.touches,
            "breaches": lvl.breaches,
            "recency_weight": round(lvl.recency_weight, 2),
            "backtest_touches": stats.n_touches,
            "hit_rate_3d_pct": None if stats.hit_rate_short is None else round(stats.hit_rate_short, 1),
            "avg_fwd_ret_3d_pct": None if stats.avg_fwd_ret_short is None else round(stats.avg_fwd_ret_short, 2),
            "hit_rate_5d_pct": None if stats.hit_rate_long is None else round(stats.hit_rate_long, 1),
            "avg_fwd_ret_5d_pct": None if stats.avg_fwd_ret_long is None else round(stats.avg_fwd_ret_long, 2),
        })

    return atr_pct, rows


def score_candidates(rows: list[dict]) -> list[dict]:
    """Adds a composite `score` field and returns rows sorted best-first."""
    if not rows:
        return rows

    returns = np.array([r["avg_fwd_ret_5d_pct"] if r["avg_fwd_ret_5d_pct"] is not None else 0.0
                         for r in rows])
    ret_mean, ret_std = returns.mean(), returns.std() or 1.0
    recency_vals = np.array([r["recency_weight"] for r in rows])
    max_recency = recency_vals.max() or 1.0

    for r, ret in zip(rows, returns):
        ret_z = (ret - ret_mean) / ret_std
        hit_rate = (r["hit_rate_5d_pct"] or 0) / 100
        proximity_component = 1 - (abs(r["atrx"]) / max(abs(CONFIG.atrx_lower), CONFIG.atrx_upper))
        recency_component = r["recency_weight"] / max_recency

        r["score"] = round(
            CONFIG.score_w_return * ret_z * hit_rate
            + CONFIG.score_w_proximity * proximity_component
            + CONFIG.score_w_recency * recency_component
            - CONFIG.score_breach_penalty * r["breaches"],
            3,
        )

    return sorted(rows, key=lambda r: r["score"], reverse=True)


def main():
    setup_logging()

    try:
        symbols = CONFIG.load_universe()
    except (FileNotFoundError, ValueError) as e:
        logger.critical("Could not load universe: %s", e)
        sys.exit(1)

    try:
        kite = get_kite_session()
    except RuntimeError as e:
        logger.critical("Authentication failed: %s", e)
        sys.exit(1)

    tokens = resolve_tokens(kite, symbols)
    if not tokens:
        logger.critical("No valid symbols to analyze.")
        sys.exit(1)

    # Fetch enough calendar days to cover the lookback window plus the
    # confirmation buffer pivot detection and the forward-return backtest
    # both need at their edges.
    buffer_days = CONFIG.pivot_window + CONFIG.atr_period + CONFIG.forward_days_long + 15
    calendar_days = int((CONFIG.lookback_days + buffer_days) * 1.6)  # trading days -> calendar days

    history: dict[str, pd.DataFrame] = {}
    for sym, token in tokens.items():
        df = fetch_history(kite, token, calendar_days)
        if df is not None:
            history[sym] = df
        time_module.sleep(1.0 / 3.0)  # respect Kite's 3 req/sec historical limit

    if not history:
        logger.critical("Could not fetch history for any symbol.")
        sys.exit(1)

    # Pass 1: current ATR% for every symbol, to define "volatile" relative
    # to this universe rather than with an arbitrary fixed number.
    atr_pcts = {}
    for sym, candles in history.items():
        atr_pct, _ = analyze_symbol(sym, candles)
        if atr_pct is not None:
            atr_pcts[sym] = atr_pct

    if not atr_pcts:
        logger.critical("Could not compute ATR for any symbol.")
        sys.exit(1)

    threshold = np.percentile(list(atr_pcts.values()), CONFIG.min_atr_percentile)
    volatile_symbols = [s for s, v in atr_pcts.items() if v >= threshold]
    logger.info(
        "%d/%d symbols pass the volatility filter (ATR%% >= %.2f%%, the %.0fth percentile).",
        len(volatile_symbols), len(atr_pcts), threshold * 100, CONFIG.min_atr_percentile,
    )

    # Pass 2: full level detection + backtest, volatile symbols only.
    all_rows = []
    for sym in volatile_symbols:
        _, rows = analyze_symbol(sym, history[sym])
        all_rows.extend(rows)

    ranked_all = score_candidates(all_rows)

    if not ranked_all:
        logger.info("No candidates matched all filters today.")
        return

    CONFIG.output_dir.mkdir(parents=True, exist_ok=True)

    # CSV keeps the trimmed top_n -- a quick, ready-to-open shortlist.
    ranked_top = ranked_all[:CONFIG.top_n]
    out_file = CONFIG.output_dir / f"atrx_{datetime.now():%Y%m%d_%H%M}.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(ranked_top[0].keys()))
        writer.writeheader()
        writer.writerows(ranked_top)

    # HTML report gets EVERY candidate that passed the filters -- since
    # the whole point is to let you sort/filter it yourself.
    try:
        html_path = generate_html_report(
            ranked_all, universe_size=len(symbols), volatile_count=len(volatile_symbols)
        )
        print(f"\n{len(ranked_all)} candidates -> HTML report: {html_path}")
    except OSError:
        logger.error("HTML report generation failed; CSV is still available.")

    print(f"{len(ranked_top)} candidates written to {out_file}\n")
    header = ("SYMBOL", "PRICE", "LEVEL", "ATRx", "TOUCHES", "BREACH",
              "HIT%5D", "AVGRET%5D", "SCORE")
    print("{:<12}{:>10}{:>10}{:>7}{:>9}{:>7}{:>8}{:>11}{:>8}".format(*header))
    for r in ranked_top:
        print("{:<12}{:>10}{:>10}{:>7}{:>9}{:>7}{:>8}{:>11}{:>8}".format(
            r["symbol"], r["current_price"], r["level"], r["atrx"],
            r["touches"], r["breaches"],
            r["hit_rate_5d_pct"] if r["hit_rate_5d_pct"] is not None else "-",
            r["avg_fwd_ret_5d_pct"] if r["avg_fwd_ret_5d_pct"] is not None else "-",
            r["score"],
        ))


if __name__ == "__main__":
    main()
