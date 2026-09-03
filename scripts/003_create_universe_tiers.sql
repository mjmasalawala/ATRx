-- Adds market-cap tiers to the universe: large cap (existing), mid cap
-- (NSE Nifty Midcap 150), small cap (NSE Nifty Smallcap 100). Run once
-- manually in Neon's SQL Editor (or via psql), after 001/002.
--
-- This is a NEW table (screener_universes, plural) rather than an ALTER of
-- the old singleton screener_universe -- keeps this additive and leaves
-- the existing table/data untouched. db_store.py's tiered load/save reads
-- from this table; screener_universe is no longer read by the app once
-- this migration lands, but nothing here deletes it.
--
-- Each tier is capped at ~80 symbols (not the full 150/100 from the
-- source indices) so a screener run on any tier still reliably finishes
-- inside Vercel's 60s Hobby-plan function timeout at Kite's 3 req/sec
-- historical-data rate limit. Safe to re-run: ON CONFLICT DO NOTHING
-- won't overwrite a tier the app has since updated.

CREATE TABLE IF NOT EXISTS screener_universes (
    tier TEXT PRIMARY KEY,
    note TEXT,
    symbols JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO screener_universes (tier, note, symbols) VALUES (
    'large_cap',
    'Your large-cap trading universe -- edit anytime, this doesn''t need to change daily. Generated from the first 80 constituents of NSE''s Nifty 100 index, as a market-cap-ranked stand-in for ''market cap > 5000cr'' (every Nifty 100 name is far above that threshold) -- capped at 80 rather than the full 150+ we''d like, because on Vercel''s free Hobby plan a single function run is hard-limited to 60s and Kite''s 3 req/sec historical-data rate limit alone eats ~0.5s per symbol.',
    '["ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT", "DMART", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG", "BANKBARODA", "BEL", "BPCL", "BHARTIARTL", "BOSCHLTD", "BRITANNIA", "CGPOWER", "CANBK", "CHOLAFIN", "CIPLA", "COALINDIA", "CUMMINSIND", "DLF", "DIVISLAB", "DRREDDY", "EICHERMOT", "ETERNAL", "GAIL", "GODREJCP", "GRASIM", "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HAL", "HINDUNILVR", "HINDZINC", "HYUNDAI", "ICICIBANK", "ITC", "INDHOTEL", "IOC", "IRFC", "INFY", "INDIGO", "JSWSTEEL", "JINDALSTEL", "JIOFIN", "KOTAKBANK", "LTM", "LT", "LODHA", "M&M", "MARUTI", "MAXHEALTH", "MAZDOCK", "MUTHOOTFIN", "NTPC", "NESTLEIND", "ONGC", "PIDILITIND", "PFC", "POWERGRID", "PNB", "RECLTD", "RELIANCE", "SBILIFE", "MOTHERSON", "SHREECEM", "SHRIRAMFIN", "ENRIN", "SIEMENS", "SOLARINDS", "SBIN"]'::jsonb
)
ON CONFLICT (tier) DO NOTHING;

INSERT INTO screener_universes (tier, note, symbols) VALUES (
    'mid_cap',
    'Mid-cap trading universe -- the first 80 constituents of NSE''s Nifty Midcap 150 index (rank ~101-250 by free-float market cap), capped at 80 for the same 60s function-timeout reason as large_cap.',
    '["360ONE", "3MINDIA", "ACC", "AIAENG", "APLAPOLLO", "AUBANK", "AWL", "ABBOTINDIA", "ATGL", "ABCAPITAL", "AJANTPHARM", "ALKEM", "ANTHEM", "APARINDS", "APOLLOTYRE", "ASHOKLEY", "ASTRAL", "AUROPHARMA", "AIIL", "BSE", "BAJAJHFL", "BALKRISIND", "BANKINDIA", "MAHABANK", "BERGEPAINT", "BDL", "BHARATFORG", "BHEL", "BHARTIHEXA", "GROWW", "BIOCON", "BLUESTARCO", "CRISIL", "COCHINSHIP", "COFORGE", "COLPAL", "CONCOR", "COROMANDEL", "DABUR", "DALBHARAT", "DIXON", "ENDURANCE", "ESCORTS", "EXIDEIND", "NYKAA", "FEDERALBNK", "FORTIS", "GVT&D", "GMRAIRPORT", "GICRE", "GLAXO", "GLENMARK", "MEDANTA", "GODFRYPHLP", "GODREJIND", "GODREJPROP", "FLUOROCHEM", "HDBFS", "HAVELLS", "HEROMOTOCO", "HEXT", "HINDPETRO", "POWERINDIA", "HONAUT", "HUDCO", "ICICIGI", "ICICIAMC", "ICICIPRULI", "IDFCFIRSTB", "ITCHOTELS", "INDIANB", "IRCTC", "IREDA", "INDUSTOWER", "INDUSINDBK", "NAUKRI", "IPCALAB", "JKCEMENT", "JSWENERGY", "JSWINFRA"]'::jsonb
)
ON CONFLICT (tier) DO NOTHING;

INSERT INTO screener_universes (tier, note, symbols) VALUES (
    'small_cap',
    'Small-cap trading universe -- the first 80 constituents of NSE''s Nifty Smallcap 100 index, capped at 80 for the same 60s function-timeout reason as large_cap.',
    '["AARTIIND", "ABREL", "AEGISLOG", "AFCONS", "AFFLE", "ARE&M", "AMBER", "ANANDRATHI", "ANANTRAJ", "ANGELONE", "APTUS", "ASTERDM", "ATHERENERG", "BEML", "BLS", "BANDHANBNK", "FIRSTCRY", "BRIGADE", "CESC", "CGCL", "CASTROLIND", "CDSL", "CHAMBLFERT", "CHOLAHLDNG", "CUB", "COHANCE", "CAMS", "CREDITACC", "CROMPTON", "DATAPATTNS", "DEEPAKFERT", "DELHIVERY", "DEVYANI", "LALPATHLAB", "FSL", "FIVESTAR", "FORCEMOT", "GRSE", "GLAND", "GPIL", "GESHIP", "GMDCLTD", "HBLENGINE", "HSCL", "HINDCOPPER", "IDBI", "IFCI", "IIFL", "IRCON", "ITI", "IGL", "INOXWIND", "IKS", "JBMA", "JMFINANCIL", "JSWCEMENT", "JYOTICNC", "KARURVYSYA", "KAYNES", "KEC", "KFINTECH", "MANAPPURAM", "MRPL", "MEESHO", "NATCOPHARM", "NBCC", "NH", "NAVINFLUOR", "NETWEB", "NEULANDLAB", "NUVAMA", "OLAELEC", "PGEL", "PNBHOUSING", "PWL", "PINELABS", "PIRAMALFIN", "PPLPHARMA", "POONAWALLA", "RBLBANK"]'::jsonb
)
ON CONFLICT (tier) DO NOTHING;
