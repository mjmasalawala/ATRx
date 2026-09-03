-- One-time schema migration for the ATRx screener's Neon Postgres storage.
-- Run manually in Neon's SQL Editor (or via psql) -- the app never creates
-- or alters these tables itself, only reads/writes rows in them.
--
-- Both tables are singleton (one row, id = 1): this is a personal,
-- single-user tool, so a JSONB blob per concern is simpler than a fully
-- normalized schema and still holds up if that changes later.

CREATE TABLE IF NOT EXISTS screener_config (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    params JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS screener_universe (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    note TEXT,
    symbols JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
