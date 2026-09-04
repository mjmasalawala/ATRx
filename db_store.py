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
UNIVERSE_TIERS_TABLE = "screener_universes"
DEFAULT_TIER = "large_cap"

COST_BASIS_BASELINE_TABLE = "cost_basis_baseline"
COST_BASIS_TRADES_TABLE = "cost_basis_trades"
COST_BASIS_STATE_TABLE = "cost_basis_state"

NSE_HOLIDAYS_TABLE = "nse_holidays"
SCREENER_ALERTS_SENT_TABLE = "screener_alerts_sent"


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


def load_universe(tier: str = DEFAULT_TIER) -> list[str] | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT symbols FROM {UNIVERSE_TIERS_TABLE} WHERE tier = %s", [tier])
        row = cur.fetchone()
        return row[0] if row else None


def list_universe_tiers() -> list[dict]:
    """Returns [{tier, note, symbol_count}, ...] for every tier that has a row."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT tier, note, symbols FROM {UNIVERSE_TIERS_TABLE} ORDER BY tier")
        return [{"tier": r[0], "note": r[1], "symbol_count": len(r[2])} for r in cur.fetchall()]


def save_universe(tier: str, symbols: list[str], note: str = "") -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {UNIVERSE_TIERS_TABLE} (tier, note, symbols, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (tier) DO UPDATE SET note = EXCLUDED.note, symbols = EXCLUDED.symbols, updated_at = now()
            """,
            [tier, note, Json(symbols)],
        )
        conn.commit()


def save_baseline(rows: list[dict], force: bool = False) -> int:
    """Seeds cost_basis_baseline from a one-time Kite holdings dump. Refuses
    to overwrite a symbol that already has a baseline row unless force=True,
    since this is meant to be a one-time historical starting point, not
    something a re-run should silently clobber."""
    with get_conn() as conn, conn.cursor() as cur:
        on_conflict = (
            "DO UPDATE SET quantity = EXCLUDED.quantity, avg_price = EXCLUDED.avg_price, "
            "as_of_date = EXCLUDED.as_of_date, captured_at = now()"
            if force else "DO NOTHING"
        )
        written = 0
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO {COST_BASIS_BASELINE_TABLE} (symbol, quantity, avg_price, as_of_date)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol) {on_conflict}
                """,
                [row["symbol"], row["quantity"], row["avg_price"], row["as_of_date"]],
            )
            written += cur.rowcount
        conn.commit()
        return written


def load_baseline_row(symbol: str) -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT symbol, quantity, avg_price, as_of_date FROM {COST_BASIS_BASELINE_TABLE} WHERE symbol = %s",
            [symbol],
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"symbol": row[0], "quantity": row[1], "avg_price": row[2], "as_of_date": str(row[3])}


def upsert_trades(rows: list[dict]) -> int:
    """Inserts fills pulled from Kite's trade book. trade_id is the primary
    key, so re-running the daily sync (or the manual 'Sync now' button)
    can't double-insert the same fill -- ON CONFLICT DO NOTHING makes this
    idempotent."""
    if not rows:
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        written = 0
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO {COST_BASIS_TRADES_TABLE}
                    (trade_id, symbol, exchange, side, quantity, price, trade_time, order_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trade_id) DO NOTHING
                """,
                [
                    row["trade_id"], row["symbol"], row.get("exchange"), row["side"],
                    row["quantity"], row["price"], row["trade_time"], row.get("order_id"),
                ],
            )
            written += cur.rowcount
        conn.commit()
        return written


def load_symbol_trades(symbol: str) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_id, side, quantity, price, trade_time, order_id
            FROM {COST_BASIS_TRADES_TABLE} WHERE symbol = %s ORDER BY trade_time ASC
            """,
            [symbol],
        )
        return [
            {
                "trade_id": r[0], "side": r[1], "quantity": r[2], "price": r[3],
                "trade_time": r[4].isoformat(), "order_id": r[5],
            }
            for r in cur.fetchall()
        ]


def list_traded_symbols() -> list[str]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol FROM {COST_BASIS_BASELINE_TABLE}
            UNION
            SELECT DISTINCT symbol FROM {COST_BASIS_TRADES_TABLE}
            ORDER BY symbol
            """
        )
        return [r[0] for r in cur.fetchall()]


def upsert_cost_basis_state(rows: list[dict]) -> None:
    if not rows:
        return
    with get_conn() as conn, conn.cursor() as cur:
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO {COST_BASIS_STATE_TABLE}
                    (symbol, quantity, total_cost, avg_cost, cumulative_realized, lifetime_realized, is_free, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (symbol) DO UPDATE SET
                    quantity = EXCLUDED.quantity, total_cost = EXCLUDED.total_cost,
                    avg_cost = EXCLUDED.avg_cost, cumulative_realized = EXCLUDED.cumulative_realized,
                    lifetime_realized = EXCLUDED.lifetime_realized, is_free = EXCLUDED.is_free,
                    updated_at = now()
                """,
                [
                    row["symbol"], row["quantity"], row["total_cost"], row["avg_cost"],
                    row["cumulative_realized"], row["lifetime_realized"], row["is_free"],
                ],
            )
        conn.commit()


def list_cost_basis_summary() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, quantity, total_cost, avg_cost, cumulative_realized,
                   lifetime_realized, is_free, updated_at
            FROM {COST_BASIS_STATE_TABLE} ORDER BY symbol
            """
        )
        return [
            {
                "symbol": r[0], "quantity": r[1], "total_cost": r[2], "avg_cost": r[3],
                "cumulative_realized": r[4], "lifetime_realized": r[5], "is_free": r[6],
                "updated_at": r[7].isoformat(),
            }
            for r in cur.fetchall()
        ]


def replace_nse_holidays(year: int, rows: list[dict]) -> int:
    """Replaces every stored holiday in `year` with `rows` ([{date, description}]).
    Wipe-and-reinsert rather than upsert, since NSE occasionally revises its
    published list (e.g. added muhurat trading days) and a stale removed
    entry should disappear, not linger."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {NSE_HOLIDAYS_TABLE} WHERE EXTRACT(YEAR FROM holiday_date) = %s",
            [year],
        )
        for row in rows:
            cur.execute(
                f"INSERT INTO {NSE_HOLIDAYS_TABLE} (holiday_date, description) "
                f"VALUES (%s, %s) ON CONFLICT (holiday_date) DO UPDATE SET description = EXCLUDED.description",
                [row["date"], row.get("description", "")],
            )
        conn.commit()
        return len(rows)


def is_nse_holiday(date) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {NSE_HOLIDAYS_TABLE} WHERE holiday_date = %s", [date])
        return cur.fetchone() is not None


def filter_unsent_candidates(alert_date, candidates: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Given [(symbol, level), ...], returns only the ones not already
    recorded as sent on `alert_date`."""
    if not candidates:
        return []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT symbol, level FROM {SCREENER_ALERTS_SENT_TABLE} WHERE alert_date = %s",
            [alert_date],
        )
        already_sent = {(r[0], float(r[1])) for r in cur.fetchall()}
    return [c for c in candidates if (c[0], float(c[1])) not in already_sent]


def record_alerts_sent(alert_date, candidates: list[tuple[str, float]]) -> None:
    if not candidates:
        return
    with get_conn() as conn, conn.cursor() as cur:
        for symbol, level in candidates:
            cur.execute(
                f"""
                INSERT INTO {SCREENER_ALERTS_SENT_TABLE} (alert_date, symbol, level)
                VALUES (%s, %s, %s)
                ON CONFLICT (alert_date, symbol, level) DO NOTHING
                """,
                [alert_date, symbol, level],
            )
        conn.commit()
