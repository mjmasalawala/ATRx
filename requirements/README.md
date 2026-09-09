# Requirements / architecture reference

Concise per-module docs so a future session doesn't have to re-read the whole repo to get context. Each doc lists purpose, key function signatures, and non-obvious gotchas per file.

- [atrx-screener.md](atrx-screener.md) — the core support-level bounce strategy: `config.py`/`indicators.py`/`levels.py`/`backtest.py`/`screener.py`/`report.py`, the WhatsApp candidate-alert cron (`candidate_alert.py`, `notify.py`, `whatsapp_notify.py`, `nse_holidays_sync.py`), `blob_store.py`, and the `index.html`/`atrx_stock.html` frontend pages.
- [cost-basis.md](cost-basis.md) — real broker-fill tracking (Zerodha tradebook CSV import) and the cost-reducing avg-cost/realized-P&L replay model: `cost_basis.py`, `cost_basis_state_sync.py`, `trade_csv_import.py`, `cost_basis.html`.
- [kite-login-infra.md](kite-login-infra.md) — the web OAuth-redirect login flow (`kite_web_auth.py`) vs. the CLI-only interactive flow (`kite_auth.py`), and the Upstash-Redis token bridge between them (`token_store.py`).
- [performance-log.md](performance-log.md) — the trade-tracking feature (log entry/exit trades per strategy, win-rate/P&L stats), including the reusable `performance_log_widget.js` component used from every strategy page.

All four modules share one Postgres access layer, `db_store.py` (raw SQL, no ORM, schema lives only in `scripts/*.sql` and is applied manually in Neon's SQL Editor — the app code never creates or alters tables), and one Flask app, `api/index.py`, which dispatches every route through a single `/api/index?r=<name>` endpoint (Vercel's Python runtime builds one WSGI entrypoint per project and its rewrite mechanism flattens every request path to that function, so `vercel.json`'s `rewrites` map each clean public URL to a `?r=` value looked up in `_ROUTES`).
