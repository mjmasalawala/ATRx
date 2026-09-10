# Performance Log module

Tracks manually-logged trades (one entry + one exit per position) tied to whichever strategy produced the signal, so win-rate/P&L stats can be computed per strategy over time. Distinct from `cost_basis_trades` (a ledger of real broker fills for tax/avg-cost accounting) — this table has nothing to do with that system; it's a personal record of "I acted on this signal, here's what happened."

One row = one full position, not one fill. The same symbol can have many separate rows over time — that's intentional, since repeated correct calls by the same strategy are exactly what the win-rate stat is meant to surface.

## Schema — `scripts/010_create_performance_log_table.sql`

```sql
performance_log (
    id SERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    entry_date DATE NOT NULL,
    entry_price NUMERIC(12,2) NOT NULL CHECK (entry_price > 0),
    exit_date DATE,
    exit_price NUMERIC(12,2) CHECK (exit_price IS NULL OR exit_price > 0),
    notes TEXT,
    signal_meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((exit_date IS NULL) = (exit_price IS NULL))
)
INDEX performance_log_strategy_idx ON (strategy)
INDEX performance_log_symbol_idx ON (symbol)
```

`signal_meta` stores the *entire* level object from the moment "Log Trade" was clicked — `level`, `atr`, `atrx`, `touches`, `breaches`, `recency_weight`, `meets_criteria`, `in_range`, `is_candidate`, `rejection_reasons`, `backtest_touches`, `hit_rate_3d_pct`/`avg_fwd_ret_3d_pct`/`hit_rate_5d_pct`/`avg_fwd_ret_5d_pct`, `score`, `pivots` — **plus `signal_date`**, the screener run's `generated_at` at click time. `signal_date` is deliberately separate from `entry_date`: the latter is whatever the user manually enters (they may act on a signal a day or more later), so the gap between them is itself an analyzable quantity ("does entry lag hurt returns").

`pct_return`/`pnl` are **never stored** — always computed at read time from `entry_price`/`exit_price`/`qty`, same principle as `screener.py`'s `score` field. `db_store.list_trades()` only computes these against `exit_price` (so both are `None` while a trade is open); `performance_log.html` additionally recomputes them client-side against the live current price for open trades (see below), so "unrealized" numbers are never persisted anywhere.

## `db_store.py` functions

`PERFORMANCE_LOG_TABLE = "performance_log"`. Same conventions as the rest of the file (raw SQL, `%s` placeholders, manual tuple→dict mapping, `Json()` for the JSONB column).

- `create_trade(strategy, symbol, qty, entry_date, entry_price, notes=None, signal_meta=None) -> int` — `INSERT ... RETURNING id`.
- `close_trade(trade_id, exit_date, exit_price) -> None`.
- `delete_trade(trade_id) -> None`.
- `list_trades(strategy=None) -> list[dict]` — newest `entry_date` first; each dict includes computed `pct_return`/`pnl` (both `None` while open).
- `get_trade_stats(strategy=None) -> dict` — built from `list_trades()` in Python (data volume is tiny, no SQL aggregation needed). Returns `{strategy_name: {trades, wins, losses, win_rate_pct, avg_return_pct, total_pnl, open_count}, ...}` — `trades`/`wins`/`losses`/rates only count **closed** trades; `open_count` is separate.

## `api/index.py` routes

- `performance_log_page()` → `/performance-log`, serves `performance_log.html`.
- `performance_log_widget_js()` → `/performance-log-widget.js`, serves `performance_log_widget.js` with `mimetype="application/javascript"` (via `_serve_file`'s `mimetype` param — this is also what `home`/`cost_basis_page`/`atrx_stock_page` now call, renamed from the old HTML-only `_serve_html`).
- `trades_endpoint()` → `/api/trades` — `GET` lists (`?strategy=` optional filter); `POST` creates one (JSON body: `strategy`, `symbol`, `qty`, `entry_date`, `entry_price`, optional `notes`/`signal_meta`; 400 if any required field is missing).
- `trades_close_endpoint()` → `/api/trades-close`, `POST` (`trade_id`, `exit_date`, `exit_price`).
- `trades_delete_endpoint()` → `/api/trades-delete`, `POST` (`trade_id`).
- `trade_stats_endpoint()` → `/api/trade-stats`, `GET` (`?strategy=` optional).
- `trade_quotes_endpoint()` → `/api/trade-quotes`, `GET` (`?symbols=` comma-separated). **Does** require a Kite login (401 if not logged in) since it calls `kite.ltp()` for live prices — unlike every other performance-log route. Returns `{symbol: last_price}`, silently omitting any symbol Kite didn't return a quote for. Used only for open positions' current price / unrealized P&L; closed trades never call this.

No Kite-login gate on the rest of these — consistent with `/api/config`/`/api/universe-tiers` (this app has no separate identity/auth layer beyond the Kite session used for the screener's own data fetch).

## `performance_log_widget.js` — the reusable "Log Trade" component

The app's first shared JS asset (every other page is fully self-contained). Injects its own scoped `plw-` styles with hardcoded colors (not the host page's CSS variables — index.html/atrx_stock.html and cost_basis.html already disagree on `--positive`/`--negative` vs `--green`/`--red` naming *and* exact shades) and builds one modal, appended to `<body>` on first use.

Public API:
```js
window.PerformanceLog = {
  init({ strategy }),                       // sets the default strategy slug for this page
  open({ symbol, price, meta, onSuccess }),  // opens the modal; {} = blank manual entry
};
```

- When `symbol` is passed (triggered from a specific candidate row), both the **Strategy** and **Symbol** fields are pre-filled and locked — the caller already knows definitively which strategy/signal this is, so editing either would misattribute the trade.
- When opened blank (`open({})`, e.g. a page's own "+ Add Trade" button), Strategy defaults to `init()`'s value but stays editable, since a manual entry isn't necessarily for the page's own strategy.
- `onSuccess` is an optional callback fired right after a successful `POST /api/trades` (before the modal auto-closes) — `performance_log.html` uses this to refresh its tables; pages that only trigger logging (index.html, atrx_stock.html) don't need it.
- **Ordering gotcha:** `PerformanceLog.init(...)` must not be the last statement before something else that depends on it running — if the external script fails to load (e.g. this exact scenario when testing locally without Vercel's rewrite layer, where the clean `/performance-log-widget.js` path 404s), a `ReferenceError` there would silently abort every statement after it in the same inline `<script>` block. All three pages call `PerformanceLog.init()` *after* their own independent init calls (`loadTiers()`/`checkStatus()`/`refresh()`), not before, for exactly this reason.

## Wiring into index.html / atrx_stock.html

Both load the widget via `<script src="/performance-log-widget.js"></script>` and call `PerformanceLog.init({ strategy: "atrx_screener" })` — same slug in both, since it's the same underlying algorithm regardless of which page or universe tier triggered it.

`LEVEL_COLUMNS` gained a trailing `{ key: "action", label: "" }` entry; the per-level cell builder in `levelDetailHtml()` renders a "Log Trade" button only when `lvl.is_candidate` is true, tagged with `data-symbol`/`data-level-idx` attributes. A delegated click listener (on `#tbody` in index.html, `#resultWrap` in atrx_stock.html — attached once, not re-bound on every render) looks up the matching level from `BREAKDOWN`/`CURRENT_BREAKDOWN` and calls:
```js
PerformanceLog.open({ symbol, price: <row's current_price>, meta: { ...lvl, signal_date: LAST_GENERATED_AT } });
```
`LAST_GENERATED_AT` is set from `data.generated_at` (already present in every `/api/run-screener`/`/api/run-screener-symbol` response) whenever a run completes.

## `performance_log.html` — the standalone page

Same self-contained shell/CSS pattern as the other three pages. "+ Add Trade" button opens a blank modal (`PerformanceLog.open({ onSuccess: refresh })`). Two tables, both re-fetched via `refresh()`: a per-strategy stats panel (`GET /api/trade-stats`) and the full trade list (`GET /api/trades`) — open positions get an inline exit-date/exit-price form + "Close" button (`POST /api/trades-close`) instead of a static exit column; every row has a "Delete" button (`POST /api/trades-delete`). Both tables re-fetch on any close/delete/add.

Trades table columns: Symbol | Strategy | Qty | Entry (date + price) | Exit (date + price, or the close form) | Current price | Days open | % Return | P&L | Delete.

- **Dates** render via `fmtDate()` as `DD-MMM-YY` (e.g. `25-Feb-25`) everywhere a date is shown — entry, exit. The API still returns plain ISO `YYYY-MM-DD`; this is purely a display transform.
- **Current price**: `loadTrades()` collects the distinct symbols of every still-open trade and fetches them in one call to `GET /api/trade-quotes`; closed trades never hit this endpoint (they already have a final `exit_price`). Shows `–` if not logged in / the quote fetch fails / Kite didn't return a price for that symbol — never a stale or fabricated number.
- **% Return / P&L**: for a closed trade, these are the server's `pct_return`/`pnl` (against `exit_price`). For an open trade, `loadTrades()` recomputes both client-side against the fetched current price (`(currentPrice - entry_price) / entry_price`, `(currentPrice - entry_price) * qty`) — i.e. unrealized P&L — and shows `–` if no current price is available yet.
- **Days open**: `daysBetween(entry_date, exit_date)` for a closed trade; `daysBetween(entry_date, today)` for an open one (using the browser's local date, not `signal_date`). Shown as a plain integer day count.
