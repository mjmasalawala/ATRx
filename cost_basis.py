"""
Cost-basis replay for the "Cost Basis" page.

Unlike the standard weighted-average-cost method (avg cost per share stays
flat across a partial sell; realized profit is booked separately), this
nets realized profit from every sell straight into the cost basis of the
shares still held -- so the "avg cost" shown is literally how much money
per remaining share is still at risk. Once total_cost drops to zero or
below, the position is "free": historical profit has already paid back the
full original investment.

Pure calculation, no I/O -- callers (trade_csv_import.py,
cost_basis_state_sync.py, api/index.py) load the trades and pass them in.

Runs purely off trade-level history (from the uploaded tradebook CSVs --
see trade_csv_import.py), not a baseline snapshot. An earlier version
also accepted a "baseline" (a Kite holdings snapshot used as a starting
point back when only incremental trades were obtainable from Kite's live
API). That's been dropped: once full trade history is available for
every symbol, a baseline is redundant -- worse, replaying both baseline
and the trades it already summarizes double-counts everything before the
baseline date. If a symbol's full history genuinely isn't obtainable
(e.g. positions transferred in from elsewhere), that's a case to handle
explicitly if/when it comes up, not something to silently paper over
with a baseline blended into every symbol's numbers by default.
"""

from dataclasses import dataclass


@dataclass
class LedgerEntry:
    trade_id: str | None
    trade_time: str | None
    side: str
    quantity: float
    price: float
    realized: float
    quantity_after: float
    total_cost_after: float
    avg_cost_after: float | None


@dataclass
class CostBasisState:
    symbol: str
    quantity: float = 0.0
    total_cost: float = 0.0
    cumulative_realized: float = 0.0
    lifetime_realized: float = 0.0

    @property
    def avg_cost(self) -> float | None:
        return self.total_cost / self.quantity if self.quantity else None

    @property
    def is_free(self) -> bool:
        return self.quantity > 0 and self.total_cost <= 0


_QTY_EPSILON = 1e-6  # float-rounding tolerance for "effectively zero"


def replay(symbol: str, trades: list[dict]) -> dict:
    """Replays trades (chronological) into a trade-by-trade ledger and the
    resulting final state. A lot that's fully exited (quantity settles at
    ~zero) resets to a fresh cost basis on the next buy -- lifetime_realized
    keeps accumulating across lots, but cumulative_realized (what backs the
    current "FREE" check) does not.

    Raises ValueError if a sell would ever take quantity meaningfully
    negative. This is deliberately NOT treated as "open a short position" --
    this module's whole cost-basis-reduction model (and "FREE" concept) is
    built around long holdings only, so a short here means either genuinely
    shorted activity that this calculation doesn't yet know how to
    represent, or a sell landing before its matching buy due to incomplete/
    out-of-order trade history. Either way, silently clamping to zero
    (an earlier version did this) produces a wrong-but-plausible-looking
    leftover quantity -- surfacing it as a failure is safer until short
    positions are explicitly designed for."""
    state = CostBasisState(symbol=symbol)
    ledger: list[LedgerEntry] = []

    for trade in trades:
        side = trade["side"]
        qty = float(trade["quantity"])
        price = float(trade["price"])
        realized = 0.0

        if side == "BUY":
            state.total_cost += qty * price
            state.quantity += qty
        elif side == "SELL":
            avg = state.avg_cost or 0.0
            realized = (price - avg) * qty
            state.quantity -= qty
            state.total_cost = state.quantity * avg - realized
            state.cumulative_realized += realized
            state.lifetime_realized += realized
        else:
            raise ValueError(f"Unknown trade side: {side!r}")

        if state.quantity < -_QTY_EPSILON:
            raise ValueError(
                f"{symbol}: sell (trade_id={trade.get('trade_id')}, {trade.get('trade_time')}) "
                f"leaves quantity at {state.quantity:.4f} -- negative. Either this symbol has short "
                f"activity (not yet supported by this long-only cost-basis model) or its trade "
                f"history is incomplete/out of order (a sell landing before its matching buy)."
            )

        if state.quantity <= _QTY_EPSILON:
            state.quantity = 0.0
            state.total_cost = 0.0
            state.cumulative_realized = 0.0

        ledger.append(LedgerEntry(
            trade_id=trade.get("trade_id"),
            trade_time=trade.get("trade_time"),
            side=side,
            quantity=qty,
            price=price,
            realized=realized,
            quantity_after=state.quantity,
            total_cost_after=state.total_cost,
            avg_cost_after=state.avg_cost,
        ))

    return {
        "symbol": symbol,
        "quantity": state.quantity,
        "total_cost": state.total_cost,
        "avg_cost": state.avg_cost,
        "cumulative_realized": state.cumulative_realized,
        "lifetime_realized": state.lifetime_realized,
        "is_free": state.is_free,
        "ledger": [vars(e) for e in ledger],
    }


if __name__ == "__main__":
    # Self-check against the example worked through with the user:
    # buy 100@10, sell 20@12, buy 50@9.75 -> avg should land at ~9.596.
    result = replay("DEMO", [
        {"trade_id": "t1", "trade_time": "2026-01-01", "side": "BUY", "quantity": 100, "price": 10},
        {"trade_id": "t2", "trade_time": "2026-01-02", "side": "SELL", "quantity": 20, "price": 12},
        {"trade_id": "t3", "trade_time": "2026-01-03", "side": "BUY", "quantity": 50, "price": 9.75},
    ])
    assert result["quantity"] == 130
    assert abs(result["total_cost"] - 1247.5) < 1e-9, result["total_cost"]
    assert abs(result["avg_cost"] - 1247.5 / 130) < 1e-9, result["avg_cost"]
    print("OK:", result["avg_cost"])
