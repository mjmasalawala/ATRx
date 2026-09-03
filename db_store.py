"""
Neon Postgres storage for the screener's config and universe. Moves both
off local files (config.py's dataclass defaults, universe.json) so they're
readable AND writable from the stateless web deployment, and so the
parameters used on one run are still there the next time the user logs in
-- not just re-derived from whatever's committed to the repo.

Connects with whichever of these env vars is present (naming varies by
how the Neon integration was added to the Vercel project):
  DATABASE_URL, POSTGRES_URL, DATABASE_URL_UNPOOLED, POSTGRES_PRISMA_URL

Schema lives in scripts/*.sql, run manually in Neon's SQL Editor -- this
module only ever reads/writes rows, it never creates or alters tables.
"""

import os

import psycopg
from psycopg.types.json import Json

_ENV_VAR_CANDIDATES = (
    "DATABASE_URL", "POSTGRES_URL", "DATABASE_URL_UNPOOLED", "POSTGRES_PRISMA_URL",
)

CONFIG_TABLE = "screener_config"
UNIVERSE_TABLE = "screener_universe"


def _connection_string() -> str:
    for name in _ENV_VAR_CANDIDATES:
        val = os.getenv(name)
        if val:
            return val
    raise RuntimeError(
        "No Postgres connection string found (checked " + ", ".join(_ENV_VAR_CANDIDATES) + ")."
    )


def get_conn():
    return psycopg.connect(_connection_string())


def load_config() -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT params FROM {CONFIG_TABLE} WHERE id = 1")
        row = cur.fetchone()
        return row[0] if row else None


def save_config(params: dict) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {CONFIG_TABLE} (id, params, updated_at)
            VALUES (1, %s, now())
            ON CONFLICT (id) DO UPDATE SET params = EXCLUDED.params, updated_at = now()
            """,
            [Json(params)],
        )
        conn.commit()


def load_universe() -> list[str] | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT symbols FROM {UNIVERSE_TABLE} WHERE id = 1")
        row = cur.fetchone()
        return row[0] if row else None


def save_universe(symbols: list[str], note: str = "") -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {UNIVERSE_TABLE} (id, note, symbols, updated_at)
            VALUES (1, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET note = EXCLUDED.note, symbols = EXCLUDED.symbols, updated_at = now()
            """,
            [note, Json(symbols)],
        )
        conn.commit()
