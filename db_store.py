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

HV_ATR_SNAPSHOT_TABLE = "hv_atr_snapshot"
HV_SETTINGS_TABLE = "hv_settings"
HV_DAY_TABLE = "hv_day"
HV_FOCUS_TABLE = "hv_focus"
HV_POSITIONS_TABLE = "hv_positions"
HV_FILLS_TABLE = "hv_fills"

BREAKOUT_CONFIG_TABLE = "breakout_config"
BREAKOUT_SIGNAL_EVENTS_TABLE = "breakout_signal_events"
_BREAKOUT_HORIZONS = (3, 5, 10, 20)  # must match breakout_config.BreakoutConfig.forward_horizons


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



# Trades are grouped by calendar day first, with every BUY on a day
# replayed before that day's SELLs, regardless of intraday order --
# without this, a same-day SELL whose order_execution_time happens to be
# earlier than that day's BUY (a real pattern in Zerodha's export: e.g. an
# existing lot sold in the morning and a fresh lot bought in the afternoon
# gets logged with the sell's timestamp first) makes cost_basis.replay()
# see an oversell against zero held shares, even though the day's net
# activity was never actually short. Processing BUYs before SELLs within a
# day doesn't change that day's P&L under the average-cost method (unlike
# FIFO, order within a day doesn't matter for the blended average), so
# reordering within a day is safe -- reordering *across* days would not be
# (a later day's BUY can't cover an earlier day's SELL). trade_time is the
# next tiebreak (for same-side same-day trades, actual execution order is
# still meaningful), trade_id the final one (e.g. a single order filled in
# several tranches at the same timestamp).
_TRADE_ORDER_BY = "trade_time::date ASC, (side = 'SELL') ASC, trade_time ASC, trade_id ASC"


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


def load_breakout_config() -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT params FROM {BREAKOUT_CONFIG_TABLE} WHERE id = 1")
        row = cur.fetchone()
        return row[0] if row else None


def save_breakout_config(params: dict) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {BREAKOUT_CONFIG_TABLE} (id, params, updated_at)
            VALUES (1, %s, now())
            ON CONFLICT (id) DO UPDATE SET params = EXCLUDED.params, updated_at = now()
            """,
            [Json(params)],
        )
        conn.commit()


def upsert_signal_events(rows: list[dict]) -> int:
    """
    Batch upsert of breakout signal events, keyed on (symbol, signal_date,
    signal_config_hash). Each `row` dict needs: symbol, signal_date,
    signal_config_hash, universe_tier, entry_price, window_gain_pct,
    up_periods, n_blocks, block_values (list), filter_reasons (list), and
    fwd_ret_<h>/control_ret_<h> for each horizon in _BREAKOUT_HORIZONS
    (any of these may be None if that horizon hasn't elapsed yet).

    A full overwrite on conflict (not a null-coalescing merge) is correct
    here, not just simpler: historical prices don't change between runs,
    so re-computing the same (symbol, date, hash) later only ever adds
    previously-unavailable forward-return values, never contradicts one
    already stored.
    """
    if not rows:
        return 0

    horizon_cols = []
    for h in _BREAKOUT_HORIZONS:
        horizon_cols += [f"fwd_ret_{h}d", f"control_ret_{h}d"]

    cols = (
        "symbol", "signal_date", "signal_config_hash", "universe_tier",
        "entry_price", "window_gain_pct", "up_periods", "n_blocks",
        "block_values", "filter_reasons", *horizon_cols,
    )
    placeholders = ", ".join(["%s"] * len(cols))
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("symbol", "signal_date", "signal_config_hash"))

    with get_conn() as conn, conn.cursor() as cur:
        for row in rows:
            values = [
                row["symbol"], row["signal_date"], row["signal_config_hash"], row["universe_tier"],
                row["entry_price"], row.get("window_gain_pct"), row["up_periods"], row["n_blocks"],
                Json(row.get("block_values") or []), Json(row.get("filter_reasons") or []),
            ]
            for h in _BREAKOUT_HORIZONS:
                values.append(row.get(f"fwd_ret_{h}d"))
                values.append(row.get(f"control_ret_{h}d"))
            cur.execute(
                f"""
                INSERT INTO {BREAKOUT_SIGNAL_EVENTS_TABLE} ({", ".join(cols)}, updated_at)
                VALUES ({placeholders}, now())
                ON CONFLICT (symbol, signal_date, signal_config_hash)
                DO UPDATE SET {update_clause}, updated_at = now()
                """,
                values,
            )
        conn.commit()
    return len(rows)


def load_signal_events(signal_config_hash: str, universe_tier: str | None = None) -> list[dict]:
    """All persisted events for a given signal definition (across however
    many runs/tiers contributed to it), newest signal_date first."""
    horizon_cols = []
    for h in _BREAKOUT_HORIZONS:
        horizon_cols += [f"fwd_ret_{h}d", f"control_ret_{h}d"]
    cols = (
        "symbol", "signal_date", "universe_tier", "entry_price", "window_gain_pct",
        "up_periods", "n_blocks", "block_values", "filter_reasons", *horizon_cols,
    )
    where = "signal_config_hash = %s"
    params = [signal_config_hash]
    if universe_tier:
        where += " AND universe_tier = %s"
        params.append(universe_tier)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM {BREAKOUT_SIGNAL_EVENTS_TABLE} "
            f"WHERE {where} ORDER BY signal_date DESC",
            params,
        )
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# High Volatility page (scripts/012_create_high_volatility_tables.sql)
# ---------------------------------------------------------------------------

def upsert_atr_snapshot(rows: list[dict], tier: str, run_date) -> int:
    """Overwrites the latest-ATR% row for each symbol (one row per symbol,
    ever -- this table never grows). rows: [{symbol, atr_pct, atr_percentile_rank,
    price, atr_period}]. Cached intraday_stats are deliberately left alone;
    they're keyed by their own date and refreshed when stale."""
    if not rows:
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                f"""
                INSERT INTO {HV_ATR_SNAPSHOT_TABLE}
                    (symbol, tier, atr_pct, atr_percentile_rank, price, atr_period, run_date, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (symbol) DO UPDATE SET
                    tier = EXCLUDED.tier, atr_pct = EXCLUDED.atr_pct,
                    atr_percentile_rank = EXCLUDED.atr_percentile_rank,
                    price = EXCLUDED.price, atr_period = EXCLUDED.atr_period,
                    run_date = EXCLUDED.run_date, updated_at = now()
                """,
                [r["symbol"], tier, r["atr_pct"], r.get("atr_percentile_rank"),
                 r.get("price"), r.get("atr_period"), run_date],
            )
        conn.commit()
    return len(rows)


def load_atr_snapshot() -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol, tier, atr_pct, atr_percentile_rank, price, atr_period,
                   run_date, intraday_stats, intraday_stats_date
            FROM {HV_ATR_SNAPSHOT_TABLE}
            ORDER BY atr_pct DESC
            """
        )
        return [
            {"symbol": sym, "tier": tier, "atr_pct": float(atr), "atr_percentile_rank":
             float(rank) if rank is not None else None,
             "price": float(price) if price is not None else None, "atr_period": period,
             "run_date": run_date.isoformat(), "intraday_stats": stats,
             "intraday_stats_date": stats_date.isoformat() if stats_date else None}
            for sym, tier, atr, rank, price, period, run_date, stats, stats_date in cur.fetchall()
        ]


def save_intraday_stats(symbol: str, stats: dict, stats_date) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {HV_ATR_SNAPSHOT_TABLE}
            SET intraday_stats = %s, intraday_stats_date = %s
            WHERE symbol = %s
            """,
            [Json(stats), stats_date, symbol],
        )
        conn.commit()


def load_hv_settings() -> dict | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT params FROM {HV_SETTINGS_TABLE} WHERE id = 1")
        row = cur.fetchone()
        return row[0] if row else None


def save_hv_settings(params: dict) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {HV_SETTINGS_TABLE} (id, params, updated_at)
            VALUES (1, %s, now())
            ON CONFLICT (id) DO UPDATE SET params = EXCLUDED.params, updated_at = now()
            """,
            [Json(params)],
        )
        conn.commit()


def load_hv_day(trade_date) -> dict:
    """The day's pre-open notes plus its focus list."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT gift_nifty, note FROM {HV_DAY_TABLE} WHERE trade_date = %s", [trade_date]
        )
        row = cur.fetchone()
        cur.execute(
            f"""
            SELECT symbol, bias, tags, note, pre_meta
            FROM {HV_FOCUS_TABLE} WHERE trade_date = %s ORDER BY updated_at, symbol
            """,
            [trade_date],
        )
        focus = [{"symbol": s, "bias": b, "tags": t, "note": n, "pre_meta": m}
                 for s, b, t, n, m in cur.fetchall()]
    return {
        "trade_date": trade_date.isoformat(),
        "gift_nifty": float(row[0]) if row and row[0] is not None else None,
        "note": row[1] if row else None,
        "focus": focus,
    }


def save_hv_day(trade_date, gift_nifty, note, focus: list[dict]) -> None:
    """Replaces the day's notes and its whole focus list (the page always
    sends the complete list, so removing a ticker removes its row)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {HV_DAY_TABLE} (trade_date, gift_nifty, note, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (trade_date) DO UPDATE SET
                gift_nifty = EXCLUDED.gift_nifty, note = EXCLUDED.note, updated_at = now()
            """,
            [trade_date, gift_nifty, note],
        )
        cur.execute(f"DELETE FROM {HV_FOCUS_TABLE} WHERE trade_date = %s", [trade_date])
        for f in focus:
            cur.execute(
                f"""
                INSERT INTO {HV_FOCUS_TABLE} (trade_date, symbol, bias, tags, note, pre_meta)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [trade_date, f["symbol"], f.get("bias") or "long", Json(f.get("tags") or []),
                 f.get("note"), Json(f["pre_meta"]) if f.get("pre_meta") is not None else None],
            )
        conn.commit()


def create_hv_position(trade_date, symbol: str, side: str, qty: int, price, filled_at=None,
                       setup_tag: str | None = None, note: str | None = None,
                       signal_meta: dict | None = None) -> int:
    """Opens a position together with its first entry fill."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {HV_POSITIONS_TABLE} (trade_date, symbol, side, setup_tag, note, signal_meta)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            [trade_date, symbol, side, setup_tag, note,
             Json(signal_meta) if signal_meta is not None else None],
        )
        position_id = cur.fetchone()[0]
        cur.execute(
            f"""
            INSERT INTO {HV_FILLS_TABLE} (position_id, kind, qty, price, filled_at)
            VALUES (%s, 'entry', %s, %s, COALESCE(%s::timestamptz, now()))
            """,
            [position_id, qty, price, filled_at],
        )
        conn.commit()
        return position_id


def add_hv_fill(position_id: int, kind: str, qty: int, price, filled_at=None) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {HV_FILLS_TABLE} (position_id, kind, qty, price, filled_at)
            VALUES (%s, %s, %s, %s, COALESCE(%s::timestamptz, now())) RETURNING id
            """,
            [position_id, kind, qty, price, filled_at],
        )
        fill_id = cur.fetchone()[0]
        conn.commit()
        return fill_id


def update_hv_position(position_id: int, setup_tag, note, execution_rating) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {HV_POSITIONS_TABLE}
            SET setup_tag = %s, note = %s, execution_rating = %s
            WHERE id = %s
            """,
            [setup_tag, note, execution_rating, position_id],
        )
        conn.commit()


def delete_hv_position(position_id: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {HV_POSITIONS_TABLE} WHERE id = %s", [position_id])
        conn.commit()


def delete_hv_fill(fill_id: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {HV_FILLS_TABLE} WHERE id = %s", [fill_id])
        conn.commit()


def list_hv_positions(from_date, to_date=None) -> list[dict]:
    """Positions (with all their fills) from from_date onward, newest first."""
    query = f"""
        SELECT id, trade_date, symbol, side, setup_tag, note, execution_rating, signal_meta, created_at
        FROM {HV_POSITIONS_TABLE} WHERE trade_date >= %s
    """
    params = [from_date]
    if to_date is not None:
        query += " AND trade_date <= %s"
        params.append(to_date)
    query += " ORDER BY trade_date DESC, id DESC"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        fills_by_position: dict[int, list[dict]] = {}
        if ids:
            cur.execute(
                f"""
                SELECT id, position_id, kind, qty, price, filled_at
                FROM {HV_FILLS_TABLE} WHERE position_id = ANY(%s) ORDER BY filled_at, id
                """,
                [ids],
            )
            for fid, pid, kind, qty, price, filled_at in cur.fetchall():
                fills_by_position.setdefault(pid, []).append({
                    "id": fid, "kind": kind, "qty": qty, "price": float(price),
                    "filled_at": filled_at.isoformat(),
                })

    return [
        {"id": pid, "trade_date": d.isoformat(), "symbol": sym, "side": side,
         "setup_tag": tag, "note": note, "execution_rating": rating, "signal_meta": meta,
         "created_at": created.isoformat(), "fills": fills_by_position.get(pid, [])}
        for pid, d, sym, side, tag, note, rating, meta, created in rows
    ]


def get_hv_position(position_id: int) -> dict | None:
    """One position with its fills (same shape as list_hv_positions rows)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT trade_date FROM {HV_POSITIONS_TABLE} WHERE id = %s", [position_id])
        row = cur.fetchone()
    if not row:
        return None
    matches = [p for p in list_hv_positions(row[0], row[0]) if p["id"] == position_id]
    return matches[0] if matches else None
