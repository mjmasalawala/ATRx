# Cost Basis module

Tracks real broker fills (Zerodha trades, not screener signals) to compute a running average cost and realized P&L per symbol, using a non-standard "cost-reducing" model rather than plain weighted-average-cost.

## `cost_basis.py`

Pure calculation engine — no I/O, callers load trades and pass them in. Realized profit from every sell is netted directly into the cost basis of remaining shares (rather than leaving avg cost flat and tracking realized P&L separately). Once `total_cost` drops to ≤0, the position is "FREE" — historical profit has already repaid the full investment.

An earlier version accepted a "baseline" (a Kite holdings snapshot used before full trade history was obtainable). Dropped once full history became available — replaying baseline + trades it already summarizes would double-count. See `cost_basis_baseline` table note below (schema-only now, unused).

- `LedgerEntry` (dataclass): `trade_id`, `trade_time`, `side`, `quantity`, `price`, `realized`, `quantity_after`, `total_cost_after`, `avg_cost_after`. One row per replayed trade.
- `CostBasisState` (dataclass): `symbol`, `quantity=0.0`, `total_cost=0.0`, `cumulative_realized=0.0`, `lifetime_realized=0.0`. `.avg_cost` property = `total_cost/quantity` or `None`. `.is_free` property = `quantity > 0 and total_cost <= 0`.
- `_QTY_EPSILON = 1e-6` — float-rounding tolerance for "effectively zero."
- `replay(symbol: str, trades: list[dict]) -> dict` — `trades` pre-sorted chronologically, each `{side, quantity, price, trade_id?, trade_time?}`. Returns `{symbol, quantity, total_cost, avg_cost, cumulative_realized, lifetime_realized, is_free, ledger: [...]}`.
  - **Gotcha (lot exit):** when quantity settles at ~zero, state resets fresh on the next buy. `lifetime_realized` accumulates forever across lots; `cumulative_realized` (backs the "FREE" badge) resets to 0 at that point.
  - **Gotcha (negative quantity → raises, never clamps):** a sell taking quantity below `-_QTY_EPSILON` raises `ValueError` — deliberately not treated as "open a short" (this model is long-only). Means either real short activity (unsupported) or incomplete/out-of-order history (a sell landing before its matching buy). An earlier version silently clamped to zero, producing plausible-but-wrong leftover quantity — judged unsafe.
  - Also raises `ValueError` for any `side` other than BUY/SELL.

## `cost_basis_state_sync.py`

- `recompute_all() -> dict` — recomputes the **full** symbol universe (every symbol with any uploaded trade), not just symbols touched by the latest upload. Uses `db_store.load_all_trades()` (one bulk query) rather than per-symbol connections (dozens of sequential Neon handshakes could plausibly blow the 60s Vercel timeout). Per-symbol failures are caught individually and reported in `failed` — one bad symbol (e.g. an oversell) must not block every other symbol's state from being written. Calls `db_store.upsert_cost_basis_state(state_rows)`. Returns `{"updated": [...], "failed": [{"symbol", "error"}, ...]}`.

## `trade_csv_import.py`

Zerodha Console's `/trades` API only ever returns the *current day's* fills (no date range, no history endpoint) — a periodic pull would silently lose any skipped day. So the source of truth is a manual CSV export from Kite Console (Reports → Tradebook), uploaded here.

- `_COLUMN_ALIASES` — canonical field → acceptable header aliases (case-insensitive, spaces→underscores via `_normalize_header`): `symbol`↔`tradingsymbol`/`trading symbol`, `side`↔`trade_type`/`transaction_type`, `quantity`↔`qty`, `price`↔`trade_price`/`average_price`, `trade_time`↔`order_execution_time`/`exchange_timestamp`/`fill_timestamp`, etc. `_REQUIRED_COLUMNS = ("symbol","side","quantity","price","trade_id","exchange")`.
- `parse_trades_csv(file_content: str) -> tuple[list[dict], list[dict]]` (`rows, skipped`) — raises `ValueError` if no header row, or if required columns are missing (names the actual header found, rather than silently mis-mapping). Per-row validation failures are **skipped**, not fatal, collected as `{"row": i, "reason": ...}` (rows numbered from 2, since 1 is the header).
- `import_trades_csv(file_content: str) -> dict` — `parse_trades_csv` → `db_store.upsert_trades` → `cost_basis_state_sync.recompute_all()` → `db_store.set_last_trades_sync(now)`. Returns `{parsed, inserted, symbols_updated, skipped_rows, state_updated, state_failed, synced_at}`.

## `db_store.py` — cost-basis functions

Constants: `COST_BASIS_TRADES_TABLE`, `COST_BASIS_STATE_TABLE`, `COST_BASIS_SYNC_STATE_TABLE`.

- `upsert_trades(rows) -> int` — `INSERT ... ON CONFLICT (trade_id, exchange) DO NOTHING`, returns count actually written. **Gotcha:** PK is `(trade_id, exchange)`, not `trade_id` alone — Zerodha trade IDs are only unique *within* an exchange, so the same symbol on NSE+BSE could share a trade_id; keying on trade_id alone let a real trade silently vanish (fixed in `009_fix_trades_composite_key.sql`).
- `_TRADE_ORDER_BY = "trade_time ASC, (side = 'SELL') ASC, trade_id ASC"` — same-timestamp ties (common when a CSV row only has a bare date) need a deterministic tiebreak, else a same-day SELL could sort before its matching BUY and `replay()` sees a false oversell. BUY-before-SELL doesn't affect same-day P&L under average-cost (unlike FIFO); `trade_id` is the final tiebreak.
- `load_symbol_trades(symbol) -> list[dict]`, `load_all_trades() -> dict[str, list[dict]]` (bulk, avoids per-symbol connections).
- `upsert_cost_basis_state(rows) -> None` — upsert on `symbol`.
- `get_last_trades_sync() -> str | None`, `set_last_trades_sync(when) -> None` — singleton row (`id=1`).
- `list_cost_basis_summary() -> list[dict]` — what the Cost Basis page's summary table reads directly.

## `api/index.py` — cost-basis routes

- `cost_basis_page()` → `/cost-basis`, serves `cost_basis.html`.
- `cost_basis_summary_endpoint()` → `/api/cost-basis-summary`, GET, `db_store.list_cost_basis_summary()` as JSON.
- `cost_basis_ledger_endpoint()` → `/api/cost-basis-ledger?symbol=`, GET, requires non-empty `symbol`; loads trades + `cost_basis.replay`; on replay failure (e.g. oversell) returns `{"error", "raw_trades": trades}` (500) so the raw data stays diagnosable.
- `cost_basis_upload_trades_endpoint()` → `/api/cost-basis-upload-trades`, POST multipart `file`; decodes `utf-8-sig`; `ValueError` → 400, other exceptions → 500 with traceback.
- `cost_basis_sync_status_endpoint()` → `/api/cost-basis-sync-status`, GET, `{"last_synced_at": ...}`.

## `cost_basis.html` (477 lines)

Sidebar → header (title + Upload CSV button + last-synced text) → instructional text → `#sync-status` banner → filter row (ticker text filter, hide-zero-qty checkbox, both persisted to `localStorage`) → sortable summary `<table>` (Ticker/Qty/Avg cost/Total cost/Realized-current-lot/Lifetime-realized, sort persisted to `localStorage`) → click a row to expand an inline per-trade ledger sub-row (lazily fetched, cached in `ledgerCache`; on ledger error shows the raw-trades fallback table). Key JS: `fmtNum`, `pnlClass`, `loadSummary`, `setSort`, `render`, `renderLedger`, `toggleLedger`, file-upload change handler.

## Current schema (post `009_fix_trades_composite_key.sql`)

```sql
cost_basis_trades (
    trade_id TEXT NOT NULL, symbol TEXT NOT NULL, exchange TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity NUMERIC NOT NULL, price NUMERIC NOT NULL, trade_time TIMESTAMPTZ NOT NULL,
    order_id TEXT, synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_id, exchange)  -- composite since 009 (was trade_id alone)
)
INDEX cost_basis_trades_symbol_idx ON (symbol, trade_time)

cost_basis_state (
    symbol TEXT PRIMARY KEY, quantity NUMERIC NOT NULL, total_cost NUMERIC NOT NULL,
    avg_cost NUMERIC, cumulative_realized NUMERIC NOT NULL DEFAULT 0,
    lifetime_realized NUMERIC NOT NULL DEFAULT 0, is_free BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)

cost_basis_sync_state (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1), last_synced_at TIMESTAMPTZ
)  -- singleton; tracks last CSV upload, replaces a removed daily Kite-API pull
```

**`cost_basis_baseline`** (from migration 005) is **deprecated/orphaned** — schema exists, but grep confirms it's referenced nowhere in current Python code. Not dropped by any migration; likely still an empty/stale table in the live DB.
