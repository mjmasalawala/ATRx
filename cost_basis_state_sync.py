"""
Recomputes and stores cost_basis_state (the precomputed table the Cost
Basis page's summary table actually reads) for every symbol that has a
baseline row and/or any uploaded trades.

Shared by trade_csv_import.py (after an upload) and
scripts/seed_cost_basis_baseline.py (after seeding) -- both need the FULL
symbol universe recomputed, not just the symbols they individually
touched. Scoping this to only the touched symbols was the bug that left
baseline-only positions (no trades uploaded for them yet) permanently
missing from the summary table: a symbol never gets a cost_basis_state
row until something recomputes state for it specifically, so a partial
recompute leaves every other symbol's row (if it never existed) or its
staleness (if it did) unresolved.

Per-symbol failures are caught and reported rather than aborting the
whole recompute -- one symbol with bad/unexpected data (e.g. a sell
with no matching buy history) must not silently prevent every other
symbol's state from being written at all.

Loads every symbol's baseline and trades with two bulk queries
(db_store.load_all_baselines/load_all_trades) rather than opening a
fresh database connection per symbol per table (load_baseline_row/
load_symbol_trades each open their own connection) -- for a few dozen
symbols that was 80+ sequential connection handshakes to Neon, slow
enough to plausibly run into Vercel's 60s function timeout and abort
before ever writing anything.
"""

import logging

import cost_basis
import db_store

logger = logging.getLogger("atrx.cost_basis_state_sync")


def recompute_all() -> dict:
    baselines = db_store.load_all_baselines()
    trades_by_symbol = db_store.load_all_trades()
    symbols = sorted(set(baselines) | set(trades_by_symbol))

    state_rows = []
    failed = []
    for symbol in symbols:
        try:
            result = cost_basis.replay(symbol, baselines.get(symbol), trades_by_symbol.get(symbol, []))
            state_rows.append({k: result[k] for k in (
                "symbol", "quantity", "total_cost", "avg_cost",
                "cumulative_realized", "lifetime_realized", "is_free",
            )})
        except Exception as e:
            logger.exception("Failed to recompute cost basis state for %s", symbol)
            failed.append({"symbol": symbol, "error": f"{type(e).__name__}: {e}"})

    db_store.upsert_cost_basis_state(state_rows)
    return {"updated": [r["symbol"] for r in state_rows], "failed": failed}
