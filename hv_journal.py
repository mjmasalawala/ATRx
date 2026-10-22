"""
Pure P&L math for the High Volatility journal: charges, per-position
summaries (scale-ins and partial exits included) and per-day / period
roll-ups. No DB, no Kite -- everything here is derived from the raw fills,
so P&L, charges and returns are never stored (same principle as
db_store.list_trades' pct_return/pnl).

A position is one round trip on one ticker. Entries (buy for a long, sell
for a short) can be repeated to scale in; exits can be partial. Gross P&L
uses the weighted-average entry price against every exit.
"""

from __future__ import annotations

from hv_config import CHARGES


def fill_charges(side: str, kind: str, qty: int, price: float) -> float:
    """Charges for one executed fill. For a long, entry=BUY and exit=SELL;
    for a short, entry=SELL and exit=BUY (so STT lands on the entry)."""
    turnover = qty * price
    is_buy = (kind == "entry") == (side == "long")
    brokerage = min(CHARGES["brokerage_cap"], turnover * CHARGES["brokerage_pct"] / 100)
    exchange = turnover * CHARGES["exchange_txn_pct"] / 100
    sebi = turnover * CHARGES["sebi_pct"] / 100
    gst = (brokerage + exchange + sebi) * CHARGES["gst_pct"] / 100
    stt = 0.0 if is_buy else turnover * CHARGES["stt_sell_pct"] / 100
    stamp = turnover * CHARGES["stamp_buy_pct"] / 100 if is_buy else 0.0
    return brokerage + exchange + sebi + gst + stt + stamp


def position_summary(side: str, fills: list[dict]) -> dict:
    """fills: [{"kind": "entry"|"exit", "qty": int, "price": float, ...}].
    Returns quantities, average entry/exit, gross/charges/net P&L and
    net % return on the capital deployed. Open quantity has no P&L yet --
    live marking against the current price happens in the page."""
    sign = 1 if side == "long" else -1
    entry_qty = sum(f["qty"] for f in fills if f["kind"] == "entry")
    exit_qty = sum(f["qty"] for f in fills if f["kind"] == "exit")
    entry_value = sum(f["qty"] * f["price"] for f in fills if f["kind"] == "entry")
    exit_value = sum(f["qty"] * f["price"] for f in fills if f["kind"] == "exit")
    avg_entry = entry_value / entry_qty if entry_qty else None
    avg_exit = exit_value / exit_qty if exit_qty else None

    gross = 0.0
    if avg_entry is not None and exit_qty:
        gross = sign * (avg_exit - avg_entry) * exit_qty

    # Only count entry charges for the part of the position that has been
    # exited, so a half-closed position doesn't show the whole entry cost
    # against half the profit; the rest of the entry charges are carried by
    # the still-open quantity.
    closed_fraction = min(exit_qty, entry_qty) / entry_qty if entry_qty else 0.0
    entry_charges = sum(fill_charges(side, "entry", f["qty"], f["price"])
                        for f in fills if f["kind"] == "entry") * closed_fraction
    exit_charges = sum(fill_charges(side, "exit", f["qty"], f["price"])
                       for f in fills if f["kind"] == "exit")
    charges = entry_charges + exit_charges
    net = gross - charges

    open_qty = max(entry_qty - exit_qty, 0)
    status = "open" if open_qty > 0 else "closed"
    deployed = (avg_entry or 0) * min(exit_qty, entry_qty)
    return {
        "entry_qty": entry_qty,
        "exit_qty": exit_qty,
        "open_qty": open_qty,
        "status": status,
        "avg_entry": round(avg_entry, 4) if avg_entry is not None else None,
        "avg_exit": round(avg_exit, 4) if avg_exit is not None else None,
        "gross_pnl": round(gross, 2),
        "charges": round(charges, 2),
        "net_pnl": round(net, 2),
        "net_pct": round(net / deployed * 100, 3) if deployed else None,
    }


def break_even_price(side: str, avg_entry: float, qty: int) -> float:
    """Exit price at which gross P&L exactly covers the charges of the
    full round trip (entry + exit) for `qty` shares."""
    if qty <= 0 or avg_entry <= 0:
        return avg_entry
    sign = 1 if side == "long" else -1
    entry_charges = fill_charges(side, "entry", qty, avg_entry)
    price = avg_entry
    for _ in range(4):  # exit charges depend (weakly) on the exit price itself
        exit_charges = fill_charges(side, "exit", qty, price)
        price = avg_entry + sign * (entry_charges + exit_charges) / qty
    return round(price, 2)


def target_price(side: str, avg_entry: float, qty: int, net_pct: float) -> float:
    """Exit price that yields `net_pct` percent profit AFTER all charges,
    on the capital deployed at avg_entry."""
    if qty <= 0 or avg_entry <= 0:
        return avg_entry
    sign = 1 if side == "long" else -1
    wanted_net = avg_entry * qty * net_pct / 100
    entry_charges = fill_charges(side, "entry", qty, avg_entry)
    price = avg_entry
    for _ in range(4):
        exit_charges = fill_charges(side, "exit", qty, price)
        price = avg_entry + sign * (wanted_net + entry_charges + exit_charges) / qty
    return round(price, 2)


def day_summary(positions: list[dict], cfg) -> dict:
    """positions: dicts that already carry a `summary` (position_summary)
    plus `symbol`. Round trips = closed positions."""
    closed = [p for p in positions if p["summary"]["status"] == "closed"]
    # A half-exited position still has realised P&L today; count it in net.
    net = sum(p["summary"]["net_pnl"] for p in positions)
    tickers = sorted({p["symbol"] for p in positions})
    closed_tickers = sorted({p["symbol"] for p in closed})
    round_trips = len(closed)
    limit = cfg.daily_loss_limit
    return {
        "round_trips": round_trips,
        "target_round_trips": cfg.target_round_trips,
        "tickers": tickers,
        "ticker_count": len(tickers),
        "max_tickers": cfg.max_tickers,
        "open_positions": sum(1 for p in positions if p["summary"]["status"] == "open"),
        "net_pnl": round(net, 2),
        "loss_limit": limit,
        "loss_limit_hit": bool(limit) and net <= -abs(limit),
        "done_for_day": round_trips >= cfg.target_round_trips or (
            len(closed_tickers) >= cfg.max_tickers and net > 0),
    }


def build_journal(positions: list[dict], cfg) -> dict:
    """positions: [{id, trade_date (ISO str), symbol, side, ..., fills: [...]}],
    newest first. Returns the same list with `summary` attached, plus a
    per-day roll-up (oldest first, with a cumulative net line) and totals."""
    for p in positions:
        p["summary"] = position_summary(p["side"], p["fills"])

    by_day: dict[str, list[dict]] = {}
    for p in positions:
        by_day.setdefault(p["trade_date"], []).append(p)

    days = []
    cumulative = 0.0
    for date in sorted(by_day):
        d = day_summary(by_day[date], cfg)
        d["date"] = date
        cumulative += d["net_pnl"]
        d["cumulative_net"] = round(cumulative, 2)
        days.append(d)

    return {
        "positions": positions,
        "days": days,
        "totals": {
            "net_pnl": round(sum(d["net_pnl"] for d in days), 2),
            "trading_days": len(days),
            "round_trips": sum(d["round_trips"] for d in days),
            "green_days": sum(1 for d in days if d["net_pnl"] > 0),
            "red_days": sum(1 for d in days if d["net_pnl"] < 0),
        },
    }
