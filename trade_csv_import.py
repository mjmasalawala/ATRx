"""
Trade history for the Cost Basis page comes from a manually-uploaded CSV,
not a live Kite API call. Kite Connect's /trades endpoint only ever
returns the CURRENT trading day's fills -- there's no date-range
parameter and no historical-trades endpoint in the Connect API, so a
periodic or on-demand pull through it silently loses any trading day that
gets skipped (weekends off, forgot to click, etc). Zerodha's Console
("kite.zerodha.com/console" -> Reports -> Tradebook) exports the full,
authoritative trade history as a CSV for any date range instead, so
that's the source of truth here: download it there, upload it on the
Cost Basis page.

Column names are matched case-insensitively against Zerodha's published
tradebook export headers (symbol, isin, trade_date, exchange, segment,
series, trade_type, auction, quantity, price, trade_id, order_id,
order_execution_time) with a few common aliases -- if Zerodha's export
format doesn't match what's here, parse_trades_csv raises with the header
row it actually found, rather than silently mis-mapping columns.
"""

import csv
import io
from datetime import datetime, timezone

import cost_basis_state_sync
import db_store

_COLUMN_ALIASES = {
    "symbol": ("symbol", "tradingsymbol", "trading symbol"),
    "exchange": ("exchange",),
    "side": ("trade_type", "side", "transaction_type", "trade type"),
    "quantity": ("quantity", "qty"),
    "price": ("price", "trade_price", "average_price"),
    "trade_time": ("order_execution_time", "trade_time", "exchange_timestamp", "fill_timestamp"),
    "trade_date": ("trade_date", "date"),
    "trade_id": ("trade_id", "tradeid"),
    "order_id": ("order_id", "orderid"),
}


def _normalize_header(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _build_column_map(fieldnames: list[str]) -> dict[str, str]:
    normalized = {_normalize_header(f): f for f in fieldnames}
    column_map = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                column_map[canonical] = normalized[alias]
                break
    return column_map


_REQUIRED_COLUMNS = ("symbol", "side", "quantity", "price", "trade_id")


def parse_trades_csv(file_content: str) -> list[dict]:
    """Parses a Zerodha tradebook CSV export into rows matching
    db_store.upsert_trades' shape. Raises ValueError with the actual
    header row if required columns can't be found (rather than silently
    importing garbage), and skips (rather than crashes on) individual
    rows missing a value in a required column, collecting them so the
    caller can report exactly what didn't import."""
    reader = csv.DictReader(io.StringIO(file_content))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row.")

    column_map = _build_column_map(reader.fieldnames)
    missing = [c for c in _REQUIRED_COLUMNS if c not in column_map]
    if missing:
        raise ValueError(
            f"Could not find column(s) {missing} in the CSV header. "
            f"Header found: {reader.fieldnames}"
        )

    rows = []
    skipped = []
    for i, raw in enumerate(reader, start=2):  # start=2: row 1 is the header
        try:
            side = raw[column_map["side"]].strip().upper()
            if side not in ("BUY", "SELL"):
                raise ValueError(f"unrecognized trade_type/side {side!r}")

            trade_time = None
            if "trade_time" in column_map:
                trade_time = raw[column_map["trade_time"]].strip() or None
            if not trade_time and "trade_date" in column_map:
                trade_time = raw[column_map["trade_date"]].strip() or None
            if not trade_time:
                raise ValueError("no trade_time or trade_date value")

            rows.append({
                "trade_id": raw[column_map["trade_id"]].strip(),
                "symbol": raw[column_map["symbol"]].strip().upper(),
                "exchange": raw[column_map["exchange"]].strip() if "exchange" in column_map else None,
                "side": side,
                "quantity": float(raw[column_map["quantity"]]),
                "price": float(raw[column_map["price"]]),
                "trade_time": trade_time,
                "order_id": raw[column_map["order_id"]].strip() if "order_id" in column_map else None,
            })
        except (KeyError, ValueError) as e:
            skipped.append({"row": i, "reason": str(e)})

    return rows, skipped


def import_trades_csv(file_content: str) -> dict:
    rows, skipped = parse_trades_csv(file_content)

    inserted = db_store.upsert_trades(rows)
    recompute_result = cost_basis_state_sync.recompute_all()

    now = datetime.now(timezone.utc)
    db_store.set_last_trades_sync(now)

    return {
        "parsed": len(rows) + len(skipped),
        "inserted": inserted,
        "symbols_updated": sorted({row["symbol"] for row in rows}),
        "skipped_rows": skipped,
        "state_updated": len(recompute_result["updated"]),
        "state_failed": recompute_result["failed"],
        "synced_at": now.isoformat(),
    }
