"""
Kite-session-backed actions for the Cost Basis / ATRx Stock pages. Trade
history itself is no longer pulled from Kite's live API (see
trade_csv_import.py for why -- /trades only ever returns the current
trading day's fills, so a periodic or on-demand pull can silently lose
whole skipped days). This module now only handles the one-time holdings
baseline dump, which doesn't have that problem since it's just a snapshot
of current positions, not a trade history.
"""

from datetime import date
import logging

import db_store
from kite_web_auth import get_kite_session_from_token
from token_store import load_access_token

logger = logging.getLogger("atrx.kite_sync")


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
