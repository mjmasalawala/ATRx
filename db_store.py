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

COST_BASIS_TRADES_TABLE = "cost_basis_trades"
COST_BASIS_STATE_TABLE = "cost_basis_state"
COST_BASIS_SYNC_STATE_TABLE = "cost_basis_sync_state"

NSE_HOLIDAYS_TABLE = "nse_holidays"
SCREENER_ALERTS_SENT_TABLE = "screener_alerts_sent"

INSTRUMENTS_TABLE = "instruments"

PERFORMANCE_LOG_TABLE = "performance_log"


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


def list_instruments_by_index() -> dict[str, list[str]]:
    """Returns {index_name: [ticker, ...]}, tickers ordered alphabetically
    for deterministic chunking by callers that split an index into
    fixed-size universe batches."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT index_name, ticker FROM {INSTRUMENTS_TABLE} ORDER BY index_name, ticker")
        by_index: dict[str, list[str]] = {}
        for index_name, ticker in cur.fetchall():
            by_index.setdefault(index_name, []).append(ticker)
        return by_index


def upsert_trades(rows: list[dict]) -> int:
    """Inserts fills from an uploaded tradebook CSV. Primary key is
    (trade_id, exchange), NOT trade_id alone -- Zerodha's trade IDs are
    only unique WITHIN an exchange, so a symbol traded on both NSE and BSE
    can have genuinely different trades sharing the same trade_id. Keying
    on trade_id alone (an earlier version did) let a real trade on one
    exchange silently vanish via ON CONFLICT DO NOTHING because its ID
    collided with an unrelated trade already stored from the other
    exchange -- see scripts/009_fix_trades_composite_key.sql."""
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
                ON CONFLICT (trade_id, exchange) DO NOTHING
                """,
                [
                    row["trade_id"], row["symbol"], row["exchange"], row["side"],
                    row["quantity"], row["price"], row["trade_time"], row.get("order_id"),
                ],
            )
            written += cur.rowcount
        conn.commit()
        return written



# Trades sharing an exact trade_time (common when a CSV row only has a bare
# trade_date, no order_execution_time, so several same-day fills all land
# on the same midnight timestamp) need a deterministic tiebreak -- without
# one, Postgres returns ties in an arbitrary order, and a same-day SELL
# landing before its matching BUY makes cost_basis.replay() see an oversell
# against zero held shares. Processing BUYs before SELLs within a tie
# doesn't change same-day P&L under the average-cost method (unlike FIFO,
# order within a day doesn't matter for the blended average), so it's a
# safe way to break the tie; trade_id is the final tiebreak for same-side
# same-timestamp ties (e.g. a single order filled in several tranches).
_TRADE_ORDER_BY = "trade_time ASC, (side = 'SELL') ASC, trade_id ASC"


def load_symbol_trades(symbol: str) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_id, side, quantity, price, trade_time, order_id
            FROM {COST_BASIS_TRADES_TABLE} WHERE symbol = %s ORDER BY {_TRADE_ORDER_BY}
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


def load_all_trades() -> dict[str, list[dict]]:
    """Bulk version of load_symbol_trades -- one query for every symbol's
    trades instead of one connection per symbol."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, trade_id, side, quantity, price, trade_time, order_id
            FROM {COST_BASIS_TRADES_TABLE} ORDER BY symbol, {_TRADE_ORDER_BY}
            """
        )
        by_symbol: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            by_symbol.setdefault(r[0], []).append({
                "trade_id": r[1], "side": r[2], "quantity": r[3], "price": r[4],
                "trade_time": r[5].isoformat(), "order_id": r[6],
            })
        return by_symbol




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


def get_last_trades_sync() -> str | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT last_synced_at FROM {COST_BASIS_SYNC_STATE_TABLE} WHERE id = 1")
        row = cur.fetchone()
        return row[0].isoformat() if row and row[0] else None


def set_last_trades_sync(when) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {COST_BASIS_SYNC_STATE_TABLE} (id, last_synced_at) VALUES (1, %s)
            ON CONFLICT (id) DO UPDATE SET last_synced_at = EXCLUDED.last_synced_at
            """,
            [when],
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


def create_trade(strategy: str, symbol: str, qty: int, entry_date, entry_price,
                  notes: str | None = None, signal_meta: dict | None = None) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {PERFORMANCE_LOG_TABLE}
                (strategy, symbol, qty, entry_date, entry_price, notes, signal_meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [strategy, symbol, qty, entry_date, entry_price, notes,
             Json(signal_meta) if signal_meta is not None else None],
        )
        trade_id = cur.fetchone()[0]
        conn.commit()
        return trade_id


def close_trade(trade_id: int, exit_date, exit_price) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {PERFORMANCE_LOG_TABLE}
            SET exit_date = %s, exit_price = %s, updated_at = now()
            WHERE id = %s
            """,
            [exit_date, exit_price, trade_id],
        )
        conn.commit()


def delete_trade(trade_id: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {PERFORMANCE_LOG_TABLE} WHERE id = %s", [trade_id])
        conn.commit()


def list_trades(strategy: str | None = None) -> list[dict]:
    """Returns every logged trade, newest entry_date first. Each dict
    includes pct_return/pnl computed here (never stored) when the trade
    has been closed -- same principle as screener.py's score: always
    derived from source values, not persisted redundantly."""
    query = f"""
        SELECT id, strategy, symbol, qty, entry_date, entry_price,
               exit_date, exit_price, notes, signal_meta, created_at, updated_at
        FROM {PERFORMANCE_LOG_TABLE}
    """
    params = []
    if strategy:
        query += " WHERE strategy = %s"
        params.append(strategy)
    query += " ORDER BY entry_date DESC, id DESC"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    trades = []
    for r in rows:
        (trade_id, strat, symbol, qty, entry_date, entry_price,
         exit_date, exit_price, notes, signal_meta, created_at, updated_at) = r
        entry_price = float(entry_price)
        pct_return = pnl = None
        if exit_price is not None:
            exit_price = float(exit_price)
            pct_return = round((exit_price - entry_price) / entry_price * 100, 2)
            pnl = round((exit_price - entry_price) * qty, 2)
        trades.append({
            "id": trade_id, "strategy": strat, "symbol": symbol, "qty": qty,
            "entry_date": entry_date.isoformat(), "entry_price": entry_price,
            "exit_date": exit_date.isoformat() if exit_date else None,
            "exit_price": exit_price, "notes": notes, "signal_meta": signal_meta,
            "pct_return": pct_return, "pnl": pnl,
            "created_at": created_at.isoformat(), "updated_at": updated_at.isoformat(),
        })
    return trades


def get_trade_stats(strategy: str | None = None) -> dict:
    """Built from list_trades() in Python, not SQL aggregation -- the
    data volume here is tiny (a personal trade log, not a fills ledger).
    Returns {strategy_name: {trades, wins, losses, win_rate_pct,
    avg_return_pct, total_pnl, open_count}, ...}."""
    trades = list_trades(strategy)
    by_strategy: dict[str, list[dict]] = {}
    for t in trades:
        by_strategy.setdefault(t["strategy"], []).append(t)

    stats = {}
    for strat, strat_trades in by_strategy.items():
        closed = [t for t in strat_trades if t["exit_price"] is not None]
        open_count = len(strat_trades) - len(closed)
        wins = [t for t in closed if t["pct_return"] > 0]
        losses = [t for t in closed if t["pct_return"] <= 0]
        stats[strat] = {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "avg_return_pct": round(sum(t["pct_return"] for t in closed) / len(closed), 2) if closed else None,
            "total_pnl": round(sum(t["pnl"] for t in closed), 2) if closed else 0.0,
            "open_count": open_count,
        }
    return stats
