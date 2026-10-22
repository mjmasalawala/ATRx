-- Tables for the High Volatility page (pre-open watchlist, first-hour live
-- monitor, trade journal). Run once manually in Neon's SQL Editor (or via
-- psql), after 001-011. Safe to re-run: every statement is IF NOT EXISTS.
--
-- Storage stays bounded on purpose:
--   * hv_atr_snapshot  -- ONE row per symbol, overwritten by every Find ATRx
--                         Opps run (~240 rows total, never grows).
--   * hv_settings      -- singleton row.
--   * hv_day / hv_focus-- a handful of rows per trading day.
--   * hv_positions / hv_fills -- one position per round trip, a few fills
--                         each (a few thousand rows a year).

-- Latest ATR% per symbol, written by screener.py's run (the "Find ATRx
-- Opps" morning run) so the High Volatility page can reuse it instead of
-- recomputing. intraday_stats caches the first-hour / swing statistics
-- computed from 5-minute candles the first time a symbol is shortlisted on
-- a given day (intraday_stats_date), so reloading the page is instant.
CREATE TABLE IF NOT EXISTS hv_atr_snapshot (
    symbol TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    atr_pct NUMERIC(7,2) NOT NULL,
    atr_percentile_rank NUMERIC(5,1),
    price NUMERIC(12,2),
    atr_period INTEGER,
    run_date DATE NOT NULL,
    intraday_stats JSONB,
    intraday_stats_date DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Singleton: the page's tunable parameters (amount per trade, loss limit,
-- thresholds, ...), persisted so they survive between sessions.
CREATE TABLE IF NOT EXISTS hv_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    params JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per trading day: the pre-open notes (GIFT Nifty value typed in
-- by hand, free-text day note).
CREATE TABLE IF NOT EXISTS hv_day (
    trade_date DATE PRIMARY KEY,
    gift_nifty NUMERIC(12,2),
    note TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Today's focus list: the 2-3 tickers picked each morning, with the
-- pre-open clues (tags: results / news / broker_buy / broker_sell),
-- a long/short bias and the ranking details at the time of picking.
CREATE TABLE IF NOT EXISTS hv_focus (
    trade_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    bias TEXT NOT NULL DEFAULT 'long' CHECK (bias IN ('long', 'short', 'either')),
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    note TEXT,
    pre_meta JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, symbol)
);

-- One row per position (a full round trip on one ticker). Scaling in is
-- several 'entry' fills on the same position; exits can be partial.
-- pnl / charges / return are NEVER stored -- always derived from the fills.
CREATE TABLE IF NOT EXISTS hv_positions (
    id SERIAL PRIMARY KEY,
    trade_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
    setup_tag TEXT,
    note TEXT,
    execution_rating TEXT CHECK (execution_rating IN ('clean', 'rushed')),
    signal_meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hv_positions_trade_date_idx ON hv_positions (trade_date);
CREATE INDEX IF NOT EXISTS hv_positions_symbol_idx ON hv_positions (symbol);

CREATE TABLE IF NOT EXISTS hv_fills (
    id SERIAL PRIMARY KEY,
    position_id INTEGER NOT NULL REFERENCES hv_positions (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'exit')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    price NUMERIC(12,2) NOT NULL CHECK (price > 0),
    filled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hv_fills_position_idx ON hv_fills (position_id);
