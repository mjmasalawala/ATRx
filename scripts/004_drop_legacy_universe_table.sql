-- Cleanup: drops the original singleton screener_universe table (created
-- by 001, seeded by 002), superseded by screener_universes (one row per
-- market-cap tier) in 003. Nothing in the app reads or writes this table
-- any more -- db_store.py's load_universe/save_universe have targeted
-- screener_universes exclusively since the tiering migration landed.
--
-- Run manually in Neon's SQL Editor (or via psql) once you've confirmed
-- 003 is applied and the Universe dropdown works. Not run automatically,
-- and not bundled into 003, so dropping this data stays a deliberate,
-- reviewable step rather than a side effect of an unrelated migration.

DROP TABLE IF EXISTS screener_universe;
