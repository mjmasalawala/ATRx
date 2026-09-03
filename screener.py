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

from config import CONFIG, TUNABLE_FIELDS
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


def current_atr_and_price(candles: pd.DataFrame) -> tuple[float, float] | None:
    """Returns (current_atr, current_price), or None if there's not enough data."""
    atr_series = average_true_range_series(candles, CONFIG.atr_period)
    candles = candles.assign(atr=atr_series).dropna(subset=["atr"])
    if candles.empty:
        return None
    current_atr = float(candles["atr"].iloc[-1])
    current_price = float(candles["close"].iloc[-1])
    if current_atr <= 0 or current_price <= 0:
        return None
    return current_atr, current_price


def _rejection_reasons(lvl, atrx: float, in_range: bool) -> list[str]:
    reasons = []
    if lvl.touches < CONFIG.min_touches:
        reasons.append(f"only {lvl.touches} touch{'es' if lvl.touches != 1 else ''} (needs ≥{CONFIG.min_touches})")
    if lvl.breaches > CONFIG.max_breaches:
        reasons.append(f"{lvl.breaches} breach{'es' if lvl.breaches != 1 else ''} (max {CONFIG.max_breaches} allowed)")
    if not in_range:
        side = "below" if atrx < CONFIG.atrx_lower else "above"
        reasons.append(
            f"ATRx {atrx:.2f} is {side} the allowed range ({CONFIG.atrx_lower} to {CONFIG.atrx_upper})"
        )
    return reasons


def build_symbol_breakdown(symbol: str, candles: pd.DataFrame) -> dict:
    """
    The full screening picture for one symbol: every clustered level found
    (whether or not it's currently a usable candidate) with its raw pivots,
    proximity, and backtest stats -- not just the level(s) that happened to
    qualify. `score` is filled in later, once score_candidates() has run
    over every symbol's candidate levels together (it's a relative ranking,
    not computable per-level in isolation).
    """
    atr_and_price = current_atr_and_price(candles)
    if atr_and_price is None:
        return {"symbol": symbol, "status": "insufficient_data", "levels": []}
    current_atr, current_price = atr_and_price

    levels = build_levels(candles, current_atr)

    level_rows = []
    for lvl in levels:
        atrx = (current_price - lvl.price) / current_atr
        in_range = CONFIG.atrx_lower <= atrx <= CONFIG.atrx_upper
        is_candidate = lvl.meets_criteria and in_range
        stats = backtest_level(candles, lvl, current_atr)
        level_rows.append({
            "level": round(lvl.price, 2),
            "atr": round(current_atr, 2),
            "atrx": round(atrx, 2),
            "touches": lvl.touches,
            "breaches": lvl.breaches,
            "recency_weight": round(lvl.recency_weight, 2),
            "meets_criteria": lvl.meets_criteria,
            "in_range": in_range,
            "is_candidate": is_candidate,
            "rejection_reasons": [] if is_candidate else _rejection_reasons(lvl, atrx, in_range),
            "backtest_touches": stats.n_touches,
            "hit_rate_3d_pct": None if stats.hit_rate_short is None else round(stats.hit_rate_short, 1),
            "avg_fwd_ret_3d_pct": None if stats.avg_fwd_ret_short is None else round(stats.avg_fwd_ret_short, 2),
            "hit_rate_5d_pct": None if stats.hit_rate_long is None else round(stats.hit_rate_long, 1),
            "avg_fwd_ret_5d_pct": None if stats.avg_fwd_ret_long is None else round(stats.avg_fwd_ret_long, 2),
            "score": None,
            "pivots": [{"date": p["date"].strftime("%Y-%m-%d"), "price": round(p["price"], 2)}
                       for p in lvl.pivots],
        })

    level_rows.sort(key=lambda r: abs(r["atrx"]))

    return {
        "symbol": symbol,
        "status": "screened",
        "current_price": round(current_price, 2),
        "atr": round(current_atr, 2),
        "atr_pct": round((current_atr / current_price) * 100, 2),
        "levels": level_rows,
    }


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


def run_screener(kite: KiteConnect, overrides: dict | None = None) -> dict:
    """
    Runs the full scan against an already-authenticated KiteConnect session
    and returns the results as data (no file I/O, no sys.exit) so it can be
    reused from both the CLI entrypoint and the web API handler.

    `overrides` temporarily replaces CONFIG.TUNABLE_FIELDS values for the
    duration of this call (restored in `finally`) -- lets the web UI's
    config-confirmation step run with user-adjusted parameters without a
    separate per-request Config instance threaded through every function.
    """
    original_config_values = {}
    if overrides:
        for key, value in overrides.items():
            if key not in TUNABLE_FIELDS:
                raise ValueError(f"Unknown config parameter: {key}")
            original_config_values[key] = getattr(CONFIG, key)
            setattr(CONFIG, key, value)

    try:
        result = _run_screener(kite)
        _persist_config_used()
        return result
    finally:
        for key, value in original_config_values.items():
            setattr(CONFIG, key, value)


def _persist_config_used() -> None:
    """
    Saves whatever CONFIG.TUNABLE_FIELDS values were actually used for a
    successful run to Neon, so the next login's config-review panel starts
    from the last-used parameters instead of always config.py's defaults.
    Non-fatal if the DB isn't configured/reachable -- a run must not fail
    just because this bookkeeping step did.
    """
    try:
        import db_store
        db_store.save_config(CONFIG.to_tunable_dict())
    except Exception as e:
        logger.warning("Could not persist config to DB: %s", e)


def _run_screener(kite: KiteConnect) -> dict:
    symbols = CONFIG.load_universe()

    tokens = resolve_tokens(kite, symbols)
    if not tokens:
        raise RuntimeError("No valid symbols to analyze.")

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
        raise RuntimeError("Could not fetch history for any symbol.")

    # Pass 1: current ATR% for every symbol (cheap -- no level detection or
    # backtesting yet), to define "volatile" relative to this universe
    # rather than with an arbitrary fixed number.
    atr_pcts: dict[str, float] = {}
    prices: dict[str, float] = {}
    for sym, candles in history.items():
        result = current_atr_and_price(candles)
        if result is not None:
            current_atr, current_price = result
            atr_pcts[sym] = current_atr / current_price
            prices[sym] = current_price

    if not atr_pcts:
        raise RuntimeError("Could not compute ATR for any symbol.")

    threshold = np.percentile(list(atr_pcts.values()), CONFIG.min_atr_percentile)
    volatile_symbols = {s for s, v in atr_pcts.items() if v >= threshold}
    logger.info(
        "%d/%d symbols pass the volatility filter (ATR%% >= %.2f%%, the %.0fth percentile).",
        len(volatile_symbols), len(atr_pcts), threshold * 100, CONFIG.min_atr_percentile,
    )

    def percentile_rank(sym: str) -> float:
        v = atr_pcts[sym]
        return round(sum(1 for other in atr_pcts.values() if other <= v) / len(atr_pcts) * 100, 1)

    # Every symbol in the universe gets a row in the breakdown, regardless
    # of how far it got -- this is what makes "no candidates" explainable
    # instead of just a dead end.
    screening_breakdown: list[dict] = []
    for sym in symbols:
        if sym not in tokens:
            screening_breakdown.append({"symbol": sym, "status": "symbol_not_found"})
        elif sym not in history:
            screening_breakdown.append({"symbol": sym, "status": "fetch_failed"})
        elif sym not in atr_pcts:
            screening_breakdown.append({"symbol": sym, "status": "insufficient_data"})
        elif sym not in volatile_symbols:
            screening_breakdown.append({
                "symbol": sym,
                "status": "below_volatility_threshold",
                "current_price": round(prices[sym], 2),
                "atr_pct": round(atr_pcts[sym] * 100, 2),
                "atr_percentile_rank": percentile_rank(sym),
            })

    # Pass 2: full level detection + backtest, volatile symbols only -- the
    # expensive part, but it's all local computation (no extra Kite calls),
    # so doing it for every volatile symbol instead of only eventual
    # candidates doesn't meaningfully add to the run's wall-clock time.
    all_rows = []
    for sym in volatile_symbols:
        breakdown = build_symbol_breakdown(sym, history[sym])
        breakdown["atr_percentile_rank"] = percentile_rank(sym)
        screening_breakdown.append(breakdown)

        for lvl in breakdown["levels"]:
            if lvl["is_candidate"]:
                row = {"symbol": sym, "current_price": breakdown["current_price"],
                       "atr_pct": breakdown["atr_pct"]}
                row.update({k: v for k, v in lvl.items()
                            if k not in ("meets_criteria", "in_range", "is_candidate",
                                         "rejection_reasons", "pivots")})
                all_rows.append(row)

    ranked_all = score_candidates(all_rows)

    # score_candidates() only makes sense computed once across every
    # candidate level together (it's a relative ranking), so map the
    # scores it produced back onto the matching levels nested in
    # screening_breakdown, which were built before scores existed.
    score_lookup = {(r["symbol"], r["level"]): r["score"] for r in ranked_all}
    for breakdown in screening_breakdown:
        for lvl in breakdown.get("levels", []):
            if lvl["is_candidate"]:
                lvl["score"] = score_lookup.get((breakdown["symbol"], lvl["level"]))

    # Best candidates first, then screened symbols with no qualifying level
    # (closest miss first), then symbols that never passed volatility.
    STATUS_ORDER = {"screened": 0, "below_volatility_threshold": 1, "insufficient_data": 2,
                     "fetch_failed": 3, "symbol_not_found": 4}

    def sort_key(b: dict):
        candidate_scores = [l["score"] for l in b.get("levels", []) if l["is_candidate"]]
        best_score = max(candidate_scores) if candidate_scores else None
        closest_miss = min((abs(l["atrx"]) for l in b.get("levels", [])), default=999)
        return (
            STATUS_ORDER.get(b["status"], 9),
            0 if best_score is not None else 1,
            -(best_score or 0),
            closest_miss,
        )

    screening_breakdown.sort(key=sort_key)

    return {
        "generated_at": datetime.now().isoformat(),
        "universe_size": len(symbols),
        "volatile_count": len(volatile_symbols),
        "atr_threshold_pct": round(threshold * 100, 2),
        "candidate_count": len(ranked_all),
        "rows": ranked_all,
        "top_rows": ranked_all[:CONFIG.top_n],
        "screening_breakdown": screening_breakdown,
    }


def main():
    setup_logging()

    try:
        kite = get_kite_session()
    except RuntimeError as e:
        logger.critical("Authentication failed: %s", e)
        sys.exit(1)

    try:
        result = run_screener(kite)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        logger.critical(str(e))
        sys.exit(1)

    ranked_all = result["rows"]
    if not ranked_all:
        logger.info("No candidates matched all filters today.")
        return

    CONFIG.output_dir.mkdir(parents=True, exist_ok=True)

    # CSV keeps the trimmed top_n -- a quick, ready-to-open shortlist.
    ranked_top = result["top_rows"]
    out_file = CONFIG.output_dir / f"atrx_{datetime.now():%Y%m%d_%H%M}.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(ranked_top[0].keys()))
        writer.writeheader()
        writer.writerows(ranked_top)

    # HTML report gets EVERY candidate that passed the filters -- since
    # the whole point is to let you sort/filter it yourself.
    try:
        html_path = generate_html_report(
            ranked_all, universe_size=result["universe_size"], volatile_count=result["volatile_count"]
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
