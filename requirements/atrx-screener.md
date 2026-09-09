# ATRx Screener module

The core support-level bounce strategy: find pivot lows, cluster them into validated support levels, keep ones price is currently near, backtest each level's historical touches, score/rank, and surface the result to two frontend pages, a CLI, and a WhatsApp-alerting cron job. It never places orders — output is a list to manually review.

## `config.py`

`Config` (dataclass) — every tunable numeric parameter, grouped: Broker/auth (`api_key`, `api_secret`, `access_token_file`, `exchange`), Universe (`universe_file`), Volatility (`atr_period=7` — 7, not the conventional 14), Support-level detection (`pivot_window=5`, `lookback_days=60`, `cluster_atr_multiple=1.0`, `breach_buffer_atr=0.25`, `min_touches=3`, `max_breaches=1`, `recency_decay_days=60.0`), ATRx proximity filter (`atrx_lower=-0.3`, `atrx_upper=1.0`), Touch backtest (`touch_band_atr=0.5`, `forward_days_short=3`, `forward_days_long=5`), Volatility pre-filter (`min_atr_percentile=50.0`), Score weights (`score_w_return=0.5`, `score_w_proximity=0.3`, `score_w_recency=0.2`, `score_breach_penalty=0.15`), Output (`output_dir`, `top_n=25`).

- `Config.load_universe(self, tier: str = "large_cap") -> list[str]` — tries `db_store.load_universe(tier)` (Neon `screener_universes`) first; falls back to `universe.json` **only for `tier == "large_cap"`** (raises `RuntimeError` for any other tier if the DB is down — universe.json predates tiering).
- `Config.to_tunable_dict(self) -> dict` — `{field: value}` for every `TUNABLE_FIELDS` entry.
- `TUNABLE_FIELDS` — the subset of fields the web UI exposes for per-run overrides (excludes broker/auth/path fields).
- `CONFIG = Config()` — the module-level singleton every other module reads (and `screener.py` transiently mutates via a context manager, see below).

Score formula: `score = w_return*ret_z*hit_rate + w_proximity*proximity_component + w_recency*recency_component - breach_penalty*breaches`. All three components normalized ~0-1 across the candidate list before weighting.

## `indicators.py`

- `average_true_range_series(candles: pd.DataFrame, period: int) -> pd.Series` — the **full rolling ATR series**, not just the latest value (needed for breach detection and the touch backtest at every historical day, not just today).

## `levels.py`

Pipeline: pivots → clusters → scored levels.

- `Level` (dataclass): `price`, `touches`, `breaches`, `recency_weight`, `meets_criteria: bool = False` (touches ≥ min_touches AND breaches ≤ max_breaches), `pivots: list` (`[{"date": Timestamp, "price": float}, ...]`).
- `find_pivot_lows(candles, window) -> pd.DataFrame` — a day is a pivot low if its `low` is the strict minimum among `window` days on both sides (ties excluded). The most recent `window` days can never be confirmed as pivots yet.
- `_regroup(sorted_pivots, threshold) -> list[list[int]]` — chains pivots (sorted ascending by `low`) into clusters where each is within `threshold` of the *previous* pivot in the running group (chained proximity, not pairwise-to-center).
- `count_breaches(candles, level_price, current_atr, breach_buffer_atr) -> int` — counts **closing** prices below `level_price - current_atr*breach_buffer_atr`. Intraday wicks don't count.
- `build_levels(candles, current_atr) -> list[Level]` — **returns every cluster found**, not just qualifying ones — each is tagged `meets_criteria` instead of being silently dropped, so callers can explain a result, not just produce one. Level price = `median` of the group's lows. `recency_weight` = sum of `exp(-age_days/recency_decay_days)` over the cluster's pivots.

## `backtest.py`

- `TouchStats` (dataclass): `n_touches`, `hit_rate_short`, `hit_rate_long`, `avg_fwd_ret_short`, `avg_fwd_ret_long` (all `float | None`).
- `backtest_level(candles, level, current_atr) -> TouchStats` — a "touch" is any close within `touch_band_atr * current_atr` of the level (deliberately broader than the pivot-low definition, so short-lived tests that never became a full pivot still count). Forward returns computed at `pos + forward_days_short` and `pos + forward_days_long` (skipped past end of data). `hit_rate` = % positive; `avg_ret` = mean, both as percentages.

## `screener.py`

Orchestrates the full scan. CLI entrypoint (`python screener.py`) and reusable functions for the web API.

- `setup_logging()`, `resolve_tokens(kite, symbols) -> dict[str,int]` (via `kite.instruments`, skips+logs unresolved symbols), `fetch_history(kite, token, calendar_days) -> pd.DataFrame | None`.
- `current_atr_and_price(candles) -> tuple[float,float] | None` — `(current_atr, current_price)` or `None` if insufficient/non-positive data.
- `_rejection_reasons(lvl, atrx, in_range) -> list[str]` — human-readable reasons a level didn't qualify.
- `build_symbol_breakdown(symbol, candles) -> dict` — full per-symbol picture: ATR/price, all levels (with ATRx/backtest per level), `is_candidate` tag, sorted by `abs(atrx)`. `score` left `None` (filled later, it's a relative ranking).
- `score_candidates(rows) -> list[dict]` — adds `score`, sorts best-first.
- `_config_overrides(overrides: dict | None)` (contextmanager) — temporarily mutates `CONFIG` attrs named in `overrides` (validated against `TUNABLE_FIELDS`), restores originals on exit even on error.
- `run_screener(kite, overrides=None, universe_tier="large_cap") -> dict` — full universe-tier scan. Wraps `_run_screener` in `_config_overrides`, then calls `_persist_config_used()` (successful runs persist config to Neon as "last used").
- `run_screener_symbol(kite, symbol, overrides=None) -> dict` — single-ticker scan for the ATRx Stock page. Same `build_symbol_breakdown`→`score_candidates` pipeline, but **no** relative-volatility pre-filter (only makes sense across a universe) and its own `score_candidates` peer set (just this symbol's candidate levels). **Deliberately never calls `_persist_config_used()`** — experimenting here must not change what the tier scan starts from next time.
- `_persist_config_used()` — saves `CONFIG.to_tunable_dict()` via `db_store.save_config`; non-fatal (a run must not fail because bookkeeping did).
- `_run_screener(kite, universe_tier="large_cap") -> dict` — the real implementation. Loads universe, resolves tokens, fetches history with rate limiting (spaces request **start** times 1/3s apart, not a flat sleep-after — flat-sleep was pushing bigger universes like small_cap over the 60s Vercel Hobby timeout), two-pass pipeline (Pass 1: cheap ATR%-only across all symbols to define "volatile" as the top `(100-min_atr_percentile)`th percentile *within this universe*; Pass 2: full level detection+backtest for volatile symbols only), scores/ranks, builds `screening_breakdown` where **every** universe symbol gets a row regardless of outcome (`symbol_not_found` / `fetch_failed` / `insufficient_data` / `below_volatility_threshold` / `screened`) — this is what makes "no candidates" explainable instead of a dead end. `calendar_days` buffer = `int((lookback_days + pivot_window + atr_period + forward_days_long) * 1.3)`. Sort order (`STATUS_ORDER`): candidates-first (by score desc) → screened-no-candidate (by closest ATRx miss) → below-volatility → insufficient-data → fetch-failed → symbol-not-found. Scores computed once globally then mapped back onto the nested breakdown via a `(symbol, level)` lookup.
- `main()` — CLI: auth via `get_kite_session()` (from `kite_auth.py`), runs, writes CSV (`top_rows`) + HTML report (`report.py`, all ranked candidates), prints a summary table.

## `report.py`

- `generate_html_report(rows, universe_size, volatile_count) -> str` — self-contained HTML file (embedded JSON, client-side sort/filter, dark theme). `rows` is the **full** ranked list, not truncated to `top_n`. Writes both a dated file and `atrx_report_latest.html` to `CONFIG.output_dir`; returns the `_latest` path.

## `candidate_alert.py`

The candidate-scan agent, triggered every ~25min (during market hours) by GitHub Actions via `POST /api/scan-candidates?tier=<tier>` (Vercel Hobby cron can't fire that often).

- `_is_trading_window(now_ist) -> tuple[bool,str]` — weekday + 8:50am-3:00pm IST + not an NSE holiday (`db_store.is_nse_holiday`). **Fails open** on DB errors checking holidays (missing an alert on a real holiday is cheaper than silently skipping every real trading day on a transient DB error).
- `run_candidate_scan(tier) -> dict` — checks window → loads saved Kite token (sends "login needed" WhatsApp + returns early if missing) → `screener.run_screener(kite, overrides=None, universe_tier=tier)` **always with config.py defaults, never the UI's saved overrides** (this agent's parameters are meant to be fixed) → filters already-alerted-today candidates (`db_store.filter_unsent_candidates`) → sorts by ATR% desc then `abs(atrx)` asc → sends one combined WhatsApp (`whatsapp_notify.send_whatsapp`) → records sent (`db_store.record_alerts_sent`).

## `notify.py`

Best-effort email via Resend (`RESEND_API_KEY`, `RESEND_FROM_EMAIL` must be verified, `NOTIFY_EMAIL` defaults to `mjmasalawala@gmail.com`). `send_login_needed_email() -> bool`. Used by the *cost-basis* sync flow, not candidate alerting (which uses WhatsApp instead). Never raises.

## `whatsapp_notify.py`

Best-effort WhatsApp via CallMeBot's free API (no delivery SLA). `CALLMEBOT_APIKEY`, `ALERT_PHONE` (default `+919970852786`), `_MAX_TEXT_LEN=1500` (CallMeBot silently truncates/drops overly long messages, kept well under WhatsApp's own ~4096 limit).

- `send_login_needed_whatsapp() -> bool`, `send_whatsapp(text) -> bool` (truncates if needed, never raises).

## `nse_holidays_sync.py`

Fetches NSE's published holiday calendar into Neon (`nse_holidays`), meant to run once/year via `/api/sync-nse-holidays` (Vercel's own cron, since it's needed only annually — see `vercel.json`).

- `fetch_holidays(year=None) -> list[dict]` — CM (Capital Market/equity) segment only.
- `sync_year(year=None) -> dict` — fetches + persists via `db_store.replace_nse_holidays`; raises if zero holidays returned.
- **Gotcha:** nseindia.com blocks bare API requests — must hit the homepage first in the same `requests.Session` to pick up cookies, then reuse them for the API call (the standard community NSE-API workaround).

## `blob_store.py`

Persists each run's CSV to Vercel Blob (the function's filesystem doesn't survive between invocations). Requires `BLOB_READ_WRITE_TOKEN`.

- `upload_csv(pathname, content: bytes) -> str` — raw HTTP `PUT` (no official Python SDK), headers `x-api-version: 7`, `x-content-type: text/csv`, `x-add-random-suffix: 0`. Raises `RuntimeError` with the raw response body on failure.

## `db_store.py` — screener-related functions

Constants: `CONFIG_TABLE="screener_config"`, `UNIVERSE_TIERS_TABLE="screener_universes"`, `DEFAULT_TIER="large_cap"`, `NSE_HOLIDAYS_TABLE="nse_holidays"`, `SCREENER_ALERTS_SENT_TABLE="screener_alerts_sent"`, `INSTRUMENTS_TABLE="instruments"`.

- `load_config() -> dict | None`, `save_config(params: dict) -> None` (singleton row `id=1`).
- `load_universe(tier=DEFAULT_TIER) -> list[str] | None`, `list_universe_tiers() -> list[dict]` (`{tier, note, symbol_count}`), `save_universe(tier, symbols, note="") -> None`.
- `list_instruments_by_index() -> dict[str, list[str]]` — `{index_name: [ticker,...]}`, alphabetical, for deterministic universe-batch chunking.
- `replace_nse_holidays(year, rows) -> int` — wipe-and-reinsert (not upsert) since NSE revises its list.
- `is_nse_holiday(date) -> bool`.
- `filter_unsent_candidates(alert_date, candidates: list[tuple[str,float]]) -> list[...]`, `record_alerts_sent(alert_date, candidates) -> None` (idempotent, `ON CONFLICT DO NOTHING`).

All functions follow the file's shared convention (see `requirements/kite-login-infra.md` or `db_store.py`'s module docstring): raw SQL, f-string table constants, `%s` placeholders, manual tuple→dict mapping, `Json()` for JSONB, schema lives only in `scripts/*.sql`.

## `index.html` — ATRx Screener page

Sidebar (ATRx Screener / ATRx Stock / Cost Basis) → header (title, `#meta`, universe-tier `<select id="tierSelect">`, login button) → `#banner` → collapsible `#configSection` (grouped tunable fields, `CONFIG_GROUPS`, with hover tips and a recency-decay-vs-lookback warning) → results `<table>` (`BREAKDOWN_COLUMNS`: symbol/price/ATR%/ATR-percentile/level-count/qualifying-count/best-score/status) with per-symbol expandable detail rows (`LEVEL_COLUMNS`: full pivot-cluster breakdown, pass/fail styling, rejection reasons, pivot dates).

Key JS: `renderConfigForm`/`applyConfigValues`/`checkConfigWarnings`/`collectConfigOverrides` (config panel), `renderHeader`/`levelDetailHtml`/`renderRows` (results table + drill-down), `checkStatus`/`loadConfig`/`loadTiers` (init), run-button handler (`POST /api/run-screener` with `{overrides, universe_tier}`, populates `BREAKDOWN` from `data.screening_breakdown`).

## `atrx_stock.html` — ATRx Stock page

Same sidebar/header/config-panel scaffolding and CSS as index.html (duplicated, not shared). Differences: a ticker `<input>` instead of a tier selector; no `screening_breakdown` table — a single-symbol summary card + the same pivot-cluster detail table, always shown (not behind a click, since there's only one symbol); `CONFIG_GROUPS`' Volatility group omits `min_atr_percentile` (no cross-universe pre-filter for one ticker); calls `POST /api/run-screener-symbol` with `{symbol, overrides}`; own localStorage key `atrx_stock_config_collapsed` (independent of index.html's `atrx_config_collapsed`); no CSV-snapshot link (only the full-universe run archives to Blob).

## `.github/workflows/scan-candidates.yml` + `vercel.json` crons

The workflow fires on 16 explicit cron schedules (25 min apart, Mon-Fri, ~8:50am-3:05pm IST — an explicit list rather than `*/25` because 25 doesn't evenly divide 60) plus `workflow_dispatch`. For each configured universe tier (fetched from `/api/universe-tiers`), it `POST`s `/api/scan-candidates?tier=<tier>` with `Authorization: Bearer $CRON_SECRET`; one tier's failure doesn't abort the rest. This lives in GitHub Actions (not Vercel cron) because Vercel Hobby cron can only fire once/day. `vercel.json`'s own `crons` section has exactly one entry — `sync-nse-holidays`, once/year (Jan 1) — since that only needs to run annually.
