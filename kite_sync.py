"""
Daily trade sync for the Cost Basis page. Pulls today's fills from Kite's
trade book, appends any new ones to cost_basis_trades (trade_id as the
primary key makes this idempotent -- see db_store.upsert_trades), then
recomputes and stores each touched symbol's current cost-basis state.

Triggered two ways, both calling sync_todays_trades():
  - the daily Vercel Cron hitting /api/sync-trades (see api/index.py)
  - the "Sync now" button on the Cost Basis page, POSTing /api/sync-trades-now
"""

from datetime import date
import logging

import cost_basis
import db_store
import notify
from kite_web_auth import get_kite_session_from_token
from token_store import load_access_token

logger = logging.getLogger("atrx.kite_sync")


def sync_todays_trades() -> dict:
    try:
        token = load_access_token()
    except RuntimeError as e:
        return {"synced": 0, "reason": f"Token store not configured: {e}", "notified": False}

    if not token:
        notified = notify.send_login_needed_email()
        return {"synced": 0, "reason": "not logged in", "notified": notified}

    kite = get_kite_session_from_token(token)
    fills = kite.trades()  # Kite's trade book only ever contains actual fills.

    rows = [
        {
            "trade_id": str(f["trade_id"]),
            "symbol": f["tradingsymbol"],
            "exchange": f.get("exchange"),
            "side": f["transaction_type"],
            "quantity": f["quantity"],
            "price": f["average_price"],
            "trade_time": f.get("fill_timestamp") or f.get("exchange_timestamp"),
            "order_id": f.get("order_id"),
        }
        for f in fills
    ]

    inserted = db_store.upsert_trades(rows)

    symbols = sorted({row["symbol"] for row in rows})
    state_rows = []
    for symbol in symbols:
        baseline = db_store.load_baseline_row(symbol)
        trades = db_store.load_symbol_trades(symbol)
        result = cost_basis.replay(symbol, baseline, trades)
        state_rows.append({k: result[k] for k in (
            "symbol", "quantity", "total_cost", "avg_cost",
            "cumulative_realized", "lifetime_realized", "is_free",
        )})
    db_store.upsert_cost_basis_state(state_rows)

    return {"synced": inserted, "symbols_updated": symbols, "notified": False}


def download_holdings_baseline() -> dict:
    """Dumps current Kite holdings into cost_basis_baseline -- the same
    one-time seed scripts/seed_cost_basis_baseline.py does locally, but
    triggered from the "Download baseline positions" button on the ATRx
    Stock page using the browser's already-logged-in session, instead of
    the interactive local-login script.

    Deliberately never overwrites a symbol that already has a baseline row
    (force=False) -- this is meant to capture your starting position once,
    not to be re-run over an in-progress cost-basis history."""
    try:
        token = load_access_token()
    except RuntimeError as e:
        return {"fetched": 0, "written": 0, "reason": f"Token store not configured: {e}"}

    if not token:
        return {"fetched": 0, "written": 0, "reason": "not logged in"}

    kite = get_kite_session_from_token(token)
    holdings = kite.holdings()

    rows = [
        {
            "symbol": h["tradingsymbol"],
            "quantity": h["quantity"],
            "avg_price": h["average_price"],
            "as_of_date": date.today().isoformat(),
        }
        for h in holdings
        if h["quantity"] > 0
    ]

    written = db_store.save_baseline(rows)
    return {"fetched": len(rows), "written": written, "skipped": len(rows) - written}
