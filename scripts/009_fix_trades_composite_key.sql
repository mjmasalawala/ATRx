-- Zerodha trade IDs are only unique WITHIN an exchange, not globally --
-- the same numeric trade_id can independently occur on both NSE and BSE
-- for genuinely different trades. cost_basis_trades used trade_id alone
-- as its primary key, so whenever a symbol was traded on both exchanges,
-- a real trade on one exchange could collide with an already-stored
-- trade_id from the other and get silently dropped by upsert_trades'
-- ON CONFLICT (trade_id) DO NOTHING -- looking exactly like a missing
-- buy and producing a bogus "negative quantity" oversell (confirmed:
-- ARE&M, traded on both NSE and BSE). Run once manually in Neon's SQL
-- Editor, after 001-008.
--
-- IMPORTANT: after running this, re-upload every trades CSV you have.
-- Any trade previously dropped by the trade_id collision was never
-- actually stored -- there's nothing here to "recover," it needs to be
-- re-imported now that the conflict check is scoped correctly.
--
-- If the ALTER COLUMN below fails ("column contains null values"), some
-- already-stored trade has no exchange recorded -- run
-- `SELECT * FROM cost_basis_trades WHERE exchange IS NULL;` to see which,
-- and decide there (rather than guessing here) whether to backfill or
-- delete those rows before re-running this migration.

ALTER TABLE cost_basis_trades ALTER COLUMN exchange SET NOT NULL;
ALTER TABLE cost_basis_trades DROP CONSTRAINT cost_basis_trades_pkey;
ALTER TABLE cost_basis_trades ADD PRIMARY KEY (trade_id, exchange);
