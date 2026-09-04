-- Backs the "Cost Basis" page: per-ticker acquisition cost tracking where
-- realized profit from partial sells reduces the cost basis of the
-- remaining shares (rather than the usual "avg cost stays flat" method).
-- Run once manually in Neon's SQL Editor (or via psql), after 001-004.
--
-- cost_basis_baseline: one-time snapshot of current Kite holdings, seeded
-- locally via scripts/seed_cost_basis_baseline.py -- the starting lot for
-- each symbol before day-to-day trade syncing began.
--
-- cost_basis_trades: append-only ledger of individual fills pulled daily
-- from Kite's trade book. trade_id is Kite's own id for the fill, so it's
-- the primary key -- re-running the daily sync can't double-insert.
--
-- cost_basis_state: precomputed *current* state per symbol (qty, avg cost,
-- realized profit, etc), recomputed and upserted by the daily sync agent
-- after it appends new trades. The Cost Basis page reads this directly
-- instead of replaying the full ledger on every page load.

CREATE TABLE IF NOT EXISTS cost_basis_baseline (
    symbol TEXT PRIMARY KEY,
    quantity NUMERIC NOT NULL,
    avg_price NUMERIC NOT NULL,
    as_of_date DATE NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cost_basis_trades (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity NUMERIC NOT NULL,
    price NUMERIC NOT NULL,
    trade_time TIMESTAMPTZ NOT NULL,
    order_id TEXT,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cost_basis_trades_symbol_idx ON cost_basis_trades (symbol, trade_time);

CREATE TABLE IF NOT EXISTS cost_basis_state (
    symbol TEXT PRIMARY KEY,
    quantity NUMERIC NOT NULL,
    total_cost NUMERIC NOT NULL,
    avg_cost NUMERIC,
    cumulative_realized NUMERIC NOT NULL DEFAULT 0,
    lifetime_realized NUMERIC NOT NULL DEFAULT 0,
    is_free BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
