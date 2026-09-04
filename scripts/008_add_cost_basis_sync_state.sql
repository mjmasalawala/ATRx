-- Tracks when a trades CSV was last uploaded on the Cost Basis page (see
-- trade_csv_import.py) -- replaces the daily cron job that used to pull
-- trades from Kite's live API (removed: that API only ever returns the
-- current trading day's fills, so it could silently lose a skipped day).
-- A singleton row (id always 1) rather than a table, since there's
-- exactly one "last uploaded" moment across the whole account, not one
-- per symbol. Run once manually in Neon's SQL Editor (or via psql), after
-- 001-007.

CREATE TABLE IF NOT EXISTS cost_basis_sync_state (
    id INT PRIMARY KEY DEFAULT 1,
    last_synced_at TIMESTAMPTZ,
    CONSTRAINT cost_basis_sync_state_singleton CHECK (id = 1)
);

INSERT INTO cost_basis_sync_state (id, last_synced_at) VALUES (1, NULL)
ON CONFLICT (id) DO NOTHING;
