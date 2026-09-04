-- Schema migration for the WhatsApp candidate-alert agent. Run manually in
-- Neon's SQL Editor (or via psql) -- the app never creates or alters these
-- tables itself, only reads/writes rows in them.

-- One row per NSE trading holiday (equity/CM segment). Refreshed once a
-- year by /api/sync-nse-holidays. The candidate-scan endpoint reads this
-- (plus a plain weekend check) to decide whether "today" is a trading day.
CREATE TABLE IF NOT EXISTS nse_holidays (
    holiday_date DATE PRIMARY KEY,
    description TEXT
);

-- One row per (date, symbol, level) already WhatsApp-alerted, so the
-- every-15-min scan only messages about a candidate once per day even
-- though it may keep qualifying across many runs. Cleared implicitly by
-- date -- old rows are harmless to keep, but feel free to prune.
CREATE TABLE IF NOT EXISTS screener_alerts_sent (
    alert_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    level NUMERIC NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (alert_date, symbol, level)
);
