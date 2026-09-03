-- One-time seed data for the tables created in 001_create_screener_tables.sql.
-- Run after that migration, in Neon's SQL Editor (or via psql). Safe to
-- re-run: ON CONFLICT DO NOTHING means it won't overwrite rows the app
-- has since updated (e.g. config after a run with different parameters).
--
-- Values below mirror config.py's dataclass defaults and universe.json as
-- of the migration to Postgres -- both files stay in the repo only as the
-- local-dev fallback when DATABASE_URL isn't set, not as the source of
-- truth for the deployed app anymore.

INSERT INTO screener_config (id, params) VALUES (
    1,
    '{
        "atr_period": 7,
        "pivot_window": 5,
        "lookback_days": 60,
        "cluster_atr_multiple": 1.0,
        "breach_buffer_atr": 0.25,
        "min_touches": 3,
        "max_breaches": 1,
        "recency_decay_days": 60.0,
        "atrx_lower": -0.3,
        "atrx_upper": 1.0,
        "touch_band_atr": 0.5,
        "forward_days_short": 3,
        "forward_days_long": 5,
        "min_atr_percentile": 50.0,
        "score_w_return": 0.5,
        "score_w_proximity": 0.3,
        "score_w_recency": 0.2,
        "score_breach_penalty": 0.15,
        "top_n": 25
    }'::jsonb
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO screener_universe (id, note, symbols) VALUES (
    1,
    'Your trading universe -- edit anytime, this doesn''t need to change daily. Generated from the first 80 constituents of NSE''s Nifty 100 index, as a market-cap-ranked stand-in for ''market cap > 5000cr'' (every Nifty 100 name is far above that threshold) -- capped at 80 rather than the full 150+ we''d like, because on Vercel''s free Hobby plan a single function run is hard-limited to 60s and Kite''s 3 req/sec historical-data rate limit alone eats ~0.5s per symbol.',
    '["ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT", "DMART", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG", "BANKBARODA", "BEL", "BPCL", "BHARTIARTL", "BOSCHLTD", "BRITANNIA", "CGPOWER", "CANBK", "CHOLAFIN", "CIPLA", "COALINDIA", "CUMMINSIND", "DLF", "DIVISLAB", "DRREDDY", "EICHERMOT", "ETERNAL", "GAIL", "GODREJCP", "GRASIM", "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HAL", "HINDUNILVR", "HINDZINC", "HYUNDAI", "ICICIBANK", "ITC", "INDHOTEL", "IOC", "IRFC", "INFY", "INDIGO", "JSWSTEEL", "JINDALSTEL", "JIOFIN", "KOTAKBANK", "LTM", "LT", "LODHA", "M&M", "MARUTI", "MAXHEALTH", "MAZDOCK", "MUTHOOTFIN", "NTPC", "NESTLEIND", "ONGC", "PIDILITIND", "PFC", "POWERGRID", "PNB", "RECLTD", "RELIANCE", "SBILIFE", "MOTHERSON", "SHREECEM", "SHRIRAMFIN", "ENRIN", "SIEMENS", "SOLARINDS", "SBIN"]'::jsonb
)
ON CONFLICT (id) DO NOTHING;
