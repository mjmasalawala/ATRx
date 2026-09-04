"""
Recomputes and stores cost_basis_state (the precomputed table the Cost
Basis page's summary table actually reads) for every symbol with any
uploaded trades. Runs purely off trade history -- see cost_basis.py for
why baseline snapshots are no longer part of this calculation.

Called by trade_csv_import.py after every upload -- needs the FULL symbol
universe recomputed each time, not just the symbols touched by that
particular upload, since a symbol never gets a cost_basis_state row until
something recomputes state for it specifically.

Per-symbol failures are caught and reported rather than aborting the
whole recompute -- one symbol with bad/unexpected data (e.g. a sell with
no matching buy history) must not silently prevent every other symbol's
state from being written at all.

Loads every symbol's trades with one bulk query (db_store.load_all_trades)
rather than opening a fresh database connection per symbol
(load_symbol_trades opens its own connection) -- for a few dozen symbols
that was dozens of sequential connection handshakes to Neon, slow enough
to plausibly run into Vercel's 60s function timeout and abort before
ever writing anything.
"""

import logging

import cost_basis
import db_store

logger = logging.getLogger("atrx.cost_basis_state_sync")


def recompute_all() -> dict:
    trades_by_symbol = db_store.load_all_trades()

    state_rows = []
    failed = []
    for symbol in sorted(trades_by_symbol):
        try:
            result = cost_basis.replay(symbol, trades_by_symbol[symbol])
            state_rows.append({k: result[k] for k in (
                "symbol", "quantity", "total_cost", "avg_cost",
                "cumulative_realized", "lifetime_realized", "is_free",
            )})
        except Exception as e:
            logger.exception("Failed to recompute cost basis state for %s", symbol)
            failed.append({"symbol": symbol, "error": f"{type(e).__name__}: {e}"})

    db_store.upsert_cost_basis_state(state_rows)
    return {"updated": [r["symbol"] for r in state_rows], "failed": failed}
