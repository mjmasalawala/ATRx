"""
Kite/DB plumbing for the High Volatility live tab: Start watch (pre-open
context per stock), the live snapshot (quotes + 1-minute candles -> each
card's stage, suggestion and risk state), scoring the day's signals and
replaying past sessions. The rules themselves live in hv_signals.py and
hv_risk.py (pure, unit-tested); this module only fetches and stores.
"""

from __future__ import annotations

import logging
import time as time_module
from datetime import date, datetime, timedelta

from kiteconnect import KiteConnect

import db_store
from hv_config import HVConfig
from hv_journal import build_journal
from hv_risk import day_state, entry_blockers, period_state, to_ist_naive
from hv_screener import EXCHANGE, IST, get_quotes, today_ist
from hv_signals import (build_context, evaluate, manage_position, nifty_state, replay_day, resample,
                        score_signal)
from screener import resolve_tokens

logger = logging.getLogger("atrx.hv")

NIFTY_SYMBOL = "NIFTY 50"
NIFTY_KEY = f"{EXCHANGE}:{NIFTY_SYMBOL}"
_MIN_REQUEST_INTERVAL = 1.0 / 3.0   # Kite historical: 3 requests a second
_TIME_BUDGET_SECONDS = 42           # Vercel Hobby stops a function at 60 s
EVENT_TAGS = ("results", "news")


class _Pacer:
    """Spaces Kite historical calls to the 3/s limit."""

    def __init__(self):
        self.next_at = time_module.monotonic()

    def wait(self):
        delay = self.next_at - time_module.monotonic()
        if delay > 0:
            time_module.sleep(delay)
        self.next_at = time_module.monotonic() + _MIN_REQUEST_INTERVAL


def fetch_candles(kite: KiteConnect, token: int, start: datetime, end: datetime, interval: str) -> list[dict]:
    """Kite candles as dicts with naive IST `ts` (bar start)."""
    raw = kite.historical_data(instrument_token=token, from_date=start, to_date=end, interval=interval)
    out = []
    for c in raw:
        ts = c["date"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is not None:
            ts = ts.astimezone(IST).replace(tzinfo=None)
        out.append({"ts": ts, "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
                    "close": float(c["close"]), "volume": float(c.get("volume") or 0)})
    return out


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=IST)


# ------------------------------------------------------------- start watch

def start_watch(kite: KiteConnect, cfg: HVConfig, symbols: list[str]) -> dict:
    """Builds and stores today's context (levels, ATR, time-of-day volume)
    for each symbol plus Nifty's token. Returns {ready, failed, pending}."""
    started = time_module.monotonic()
    today = today_ist()
    tokens = resolve_tokens(kite, symbols + [NIFTY_SYMBOL])
    end = now_ist()
    begin = end - timedelta(days=cfg.history_calendar_days)
    pacer, rows, failed, pending = _Pacer(), [], {}, []
    for sym in symbols:
        if sym not in tokens:
            failed[sym] = "symbol not found on NSE"
            continue
        if time_module.monotonic() - started > _TIME_BUDGET_SECONDS:
            pending.append(sym)
            continue
        pacer.wait()
        try:
            ctx = build_context(fetch_candles(kite, tokens[sym], begin, end, "5minute"), today)
        except Exception as e:
            failed[sym] = f"history fetch failed: {e}"
            continue
        if ctx is None:
            failed[sym] = "fewer than 3 sessions of 5-minute history"
            continue
        rows.append({"symbol": sym, "instrument_token": tokens[sym], "context": ctx})
    if NIFTY_SYMBOL in tokens:
        rows.append({"symbol": NIFTY_SYMBOL, "instrument_token": tokens[NIFTY_SYMBOL], "context": {}})
    if rows:
        db_store.save_watch_context(today, rows)
    return {"ready": [r["symbol"] for r in rows if r["symbol"] != NIFTY_SYMBOL], "failed": failed,
            "pending": pending}


# ----------------------------------------------------------- live snapshot

def _position_view(p: dict) -> dict:
    s = p["summary"]
    entries = [f for f in p["fills"] if f["kind"] == "entry"]
    return {"avg_entry": s["avg_entry"], "open_qty": s["open_qty"], "entry_qty": s["entry_qty"],
            "entry_fills": len(entries), "planned_stop": p.get("planned_stop"),
            "risk_amount": p.get("risk_amount"),
            "opened_at": to_ist_naive(min(f["filled_at"] for f in entries)) if entries else None}


def risk_snapshot(cfg: HVConfig, now: datetime) -> tuple[dict, dict, list[dict]]:
    """(day_state, period_state, today's positions) from the journal."""
    today = now.date()
    positions = build_journal(db_store.list_hv_positions(today - timedelta(days=62)), cfg)["positions"]
    period = period_state(positions, cfg, today)
    todays = [p for p in positions if p["trade_date"] == today.isoformat()]
    day = day_state(todays, cfg, now, one_r=cfg.one_r * period["size_mult"])
    if period["monthly_stop"] and not day["lockout"]:
        day["lockout"] = "monthly drawdown limit -- review the journal before trading again"
    return day, period, todays


def live_snapshot(kite: KiteConnect, cfg: HVConfig, focus: list[str], strip: list[str]) -> dict:
    now = now_ist()
    today = now.date()
    contexts = db_store.load_watch_context(today)
    day_info = db_store.load_hv_day(today)
    tags = {f["symbol"]: f.get("tags") or [] for f in day_info.get("focus") or []}
    symbols = focus + [s for s in strip if s not in focus]

    quotes = get_quotes(kite, symbols, [NIFTY_KEY])
    nifty_quote = quotes.pop(NIFTY_KEY, {})

    session_start = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    pacer, bars = _Pacer(), {}
    nifty = None
    nifty_ctx = contexts.get(NIFTY_SYMBOL)
    if now >= session_start:
        if nifty_ctx and nifty_ctx["instrument_token"]:
            pacer.wait()
            try:
                nifty = nifty_state(fetch_candles(kite, nifty_ctx["instrument_token"], session_start, now, "minute"), now)
            except Exception as e:
                logger.warning("Nifty candles failed: %s", e)
        for sym in symbols:
            c = contexts.get(sym)
            if not c:
                continue
            pacer.wait()
            try:
                bars[sym] = fetch_candles(kite, c["instrument_token"], session_start, now, "minute")
            except Exception as e:
                logger.warning("1-minute candles failed for %s: %s", sym, e)
                bars[sym] = []

    day, period, todays = risk_snapshot(cfg, now)
    one_r = cfg.one_r * period["size_mult"]
    nifty_down = bool(nifty and nifty["state"] == "Trend down")
    open_by_symbol = {p["symbol"]: p for p in todays if p["summary"]["status"] == "open"}

    cards = {}
    for sym in symbols:
        ctx = (contexts.get(sym) or {}).get("context")
        mult = cfg.event_size_mult if any(t in EVENT_TAGS for t in tags.get(sym, [])) else 1.0
        ev = evaluate(bars.get(sym, []), ctx, quotes.get(sym), nifty, cfg, now, one_r=one_r,
                      exposure_left=day["exposure_left"], size_mult=mult)
        ev["size_mult"] = mult * period["size_mult"]
        if sym in open_by_symbol:
            ev["position"] = manage_position(_position_view(open_by_symbol[sym]), bars.get(sym, []), ev,
                                             nifty, cfg, now)
        elif ev["stage"] in ("armed", "entry"):
            blockers = entry_blockers(sym, day, cfg, nifty_down)
            if blockers:
                ev["stage"] = "blocked"
                ev["reasons"].extend(blockers)
        if ev["stage"] in ("armed", "entry") and sym not in open_by_symbol:
            s = ev.get("suggestion") or {}
            signal_at = now.replace(second=0, microsecond=0)
            if s.get("signal_bar"):  # an entry is timed from the end of its confirming bar
                hh, mm = map(int, s["signal_bar"].split(":"))
                signal_at = datetime.combine(today, datetime.min.time()).replace(hour=hh, minute=mm) + timedelta(minutes=1)
            try:
                ev["signal_id"] = db_store.record_hv_signal({
                    "trade_date": today, "symbol": sym, "stage": ev["stage"], "episode": ev["episode"],
                    "signal_at": _aware(signal_at),
                    "day_type": (ev["day_type"] or {}).get("state"),
                    "metrics": {**ev["metrics"], "suggestion_ok": s.get("ok"), "fails": s.get("fails")},
                    "entry": s.get("entry"), "stop": s.get("stop"), "target": s.get("target"),
                    "qty": s.get("qty")})
            except Exception as e:  # the log is a record, never a reason to fail the refresh
                logger.warning("Could not record signal for %s: %s", sym, e)
        cards[sym] = ev

    return {
        "now": now.isoformat(timespec="seconds"),
        "quotes": quotes,
        "nifty": {**(nifty or {"state": "Range", "detail": "no candles yet"}),
                  "last_price": nifty_quote.get("last_price")},
        "cards": cards,
        "risk": day,
        "period": period,
        "missing_context": [s for s in symbols if s not in contexts],
    }


# --------------------------------------------------------------- scoring

def score_day(kite: KiteConnect, cfg: HVConfig, trade_date: date) -> dict:
    """Scores the day's unscored live signals. An entry signal is scored
    once its 30-minute window has passed; an armed one is 'confirmed' if an
    entry with the same episode followed, else 'no_confirm' after the
    entry window closes."""
    now = now_ist()
    signals = [s for s in db_store.list_hv_signals(trade_date, trade_date, source="live") if not s["outcome"]]
    if not signals:
        return {"scored": 0}
    entries = {(s["symbol"], s["episode"]) for s in signals if s["stage"] == "entry"}
    entries |= {(s["symbol"], s["episode"]) for s in db_store.list_hv_signals(trade_date, trade_date, "live")
                if s["stage"] == "entry"}
    window_closed = now >= datetime.combine(trade_date, datetime.strptime(cfg.entry_end, "%H:%M").time())
    contexts = db_store.load_watch_context(trade_date)
    session_start = datetime.combine(trade_date, datetime.min.time()).replace(hour=9, minute=15)
    session_end = min(now, session_start + timedelta(hours=6, minutes=15))
    pacer, bars, scores = _Pacer(), {}, []
    for s in signals:
        if s["stage"] == "armed":
            if (s["symbol"], s["episode"]) in entries:
                scores.append({"id": s["id"], "outcome": "confirmed"})
            elif window_closed:
                scores.append({"id": s["id"], "outcome": "no_confirm"})
            continue
        if s["symbol"] not in bars:
            c = contexts.get(s["symbol"])
            if not c:
                continue
            pacer.wait()
            try:
                bars[s["symbol"]] = fetch_candles(kite, c["instrument_token"], session_start, session_end, "minute")
            except Exception as e:
                logger.warning("Scoring candles failed for %s: %s", s["symbol"], e)
                bars[s["symbol"]] = []
        result = score_signal({"signal_at": to_ist_naive(s["signal_at"]), "entry": s["entry"],
                               "stop": s["stop"], "target": s["target"]}, bars[s["symbol"]], cfg)
        if result.get("outcome"):
            scores.append({"id": s["id"], **result, "action": None if s["action"] else "lapsed"})
    db_store.save_hv_signal_scores(scores)
    return {"scored": len(scores)}


# ---------------------------------------------------------------- replay

def replay(kite: KiteConnect, cfg: HVConfig, symbol: str) -> dict:
    """Runs the detector over the last ~60 calendar days of 1-minute data
    (Kite's limit per request) for one stock. Each day's context is built
    from the sessions before it, so only days with 10+ earlier sessions are
    replayed. Replaces the stock's earlier replay rows for those days."""
    tokens = resolve_tokens(kite, [symbol, NIFTY_SYMBOL])
    if symbol not in tokens:
        raise ValueError(f"{symbol} not found on NSE")
    today = today_ist()
    end = datetime.combine(today, datetime.min.time())
    begin = end - timedelta(days=59)
    pacer = _Pacer()
    pacer.wait()
    stock = fetch_candles(kite, tokens[symbol], begin, end, "minute")
    nifty = []
    if NIFTY_SYMBOL in tokens:
        pacer.wait()
        nifty = fetch_candles(kite, tokens[NIFTY_SYMBOL], begin, end, "minute")

    by_day: dict[date, list[dict]] = {}
    for b in stock:
        by_day.setdefault(b["ts"].date(), []).append(b)
    nifty_by_day: dict[date, list[dict]] = {}
    for b in nifty:
        nifty_by_day.setdefault(b["ts"].date(), []).append(b)
    days = sorted(by_day)
    five_by_day = {d: resample(by_day[d]) for d in days}

    rows, replayed = [], []
    for i, d in enumerate(days):
        if i < 10 or d >= today:
            continue
        history = [b for prev in days[max(0, i - 20):i] for b in five_by_day[prev]]
        ctx = build_context(history, d)
        if ctx is None:
            continue
        replayed.append(d)
        for sig in replay_day(by_day[d], ctx, nifty_by_day.get(d, []), cfg):
            rows.append({**sig, "trade_date": d, "signal_at": _aware(sig["signal_at"])})
    if replayed:
        db_store.replace_hv_replay_signals(symbol, replayed[0], replayed[-1], rows)
    scored = [r for r in rows if r.get("outcome") in ("target", "stop", "timeout")]
    return {
        "symbol": symbol, "days": len(replayed),
        "from": replayed[0].isoformat() if replayed else None,
        "to": replayed[-1].isoformat() if replayed else None,
        "signals": len(rows),
        "targets": sum(1 for r in rows if r.get("outcome") == "target"),
        "stops": sum(1 for r in rows if r.get("outcome") == "stop"),
        "avg_r": round(sum(r["outcome_r"] for r in scored) / len(scored), 2) if scored else None,
    }
