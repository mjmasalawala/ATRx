# ATRx — instructions for Claude Code

**Before exploring this codebase, read `requirements/README.md` and the relevant doc(s) it points to.** This repo has four modules, each with a concise reference doc in `requirements/`: `atrx-screener.md`, `cost-basis.md`, `kite-login-infra.md`, `performance-log.md`. Each doc lists, per source file: purpose, key function signatures, and non-obvious design decisions/gotchas. They were written specifically so a new session doesn't have to re-read the whole repo (source files, `db_store.py`, `api/index.py`'s route table, the HTML pages) just to get oriented — a prior session already paid that cost once and wrote it down.

Use the docs as your primary source of context. Only read the actual source files when:
- the docs don't cover what you need in enough detail,
- you're about to change a file and need to verify its *current* exact contents (the docs are a snapshot, not guaranteed byte-for-byte current), or
- you're debugging something the docs wouldn't explain (a runtime error, a data issue).

If you make a change substantial enough that it would make one of these docs meaningfully wrong or incomplete, update that doc as part of the same change — don't leave the next session to rediscover it the hard way.

## A few standing facts worth knowing before you start

- Single Flask app (`api/index.py`), one route (`/api/index`), dispatching on a `?r=` query param via a `_ROUTES` dict — not real per-path routing. This is because Vercel's Python runtime builds one WSGI entrypoint per project and its rewrite mechanism flattens every request path to that one function. `vercel.json`'s `rewrites` map each clean public URL to the right `?r=` value. Adding a new page or endpoint means: a handler function, a `_ROUTES` entry, and a matching `vercel.json` rewrite — see any existing route for the pattern.
- One Postgres access module, `db_store.py` — raw SQL, no ORM, manual tuple→dict mapping. Schema lives only in `scripts/*.sql`, applied manually in Neon's SQL Editor. **`db_store.py` must never create or alter tables itself** — if a change needs a new table or column, write a new numbered SQL script and ask the user to run it themselves.
- No shared frontend framework — every HTML page is a fully self-contained file (own `<style>`, own inline `<script>`). The one exception is `performance_log_widget.js`, the first shared JS asset, loaded via `<script src="...">` from multiple pages.
