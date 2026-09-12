# Breakout Screener module ("N-of-M block staircase")

A second, independent strategy alongside the ATRx support-level screener. Finds stocks whose price has risen in a "staircase": split the trailing `period_days * n_blocks` trading days into `n_blocks` consecutive blocks, reduce each block to one number (`block_value`), and fire when at least `min_up_periods` of the block-to-block comparisons are up. Default: 3-day blocks, 10 blocks (30 trading days), 8 of 9 comparisons up, block value = lowest close in the block (a rising-lows/uptrend-structure reading, not last-close momentum).

Shares nothing with `config.py`/`levels.py`/`backtest.py`/`screener.py` — separate config object, separate signal logic, separate persisted tables (`breakout_config`, `breakout_signal_events`, not `screener_config`). Does share `db_store.py`'s universe tiers (`screener_universes`) and the Kite session/rate-limiting pattern.

## `breakout_signal.py`

Pure, dependency-free (no Kite, no DB, no config singleton) — the only file that answers "did the staircase pattern hold on this day."

- `SignalResult` (dataclass): `fired`, `n_blocks`, `up_periods`, `required_up_periods`, `block_values` (oldest first), `block_end_dates`, `window_start_idx`, `window_end_idx`.
- `evaluate_at(candles, end_idx, period_days, n_blocks, min_up_periods, block_value="min_close") -> SignalResult | None` — evaluates using only rows up to and including `end_idx` (no lookahead by construction). Returns `None` if there isn't enough history before `end_idx` to fill `n_blocks` full blocks.
- `scan_all(candles, ...) -> list[SignalResult]` — evaluates at every valid `end_idx`, oldest first. Used by both the in-window backtest and (via its tail) the live scan, so both paths run identical logic.
- `dedupe_consecutive(results) -> list[SignalResult]` — collapses a run of consecutive fired days (trailing-window re-alignment can keep a staircase "fired" for several days running) down to just the first day of each run, so that counts as one signal event, not one per day.
- `BLOCK_VALUE_FUNCS` — `min_close` (default), `max_close`, `last_close`, `mean_close`.

## `breakout_config.py`

`BreakoutConfig` (dataclass) — signal (`period_days=3`, `n_blocks=10`, `min_up_periods=8`, `block_value="min_close"`), data (`history_trading_days=180`), scan (`signal_recent_days=3`), backtest (`forward_horizons=(3,5,10,20)`), filters (`min_avg_turnover_cr=5.0`, `min_price=20.0`, `max_circuit_days=1`, `min_window_gain_pct=5.0`, `max_window_gain_pct=50.0`, `require_breakout_confirm=False`, `breakout_high_days=60`).

- `lookback_days() -> int` — always `period_days * n_blocks`, never entered directly (no partial-block ambiguity).
- `signal_config_hash() -> str` — sha256 (first 16 hex chars) of only `SIGNAL_HASH_FIELDS` (`period_days`/`n_blocks`/`min_up_periods`/`block_value`) — the key persisted evidence is grouped by. Changing a filter or a display setting doesn't fragment previously-collected evidence for the same underlying signal definition.
- `config_from_dict(data) -> BreakoutConfig` — builds from a posted/persisted dict, ignoring unknown keys, defaulting anything missing.
- `TUNABLE_FIELDS` — what the web UI exposes. `CONFIG` — the module-level singleton, mutated transiently via `breakout_screener._config_overrides` the same way `screener.py` does for `config.CONFIG`.

## `breakout_filters.py`

Tradability/quality filters, each `(window_df, cfg) -> (passed: bool, reason: str | None)`, mirroring `screener.py`'s `_rejection_reasons` pattern — every filtered-out signal keeps its reason for display.

- `filter_min_price`, `filter_min_avg_turnover` (skips if no volume column), `filter_circuit_days` (a day where `close == high` AND `|move| >= 4.5%` is "circuit-like" — a proxy since Kite doesn't expose the actual circuit band), `filter_window_gain` (both a floor and a ceiling — too little gain is noise, too much reads as parabolic/exhaustion), `filter_breakout_confirm` (only active when `require_breakout_confirm=True`; no-lookahead — only looks at data up to and including the signal day).
- `apply_filters(candles, window_start_idx, window_end_idx, cfg) -> list[str]` — runs every filter, returns all failure reasons (not just the first).

## `breakout_screener.py`

Orchestrates one run: fetch → signal + filters per symbol → live candidate tagging → in-window backtest with control returns → persist events → return everything the page needs. Mirrors `screener.py`'s shape (rate-limited fetch spacing request *starts* 1/3s apart, per-symbol breakdown covering every universe symbol regardless of outcome) but is fully independent code.

- `resolve_tokens`, `fetch_history` (fetches `history_trading_days` trading days via a calendar-day buffer, then trims to the exact trailing window so every symbol's window is directly comparable regardless of its own holiday pattern), `fetch_universe_history` (rate-limited loop).
- `_control_return(history, signal_idx, horizon)` — equal-weighted mean return of every *other* fetched symbol in the same tier over the same positional window. Needs no extra data since every symbol shares the same trailing window length.
- `_build_symbol_events(symbol, candles, history, cfg) -> list[dict]` — every deduped historical firing for one symbol, with `fwd_ret_<h>`/`control_ret_<h>` computed wherever that horizon has elapsed within the fetched window (else `None`), and `filter_reasons` recorded regardless of pass/fail (a filtered signal is still evidence for "would this have worked").
- `build_symbol_breakdown(symbol, candles, history, cfg) -> dict` — one row per symbol: `status` (`candidate`/`filtered_out`/`signal_in_window`/`no_signal`/`insufficient_data`), current price, latest block values, `structural_stop` (= the last block's `min_close` — the signal's own definition of "trend intact", used as the natural stop rather than an arbitrary ATR multiple), distance to stop, and this symbol's own historical `events`.
- `run_breakout_screener(kite, overrides=None, universe_tier="large_cap") -> dict` — full run against an authenticated session; wraps `_run_breakout_screener` in `_config_overrides`, then persists the config used (non-fatal if DB is down).
- `_run_breakout_screener` — loads the tier's universe from `db_store.load_universe` (no `universe.json` fallback — unlike `config.Config.load_universe`, this strategy has no legacy local file), fetches, builds every symbol's breakdown, persists events via `db_store.upsert_signal_events`, sorts by `STATUS_ORDER` (candidates first), returns the full result dict including `signal_config_hash` and `n_events_persisted`/`persist_error`.

## `breakout_stats.py`

Pure arithmetic on a list of persisted/in-run event dicts — no I/O.

- `horizon_summary(events, horizon) -> dict` — `n`, `hit_rate_pct`, `avg_fwd_ret_pct`, `avg_control_ret_pct`, `avg_excess_ret_pct`, `excess_ci` (a stdlib-only percentile bootstrap 95% CI on the mean excess return, `None` below 5 events).
- `summarize(events, horizons=(3,5,10,20)) -> dict` — `by_horizon`, plus a `verdict_tag` (`edge`/`no_edge`/`insufficient_data`) and a plain-English `verdict_text`, built off the 10-day horizon's excess-return CI (edge = CI entirely above zero; no_edge = CI entirely at/below zero or spans zero; insufficient_data = fewer than 30 events with that horizon observed).

## `scripts/011_create_breakout_tables.sql`

- `breakout_config` — singleton last-used-params row, same shape as `screener_config`.
- `breakout_signal_events` — one row per `(symbol, signal_date, signal_config_hash)`. `universe_tier`, `entry_price`, `window_gain_pct`, `up_periods`/`n_blocks`, `block_values`/`filter_reasons` (JSONB), and nullable `fwd_ret_<h>`/`control_ret_<h>` columns for h in 3/5/10/20. A later run touching the same `(symbol, date, hash)` overwrites the row (full overwrite, not a null-coalescing merge — historical prices don't change between runs, so re-computation only ever fills in previously-unavailable forward returns as more calendar time elapses).

## `db_store.py` — breakout-related functions

Constants: `BREAKOUT_CONFIG_TABLE="breakout_config"`, `BREAKOUT_SIGNAL_EVENTS_TABLE="breakout_signal_events"`, `_BREAKOUT_HORIZONS=(3,5,10,20)` (must match `BreakoutConfig.forward_horizons`'s default).

- `load_breakout_config() -> dict | None`, `save_breakout_config(params) -> None` (singleton row, same pattern as `save_config`).
- `upsert_signal_events(rows: list[dict]) -> int` — batch upsert keyed on `(symbol, signal_date, signal_config_hash)`.
- `load_signal_events(signal_config_hash, universe_tier=None) -> list[dict]` — everything persisted for one signal definition (optionally filtered to one tier), newest `signal_date` first.

## `api/index.py` routes

- `breakout_page()` → `/breakout`, serves `breakout.html`.
- `breakout_config_endpoint()` → `/api/breakout-config` — `GET` returns the last-persisted config (or `BreakoutConfig()` defaults); `POST` (JSON body = tunable fields) validates via `config_from_dict` and persists without running a scan.
- `run_breakout_endpoint()` → `/api/run-breakout`, `POST` (`{overrides, universe_tier}`) — same login-gate/error-handling shape as `run_screener_endpoint`.
- `breakout_stats_endpoint()` → `/api/breakout-stats?hash=<signal_config_hash>&tier=<tier>` (tier optional) — loads persisted events and returns `breakout_stats.summarize(...)`.

## `breakout.html`

Same sidebar/header/config-panel/cache-then-refresh scaffolding as `index.html` (duplicated, not shared — same convention as every other ATRx page). Differences:

- Config panel groups: Staircase signal (`period_days`/`n_blocks`/`min_up_periods`/`block_value` — the last two rendered as `<select>`), Data & scan, Tradability filters, Quality filters (`require_breakout_confirm` rendered as a checkbox). A derived-lookback note (`period_days × n_blocks`) recomputes live as those two fields change, with a warning if `history_trading_days` doesn't comfortably exceed lookback + the longest forward horizon.
- A **verdict card** above the results table, driven by `/api/breakout-stats`, with its own `refreshStatsBtn` and its own localStorage cache key (`atrx_breakout_stats_cache_v1`) — separate from the run cache (`atrx_breakout_cache_v1`) since it reflects everything ever persisted for the current `signal_config_hash`, not just this run.
- Results table columns: symbol, price, up periods (`x/required`), structural stop, distance to stop, historical-signal count, status. Expanding a row shows the latest block-value chain (arrows colored by up/down) and a table of every historical signal found for that symbol in the fetched window, each with its forward returns per horizon and a "Log Trade" button (only on signals that passed every filter).
- `PerformanceLog.init({ strategy: "breakout_staircase" })` — a distinct strategy slug from `atrx_screener`, so Performance Log's win-rate stats track the two strategies separately.

## Cost/timing note

One run fetches `history_trading_days` (default 180) of daily history for every symbol in the selected tier — one Kite historical-data request per symbol, not per day, rate-limited to 3/sec the same way `screener.py` is. For an ~75-80 symbol tier that's roughly 25-30 seconds, comfortably inside Vercel's 60s Hobby cap.
