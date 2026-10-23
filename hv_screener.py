"""
Kite/DB plumbing for the High Volatility page's pre-open watchlist:
shortlist from the saved ATR% snapshot, fetch 5-minute candles (cached per
day on the snapshot row), quote the shortlist, rank. The statistics and
ranking themselves live in hv_ranking.py (pure, unit-tested).
"""

from __future__ import annotations

import json
import logging
import re
import time as time_module
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from kiteconnect import KiteConnect

import db_store
from hv_config import HVConfig
from hv_ranking import intraday_stats, rank_candidates
from screener import resolve_tokens

logger = logging.getLogger("atrx.hv")

IST = ZoneInfo("Asia/Kolkata")
EXCHANGE = "NSE"
NIFTY_KEY = "NSE:NIFTY 50"

# Same 3 req/sec Kite historical-data limit as screener.py; request starts
# are spaced rather than sleeping a flat interval after each call.
_MIN_REQUEST_INTERVAL = 1.0 / 3.0
# Vercel Hobby kills a function at 60s. Stop fetching new candles a bit
# before that and report what's left -- results are cached per symbol, so
# pressing the button again simply continues where this call stopped.
_TIME_BUDGET_SECONDS = 42


def today_ist() -> date:
    return datetime.now(IST).date()


def fetch_5min(kite: KiteConnect, token: int, calendar_days: int) -> pd.DataFrame | None:
    end = datetime.now(IST).replace(tzinfo=None)
    start = end - timedelta(days=calendar_days)
    try:
        raw = kite.historical_data(instrument_token=token, from_date=start, to_date=end,
                                   interval="5minute")
    except Exception as e:
        logger.error("5-minute fetch failed: %s", e)
        return None
    df = pd.DataFrame(raw)
    if df.empty:
        return None
    df = df.rename(columns={"date": "timestamp"})
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


GIFT_NIFTY_URL = "https://groww.in/indices/global-indices/sgx-nifty"


def fetch_gift_nifty() -> dict:
    """Latest GIFT Nifty print, read from the data Groww embeds in its GIFT
    Nifty page (__NEXT_DATA__; no login, but undocumented so it may change).
    Returns {price, as_of (ISO, IST), day_open, day_high, day_low, prev_close}."""
    resp = requests.get(GIFT_NIFTY_URL, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
    if not match:
        raise RuntimeError("GIFT Nifty page layout changed (no embedded data)")
    try:
        p = json.loads(match.group(1))["props"]["pageProps"]["globalIndicesData"]["priceData"]
        return {
            "price": p["value"],
            "as_of": datetime.fromtimestamp(p["tsInMillis"], IST).isoformat(),
            "day_open": p.get("open"), "day_high": p.get("high"), "day_low": p.get("low"),
            "prev_close": p.get("close"),
        }
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"GIFT Nifty page layout changed (missing {e})")


def normalise_quote(raw: dict) -> dict:
    ohlc = raw.get("ohlc") or {}
    return {
        "last_price": raw.get("last_price") or None,
        "open": ohlc.get("open") or None,
        "high": ohlc.get("high") or None,
        "low": ohlc.get("low") or None,
        "prev_close": ohlc.get("close") or None,
        "volume": raw.get("volume") if raw.get("volume") is not None else raw.get("volume_traded"),
        "vwap": raw.get("average_price") or None,
        "upper_circuit": raw.get("upper_circuit_limit") or None,
        "lower_circuit": raw.get("lower_circuit_limit") or None,
        "timestamp": raw["timestamp"].isoformat() if raw.get("timestamp") else None,
    }


def get_quotes(kite: KiteConnect, symbols: list[str], extra_keys: list[str] | None = None) -> dict[str, dict]:
    """{symbol_or_key: normalised quote}. Symbols come back under their bare
    ticker, extra_keys under the key as given (e.g. "NSE:NIFTY 50")."""
    keys = [f"{EXCHANGE}:{s}" for s in symbols] + list(extra_keys or [])
    if not keys:
        return {}
    raw = kite.quote(keys)
    out = {}
    for sym in symbols:
        q = raw.get(f"{EXCHANGE}:{sym}")
        if q:
            out[sym] = normalise_quote(q)
    for key in extra_keys or []:
        if raw.get(key):
            out[key] = normalise_quote(raw[key])
    return out


def _cached_stats_valid(row: dict, cfg: HVConfig, today: date) -> bool:
    stats = row.get("intraday_stats")
    return bool(
        stats
        and row.get("intraday_stats_date") == today.isoformat()
        and stats.get("swing_pct") == cfg.swing_pct
        and stats.get("first_hour_minutes") == cfg.first_hour_minutes
    )


def build_candidates(kite: KiteConnect, cfg: HVConfig, extras: list[str], force: bool = False) -> dict:
    started = time_module.monotonic()
    today = today_ist()
    snapshot = db_store.load_atr_snapshot()
    by_symbol = {r["symbol"]: r for r in snapshot}

    shortlist = [r for r in snapshot if r["atr_pct"] >= cfg.min_atr_pct][: cfg.shortlist_size]
    work = [{"symbol": r["symbol"], "tier": r["tier"], "atr_pct": r["atr_pct"], "manual": False,
             "row": r} for r in shortlist]
    shortlisted = {w["symbol"] for w in work}
    for sym in extras:
        if sym in shortlisted:
            continue
        snap = by_symbol.get(sym)  # typed in by hand but already in the snapshot: use its ATR%
        work.append({"symbol": sym, "tier": snap["tier"] if snap else "manual",
                     "atr_pct": snap["atr_pct"] if snap else None, "manual": True, "row": snap})

    tokens = resolve_tokens(kite, [w["symbol"] for w in work]) if work else {}

    pending: list[str] = []
    next_request_at = time_module.monotonic()
    for w in work:
        sym, row = w["symbol"], w["row"]
        if row and not force and _cached_stats_valid(row, cfg, today):
            w["stats"] = row["intraday_stats"]
            continue
        if sym not in tokens:
            w["stats"] = None
            w["missing"] = "symbol not found on NSE"
            continue
        if time_module.monotonic() - started > _TIME_BUDGET_SECONDS:
            pending.append(sym)
            w["stats"] = None
            w["skipped"] = True
            continue
        wait = next_request_at - time_module.monotonic()
        if wait > 0:
            time_module.sleep(wait)
        next_request_at = time_module.monotonic() + _MIN_REQUEST_INTERVAL

        candles = fetch_5min(kite, tokens[sym], cfg.history_calendar_days)
        stats = intraday_stats(candles, cfg, today) if candles is not None else None
        w["stats"] = stats
        if stats is not None and row is not None:
            try:
                db_store.save_intraday_stats(sym, stats, today)
            except Exception as e:  # caching is an optimisation, never fatal
                logger.warning("Could not cache intraday stats for %s: %s", sym, e)

    quotes, nifty = {}, {}
    quote_error = None
    try:
        quotes = get_quotes(kite, [w["symbol"] for w in work], [NIFTY_KEY])
        nifty = quotes.pop(NIFTY_KEY, {})
    except Exception as e:
        quote_error = str(e)
        logger.warning("Quote fetch failed: %s", e)

    rows = [{"symbol": w["symbol"], "tier": w["tier"], "atr_pct": w["atr_pct"], "manual": w["manual"],
             "stats": w.get("stats"), "quote": quotes.get(w["symbol"]),
             "missing_reason": w.get("missing")}
            for w in work if not w.get("skipped")]
    ranked, excluded = rank_candidates(rows, cfg)

    snapshot_dates = {r["run_date"] for r in snapshot}
    latest_run = max(snapshot_dates) if snapshot_dates else None
    return {
        "generated_at": datetime.now(IST).isoformat(),
        "today": today.isoformat(),
        "snapshot_run_date": latest_run,
        "snapshot_stale": latest_run != today.isoformat() if latest_run else True,
        "snapshot_symbols": len(snapshot),
        "shortlist_size": len(shortlist),
        "nifty_prev_close": nifty.get("prev_close"),
        "nifty_last": nifty.get("last_price"),
        "ranked": ranked,
        "excluded": excluded,
        "pending": pending,
        "quote_error": quote_error,
        "tag_bonus": cfg.tag_bonus,
    }
