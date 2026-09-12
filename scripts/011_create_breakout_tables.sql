-- Schema for the breakout ("N-of-M block staircase") screener. Run
-- manually in Neon's SQL Editor (or via psql) -- the app never creates or
-- alters these tables itself, only reads/writes rows in them.
--
-- breakout_config: singleton last-used-params row, same shape/purpose as
-- screener_config (001_create_screener_tables.sql) but for this separate
-- strategy's own tunables.
--
-- breakout_signal_events: one row per (symbol, signal_date, signal_config_hash)
-- -- every historical firing of the staircase pattern found during a run's
-- in-window backtest, persisted so evidence accumulates across separate
-- runs (different universe tiers, or the same tier run again later) rather
-- than each run only ever seeing its own 180-day slice. signal_config_hash
-- (see breakout_config.BreakoutConfig.signal_config_hash) groups events by
-- the signal *definition* that produced them (period_days/n_blocks/
-- min_up_periods/block_value) -- changing a filter or display setting
-- doesn't change this hash, so it doesn't fragment previously-collected
-- evidence. Forward-return columns are nullable and re-checked/filled on
-- every run that touches the same symbol+date+hash: a signal fired near
-- the edge of one run's fetch window may not have had enough *future*
-- data yet to compute its 20-day return, but a later run (more history
-- has since elapsed) can fill that in without duplicating the row.

CREATE TABLE IF NOT EXISTS breakout_config (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    params JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS breakout_signal_events (
    symbol TEXT NOT NULL,
    signal_date DATE NOT NULL,
    signal_config_hash TEXT NOT NULL,
    universe_tier TEXT NOT NULL,
    entry_price NUMERIC(14,4) NOT NULL,
    window_gain_pct NUMERIC(10,4),
    up_periods INTEGER NOT NULL,
    n_blocks INTEGER NOT NULL,
    block_values JSONB,
    filter_reasons JSONB,          -- [] if the signal passed every filter,
                                    -- otherwise the reasons it was filtered
                                    -- out (still recorded -- a filtered
                                    -- signal is still a data point for
                                    -- "would this have worked", it's just
                                    -- not a live candidate)
    -- Forward returns and control returns, one column pair per horizon.
    -- NULL until that many trading days have elapsed since signal_date
    -- (control_ret_* is the equal-weighted mean return of every other
    -- fetched symbol in the same universe_tier over the same dates).
    fwd_ret_3d NUMERIC(10,4), control_ret_3d NUMERIC(10,4),
    fwd_ret_5d NUMERIC(10,4), control_ret_5d NUMERIC(10,4),
    fwd_ret_10d NUMERIC(10,4), control_ret_10d NUMERIC(10,4),
    fwd_ret_20d NUMERIC(10,4), control_ret_20d NUMERIC(10,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, signal_date, signal_config_hash)
);
CREATE INDEX IF NOT EXISTS breakout_signal_events_hash_idx ON breakout_signal_events (signal_config_hash);
CREATE INDEX IF NOT EXISTS breakout_signal_events_tier_idx ON breakout_signal_events (universe_tier);
