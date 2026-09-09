-- Tracks manually-logged trades (entry + exit) tied to whichever strategy
-- page produced the signal, so win-rate/P&L stats can be computed per
-- strategy. Distinct from cost_basis_trades (a ledger of real broker fills
-- for tax/avg-cost accounting, imported from a Zerodha CSV) -- this table
-- has nothing to do with that system.
--
-- One row = one full position (one entry + one eventual exit), not one
-- fill -- the same symbol can have many separate rows over time, since
-- repeated correct calls by the same strategy are exactly what the
-- win-rate stat is meant to capture.
--
-- Run manually in Neon's SQL Editor (or via psql) -- the app never creates
-- or alters this table itself, only reads/writes rows in it.

CREATE TABLE IF NOT EXISTS performance_log (
    id SERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    entry_date DATE NOT NULL,
    entry_price NUMERIC(12,2) NOT NULL CHECK (entry_price > 0),
    exit_date DATE,
    exit_price NUMERIC(12,2) CHECK (exit_price IS NULL OR exit_price > 0),
    notes TEXT,
    signal_meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((exit_date IS NULL) = (exit_price IS NULL))
);

CREATE INDEX IF NOT EXISTS performance_log_strategy_idx ON performance_log(strategy);
CREATE INDEX IF NOT EXISTS performance_log_symbol_idx ON performance_log(symbol);
