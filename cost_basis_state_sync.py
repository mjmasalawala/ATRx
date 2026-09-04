"""
Recomputes and stores cost_basis_state (the precomputed table the Cost
Basis page's summary table actually reads) for every symbol that has a
baseline row and/or any uploaded trades.

Shared by trade_csv_import.py (after an upload) and kite_sync.py (after a
baseline download) -- both need the FULL symbol universe recomputed, not
just the symbols they individually touched. Scoping this to only the
touched symbols was the bug that left baseline-only positions (no trades
uploaded for them yet) permanently missing from the summary table: a
symbol never gets a cost_basis_state row until something recomputes state
for it specifically, so a partial recompute leaves every other symbol's
row (if it never existed) or its staleness (if it did) unresolved.
"""

import cost_basis
import db_store


def recompute_all() -> list[str]:
    symbols = db_store.list_traded_symbols()
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
    return symbols
