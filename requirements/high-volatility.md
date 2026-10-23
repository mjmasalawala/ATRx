# High Volatility module (intraday first-hour trading helper)

A page (`/high-volatility`, `high_volatility.html`) that supports one specific, fully manual trading routine. **It never places orders.** Everything it shows is a prompt to look; every trade is placed by the user in Kite and then logged here.

## Build status

| Piece | Status |
|---|---|
| `scripts/012_create_high_volatility_tables.sql` | written; **user must run it once in Neon's SQL Editor** before the page can save anything |
| `scripts/013_high_volatility_signals_and_risk.sql` | written; **user must run it in Neon** before the signal-based live tab works (new `hv_positions` columns, `hv_watch_context`, `hv_signals`) |
| Signal-based live tab, risk limits, signal log + replay (`hv_signals.py`, `hv_risk.py`, `hv_live.py`) | built, unit-tested (47 hv tests), smoke-tested locally against a fake Kite/DB; **not yet run against live Kite/Neon** |
| ATR snapshot persistence from the screener run | built (`screener.py` `save_atr_snapshot`, see below) |
| `hv_config.py`, `hv_ranking.py`, `hv_journal.py`, `hv_screener.py` (+ `tests/test_hv_*.py`) | built, 22 tests pass |
| `db_store.py` hv functions | built |
| API routes + `vercel.json` rewrites | built |
| `high_volatility.html` + sidebar link on every page | built; smoke-tested locally against a fake Kite/DB, **not yet run against live Kite/Neon** |
| End-of-day tradebook import, analytics | not built -- see "Reminder for the user" |

## Files and signatures

- `hv_config.py` — `HVConfig` dataclass (all tunables, persisted in `hv_settings`), `config_from_dict(overrides) -> HVConfig` (ignores unknown keys, coerces types), `TUNABLE_FIELDS`, `CHARGES` (Zerodha rate constants).
- `hv_journal.py` — pure P&L math: `fill_charges(side, kind, qty, price)`, `position_summary(side, fills) -> dict` (avg entry/exit, open qty, gross/charges/net, net %), `target_price(side, avg_entry, qty, net_pct)` and `break_even_price(...)` (iterative because exit charges depend on the exit price), `day_summary(positions, cfg)` (round trips, loss-limit/done flags), `build_journal(positions, cfg)` (attaches `summary`, per-day roll-up with cumulative net, totals). Charges on a partially exited position are prorated: only the exited share of the entry charges is counted until the rest is closed. **`high_volatility.html` has a JS mirror of `fill_charges`/`target_price` (for live cards) -- change both together.**
- `hv_ranking.py` — pure: `count_swings(prices, pct)` (zigzag leg counter), `intraday_stats(candles_5m, cfg, today)` (per-ticker stats over completed sessions: first-hour range, up/down swing counts, `two_up_days_pct`/`two_down_days_pct`, whipsaw, last-session volume ratio, turnover, ATR% from 5-min-derived daily bars), `rank_candidates(rows, cfg) -> (ranked, excluded)`.
- `hv_screener.py` — Kite/DB plumbing: `build_candidates(kite, cfg, extras, force)`, `fetch_5min`, `get_quotes`, `normalise_quote`, `today_ist()`. Stops fetching new candles after ~42 s (Vercel's 60 s cap) and returns the rest in `pending`; stats are cached per symbol on `hv_atr_snapshot.intraday_stats` for the day (cache is invalid if `swing_pct`/`first_hour_minutes` changed), so pressing "Build watchlist" again continues where it stopped. Extra tickers not in the snapshot are never cached.
- `hv_signals.py` — pure setup detector for the live tab (long side only). `build_context(history_5m, today)` (previous day H/L/C, daily ATR(14), median volume per 5-minute slot over 10 sessions, avg first-hour 5-min traded value for the liquidity cap, last 10 closes for RSI), `evaluate(bars_1m, ctx, quote, nifty, cfg, now, one_r, exposure_left, size_mult)` -> `{stage: watching|stretched|armed|entry|blocked|closed, reasons, day_type, metrics, checks, suggestion, episode}`, `nifty_state`, `day_type`, `manage_position` (prompts: move stop at +1R, sell half at VWAP, trail under a 5-min higher low, second leg after a swing higher low; warnings: VWAP rejection with RSI>70, Nifty new lows, 1.5% from the lower band, time stop, flat-by), `score_signal` (not_filled / target / stop / timeout in R; target and stop in one bar = stop), `replay_day`, `signal_summary`. Candles are dicts with naive-IST `ts`. Thresholds are HVConfig fields; the reasoning is in the "High Volatility Strategy Risk Review" doc (claude.ai artifact ERF99qVXBDyrNSEvffXW17).
- `hv_risk.py` — pure: `size_trade` (min of risk / liquidity / exposure caps; loss per share includes `cost_pct`), `day_state` (day R, loss streak, give-back, trips, open risk, exposure, `lockout`, `cooloff_until`), `entry_blockers`, `period_state` (weekly / monthly / rolling-20, `size_mult` 0.5 after a losing week or negative rolling-20, `monthly_stop`), `position_r`, `to_ist_naive`.
- `hv_live.py` — Kite/DB plumbing: `start_watch` (5-min history -> context rows in `hv_watch_context`, plus NIFTY 50's token), `live_snapshot` (quotes + today's 1-minute candles per watched stock and Nifty, evaluate each card, apply risk lockout/blockers, record armed/entry signals, manage open positions), `risk_snapshot`, `score_day`, `replay` (≈60 calendar days of 1-minute data for one stock; days with 10+ earlier sessions).
- `db_store.py` — `upsert_atr_snapshot`, `load_atr_snapshot`, `save_intraday_stats`, `load_hv_settings`/`save_hv_settings`, `load_hv_day`/`save_hv_day` (replaces the day's whole focus list), `create_hv_position`, `add_hv_fill`, `update_hv_position`, `delete_hv_position`, `delete_hv_fill`, `list_hv_positions(from, to=None)` (now with `planned_stop`, `risk_amount`, `rule_broken`, `rule_note`, `signal_id`), `get_hv_position`, `save_watch_context` / `load_watch_context`, `record_hv_signal` (once per source/day/symbol/stage/episode), `list_hv_signals`, `set_hv_signal_action`, `save_hv_signal_scores`, `replace_hv_replay_signals`.
- `screener.py` — `run_screener(..., save_atr_snapshot=False)`; only `/api/run-screener` (the manual Find ATRx Opps / ATRx Screener runs) passes `True`. The 25-minute alert cron does not. **The snapshot save is skipped during NSE market hours** (9:15–15:30 IST weekdays) because Kite's daily history then includes today's partial candle, which would skew ATR%.
- `api/index.py` — routes `hv-settings`, `hv-candidates`, `hv-live`, `hv-watch-start`, `hv-gift-nifty`, `hv-day`, `hv-position-open`, `hv-fill`, `hv-fill-delete`, `hv-position-update`, `hv-position-delete`, `hv-journal`, `hv-signals` (GET list + summary, POST scores a day), `hv-signal-action` (skipped/taken), `hv-replay`, plus the page route `high-volatility`. `hv-settings` refuses changes to `RISK_FIELDS` from 9:15 to 15:30 IST on weekdays (409). `hv-position-open` never refuses a trade, but records it as a rule break (`rule_note`) when a risk blocker applies, there's no planned stop, the risk is over 1R, or the suggestion said skip. `hv-position-open` returns 409 if the ticker already has an open position today (use Add/Exit); `hv-fill` rejects exits larger than the open quantity and entries on a closed position.

## The strategy this serves

- Before the open (~8:30–9:15) the user picks **2–3 tickers** expected to be volatile (whipsaw is best, one-directional is fine).
- In the **first hour** they buy weakness after the open and sell the bounce for **~1–3% net**, then repeat the same round trip on the same ticker once more later in the day. Longs are preferred; shorts (intraday only) are also used.
- Target: **3–4 winning round trips across 2–3 tickers**, then stop for the day.
- Position size is **risk-based** (since the risk review, Oct 2026): 1R = `capital × risk_pct` (₹2,00,000 × 0.25% = ₹500 for the first 20 trades, then 0.5%); shares = min(1R ÷ (entry − stop + 0.2% costs), 1% of avg 5-min traded value ÷ entry, exposure left ÷ entry). A second leg is allowed only after a higher low, within the same 1R. Daily stop −2% of capital, 3 losses in a row, half of a +4R peak given back, 8 round trips, or any rule break ends the day; entries 9:20–14:30 (a 30-minute time stop finishes before the 15:00 flat time). Longs only for the first month.
- Worked example (22 Oct 2026, ETERNAL): opened low, dipped ~1%; bought 306 → sold 313 (+2.3%); bought 310 → sold 315 (+1.6%).
- Pre-open clues the user uses (all manual, not fetched): results, GIFT Nifty, news, broker calls.

## Data flow

1. **Morning:** the user runs *Find ATRx Opps* as usual. `screener.py`'s `_run_screener` already computes `atr_pct` and `atr_percentile_rank` for every symbol in pass 1; the change is to also **upsert them into `hv_atr_snapshot`** (non-fatal, like `_persist_config_used`). Only the **latest** value per symbol is kept (one row per symbol, ~240 rows) — deliberate, to avoid an ever-growing table. History is not needed: the ATR%/rank at selection time is copied into the trade's `signal_meta` / focus `pre_meta` instead.
2. **Pre-open tab:** `GET /api/hv-candidates` loads the snapshot, shortlists the top N by ATR% (after turnover and circuit-band filters), fetches **5-minute candles** (~25 calendar days) for the shortlist only, derives first-hour/swing statistics, caches them on the snapshot row (`intraday_stats`, `intraday_stats_date`) so reloads are instant, quotes the shortlist (`kite.quote`) for the pre-open indicative gap, and returns a ranked list with a "why" column. Extra tickers typed by the user (free text NSE tickers) are **never filtered** (ATR%, turnover and price-band checks are skipped, since they only add a ticker they expect to be volatile) and are included, computed on the fly and not persisted. ATR% for those is derived from the 5-minute candles aggregated to daily bars.
   - **GIFT Nifty:** `GET /api/hv-gift-nifty` (`hv_screener.fetch_gift_nifty`) reads the latest print from the `__NEXT_DATA__` JSON embedded in Groww's GIFT Nifty page (`groww.in/indices/global-indices/sgx-nifty`, no login, undocumented so it may change). The Pre-open tab auto-fills the field when it's empty and has a "Fetch live" button; typing by hand still works if the feed is down.

3. **Live tab:** "Start watch" (`POST /api/hv-watch-start`, also triggered automatically for any watched stock without context) stores each stock's context for the day. The browser then polls `GET /api/hv-live?symbols=<focus>&strip=<top shortlist>` **every 30 s**: one Kite quote call plus one 1-minute candle call per stock and one for NIFTY 50 (≈10 calls at 3/s). Each card shows the stage, a checklist with info tooltips, and in Entry zone the suggested limit price, quantity, stop, target and net R:R. Armed/entry signals are written to `hv_signals`. Polling and alerts only run while the tab is open.
4. **Trades:** "I'm in" on an Entry zone card logs the position with its planned stop, 1R and `signal_id` (marks the signal taken); "Skip" marks it skipped; an untouched entry signal is marked lapsed when scored. Exits / second legs are logged from the in-trade card.
5. **Signals tab:** `POST /api/hv-signals` scores the day's live signals (entry signals once their 30 minutes have passed; armed ones as confirmed / no_confirm); `GET` lists them with hit rate and average R by action, confirmation and day type. Replay (`POST /api/hv-replay`, one stock per call) stores `source='replay'` rows, replacing that stock's earlier replay rows.

## Ranking (pre-open)

Shortlist filters: `atr_pct >= min_atr_pct`, average turnover `>= min_avg_turnover_cr`, circuit band (from quote upper/lower limits vs prev close) `>= min_circuit_band_pct` (5% bands cap the move). Then a weighted score of min-max-normalised components across the shortlist:

- average **first-hour range %** (from 5-min history),
- **swing frequency**: share of recent days with at least two swings of `>= swing_pct` in the user's direction (`two_up_days_pct` / `two_down_days_pct`) — essentially the user's strategy backtested per ticker,
- ATR% (reused from the snapshot),
- **whipsaw score** = 1 − efficiency ratio (|net move| / total path travelled),
- catalyst: absolute pre-open gap %, yesterday's volume vs average, plus a fixed bonus per ticked tag (results / news / broker_buy / broker_sell).

Tag bonuses are applied client-side so ticking a tag re-ranks instantly. The user's GIFT Nifty value is typed in by hand; the page compares it with Nifty's previous close (from the quote) to show the implied gap.

## Charges and P&L

Net P&L is what the dashboard headlines. All figures are **derived at read time, never stored**: `hv_journal.py` computes per-position average entry, open qty, gross P&L, charges and net P&L/% from the fills. Charges model (Zerodha equity intraday, rates held as constants in `hv_config.py`, **verify against Zerodha's brokerage calculator**): brokerage `min(₹20, 0.03%)` per order, STT 0.025% on the sell side, NSE transaction charge ~0.00297% both sides, SEBI fee ₹10/crore, stamp duty 0.003% on the buy side, GST 18% on brokerage + transaction + SEBI. Roughly ₹37–38 per ₹35k round trip (~0.11%), so the target calculator shows prices at +1/+2/+3% **net** and the break-even price. Shorts mirror this (entry is the sell, exit is the buy-to-cover).

## Schema — `scripts/012_create_high_volatility_tables.sql`

- `hv_atr_snapshot(symbol PK, tier, atr_pct, atr_percentile_rank, price, atr_period, run_date, intraday_stats JSONB, intraday_stats_date, updated_at)` — one row per symbol, overwritten.
- `hv_settings(id=1, params JSONB)` — singleton tunables (amount per trade, daily loss limit, thresholds, poll interval, alert thresholds, target percentages).
- `hv_day(trade_date PK, gift_nifty, note)` — pre-open notes.
- `hv_focus(trade_date, symbol PK, bias long|short|either, tags JSONB, note, pre_meta JSONB)` — the day's 2–3 picks with clues.
- `hv_positions(id, trade_date, symbol, side long|short, setup_tag, note, execution_rating clean|rushed, signal_meta JSONB)` — one per round trip.
- `hv_fills(id, position_id → hv_positions ON DELETE CASCADE, kind entry|exit, qty, price, filled_at)` — scale-ins are extra `entry` fills; exits may be partial. A position is open while entry qty > exit qty.

Per the repo rule, `db_store.py` only reads/writes rows; it never creates or alters these tables.

## API routes (each has a `_ROUTES` entry + `vercel.json` rewrite)

`/high-volatility` (page), `hv-settings`, `hv-candidates`, `hv-live`, `hv-gift-nifty`, `hv-day` (GET/POST: notes + focus list), `hv-position-open`, `hv-fill`, `hv-fill-delete`, `hv-position-update`, `hv-position-delete`, `hv-journal` (positions, per-day summaries, totals for the dashboard).

## Page behaviour (`high_volatility.html`)

Four tabs, self-contained like every other page, with a shared `.card` style defined once (variants incl. `card--entry`), one `.pill` style with a variant per stage, and one `.tip` icon + `#tipBox` for tooltips (texts in the `TIPS` constant, from the risk review's "Metric tooltips" table). A risk bar above the tabs shows day R, open risk, round trips, loss streak, exposure, 1R, week/month, and the cool-off / Day stopped / flat-soon banners.

1. **Pre-open** — GIFT Nifty input and implied gap, extra-ticker box, ranked table with tag chips, bias, note, "focus" checkbox (autosaved).
2. **Live** — Nifty state line, one card per focus ticker (stage pill, checklist: day type, stretch, exhaustion with RSI/volume/wick/level, confirmation, spread, band distance; blocked reasons; suggested order with I'm in / Skip; in-trade prompts and warnings with Closed / Add second leg), an "Also setting up" strip for the rest of the shortlist, and a day summary after the flat time or a lockout. Alerts: soft chime on Setup armed, loud on Entry zone and exit warnings, once per symbol/episode/day.
3. **Journal & dashboard** — Today summary (trades, win rate, R, best/worst, rule breaks, today's signals), headline **net profit** and total R, cumulative net-P&L line, per-day table (R, W/L, rule breaks), and the position list (stop, R, rule-broken badge) with editable tag / planned stop / rating / rule broken / note.
4. **Signals** — signal summary tables and the signal list with outcome badges; Score today; Replay.

## Reminder for the user — analytics to build later

The user decided win rate by ticker and by time of day are *analytics*, not dashboard headlines, and asked to be reminded. Candidates (data is already captured by the schema above — `filled_at` times, side, setup tag, clue tags in `pre_meta`/`signal_meta`):

- win rate and net ₹ by ticker and by time-of-day bucket,
- long vs short performance,
- which pre-open clue (results / news / broker / gap) actually predicts good days,
- ATR rank at selection vs outcome,
- trade number within the day (does the 4th trade help or hurt?),
- effect of scale-ins (averaging down) on outcome,
- optional end-of-day Zerodha tradebook CSV import (reuse `trade_csv_import.py`) to correct logged prices/times with real fills.

## Gotchas / open items

- The tab must stay open for polling and alerts.
- Shorting is intraday only and some stocks cannot be shorted; this is a free-text note, not enforced.
- Pre-open indicative prices from `kite.quote` between 9:00–9:08 are assumed to appear in `last_price`/`ohlc` — **verify on a live morning**.
- `amount_per_trade` and the stored `daily_loss_limit` are gone; the loss limit is `capital × daily_loss_pct`. Old persisted settings keys are ignored.
- Nifty's day type uses the NIFTY 50 **index** 1-minute candles with a time-weighted average (the index has no volume), not the Nifty future the risk review mentions -- simpler, and it works for replay.
- Hand-typed extras skip only the ATR% filter; failing turnover / price-band checks show as warning badges (`safety_warnings`), and the live card still blocks a band of 5% or less.
- The live tab uses the server's clock (`hv_live.now_ist`); the 1-minute candle still forming is dropped, so a signal can lag its bar by up to ~1 minute plus the poll interval.
- The liquidity cap uses the stock's median 5-minute traded value for the signal's time of day (`ctx.value_baseline`), so midday positions are sized smaller than opening ones. Persisted settings override code defaults: if `entry_end` / `flat_by` were saved earlier as 10:15 / 10:45, change them in Settings after 15:30.
- ASM/GSM and trade-to-trade stocks are not flagged (Kite doesn't mark them) -- a manual check.
- Rows for tickers in the ranked list that the user hasn't ticked are only kept in the browser (`localStorage`, per IST date); only focus tickers are saved server-side.
- Local testing without live Kite/Neon: the repo modules use PEP 604 hints (`dict | None`) without `from __future__ import annotations`, so they need Python ≥ 3.10 (Vercel's runtime has it; the Mac's system Python 3.9 does not). The `hv_*` modules do have the future import so their unit tests run on 3.9.
- Dates must be computed in IST (`ZoneInfo("Asia/Kolkata")`), as `candidate_alert.py` does — Vercel runs in UTC.
