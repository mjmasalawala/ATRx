-- High Volatility: risk-based journal columns, the per-day watch context
-- and the signal log. Run once manually in Neon's SQL Editor (or via psql),
-- after 012. Safe to re-run: every statement is IF NOT EXISTS.
--
-- Storage stays bounded:
--   * hv_watch_context -- one small row per watched stock per day; rows
--                         older than 14 days are deleted on each Start watch.
--   * hv_signals       -- a handful of live rows per stock per day, plus
--                         replay rows (re-running a replay replaces them).

-- Each trade now records its planned stop and the rupee risk (1R) at entry,
-- so results can be read in R, and whether a rule was broken.
ALTER TABLE hv_positions ADD COLUMN IF NOT EXISTS planned_stop NUMERIC(12,2);
ALTER TABLE hv_positions ADD COLUMN IF NOT EXISTS risk_amount NUMERIC(12,2);
ALTER TABLE hv_positions ADD COLUMN IF NOT EXISTS rule_broken BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE hv_positions ADD COLUMN IF NOT EXISTS rule_note TEXT;
ALTER TABLE hv_positions ADD COLUMN IF NOT EXISTS signal_id INTEGER;

-- What "Start watch" computes before the open for each watched stock:
-- previous day's levels, daily ATR, the normal volume of each 5-minute
-- slot, the average first-hour traded value (liquidity cap).
CREATE TABLE IF NOT EXISTS hv_watch_context (
    trade_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    instrument_token BIGINT,
    context JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, symbol)
);

-- Every setup the page suggested (stage 'armed' or 'entry'), live or from a
-- replay of past sessions, with what you did and what price did next.
-- episode = the time of the flush low, so one setup is stored once.
CREATE TABLE IF NOT EXISTS hv_signals (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live', 'replay')),
    trade_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('armed', 'entry')),
    episode TEXT NOT NULL,
    signal_at TIMESTAMPTZ NOT NULL,
    day_type TEXT,
    metrics JSONB,
    entry NUMERIC(12,2),
    stop NUMERIC(12,2),
    target NUMERIC(12,2),
    qty INTEGER,
    action TEXT CHECK (action IN ('taken', 'skipped', 'lapsed')),
    skip_reason TEXT,
    outcome TEXT CHECK (outcome IN ('not_filled', 'target', 'stop', 'timeout', 'confirmed', 'no_confirm')),
    outcome_r NUMERIC(6,2),
    mfe_r NUMERIC(6,2),
    mae_r NUMERIC(6,2),
    scored_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, trade_date, symbol, stage, episode)
);
CREATE INDEX IF NOT EXISTS hv_signals_trade_date_idx ON hv_signals (trade_date);
