# Kite Login Infra

Two separate auth paths exist for two separate contexts — a web OAuth-redirect flow for the deployed app, and an interactive-terminal flow for local/CLI use. They don't share code.

## `kite_web_auth.py` — web deployment

The redirect URL a Kite Connect app sends the user back to after login is configured **once in the Kite developer console** (not passed per-request) — must be set there to `https://<your-vercel-domain>/api/callback`.

- `build_login_url() -> str` — raises `RuntimeError` if `CONFIG.api_key` unset; else `KiteConnect(api_key=...).login_url()`.
- `exchange_request_token(request_token: str) -> str` — raises `RuntimeError` if key/secret unset; calls `kite.generate_session(...)`, wraps `KiteException` into `RuntimeError(f"Login failed: {e}")`; returns the access token.
- `get_kite_session_from_token(access_token: str) -> KiteConnect` — builds a client and calls `set_access_token` (no validation here).

## `kite_auth.py` — CLI only, not part of the web app

Confirmed via grep: only imported by `screener.py`'s standalone CLI entrypoint (`get_kite_session`), never by anything under `api/`. Its own docstring says it's duplicated from a sibling project "so this screener works as a fully standalone project" — an interactive-terminal-login fallback for running the screener locally.

- `get_kite_session() -> KiteConnect` — tries a saved token (`_read_saved_token`) from `CONFIG.access_token_file` (a fixed path, `BASE_DIR/state/access_token.txt`, **not** an env var); if valid (`_token_is_valid`, checks `kite.profile()`), reuses it; else falls to `_interactive_login`, which prints the login URL, prompts via `input()` for the pasted-back `request_token`, exchanges it, and saves the token to disk.

## `token_store.py` — the bridge between the two contexts

Vercel functions are stateless/ephemeral — the access token obtained via the web OAuth flow can't live in a local variable or file, so it's stored in Upstash Redis between the callback and the next screener run.

Supports **both** naming conventions, whichever pair is present: the raw "Upstash for Redis" marketplace integration (`UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN`) or Vercel's own KV product, also Upstash-backed but named differently (`KV_REST_API_URL`/`KV_REST_API_TOKEN`).

- `TOKEN_KEY = "atrx:kite_access_token"`, `TOKEN_TTL_SECONDS = 20*60*60` (20h — Kite tokens expire ~6am IST next day, this leaves margin without a stale token surviving into the next session).
- `save_access_token(token) -> None`, `load_access_token() -> str | None` — thin wrappers over `_command(*args)`, which POSTs a Redis command array to the REST URL with bearer auth; raises `RuntimeError` if neither env var pair is configured.

## `api/index.py` — login/callback/status handlers

- `login()` → `/api/login` — calls `build_login_url()`; `RuntimeError` → plain-text 500; else redirects to the Kite login URL.
- `callback()` → `/api/callback` (this exact path is what's registered in the Kite console) — reads `status`/`request_token` query params.
  - `status != "success"` or missing `request_token` → redirect to **`/?login=failed`**.
  - Otherwise: `exchange_request_token` → `token_store.save_access_token`. On `RuntimeError` → redirect to **`/?login=failed&reason=<url-quoted message>`** (marked in a comment as temporary/diagnostic — revert to a bare `/?login=failed` once the flow's fully confirmed). On success → redirect to **`/?login=success`**.
- `status_endpoint()` → `/api/status` — `token_store.load_access_token()` (catches `RuntimeError` as "no token"), returns `{"logged_in": bool(token)}`. Only checks whether *a* token is stored, does not itself validate it against Kite's API.

All three are exposed through the single `/api/index?r=...` dispatcher (see the ATRx doc / `db_store.py`'s module docstring for why); none branch on HTTP method.

## Environment variables

| Var | Used by | Purpose |
|---|---|---|
| `KITE_API_KEY` | `config.py` → `kite_web_auth.py`, `kite_auth.py` | Kite Connect app API key |
| `KITE_API_SECRET` | `config.py` → `kite_web_auth.py`, `kite_auth.py` | Kite Connect app API secret, needed to exchange request_token → access_token |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | `token_store.py` | Raw Upstash Redis REST integration |
| `KV_REST_API_URL` / `KV_REST_API_TOKEN` | `token_store.py` | Same, under Vercel KV's naming (fallback if the raw pair isn't set) |
| `CRON_SECRET` | `api/index.py` | Bearer-auth gate on `scan-candidates`/`sync-nse-holidays` (unrelated to Kite login, defined in the same file) |
| `DATABASE_URL` / `POSTGRES_URL` / `DATABASE_URL_UNPOOLED` / `POSTGRES_PRISMA_URL` | `db_store.py` | Neon connection string (first one present wins) |
