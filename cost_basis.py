"""
Cost-basis replay for the "Cost Basis" page.

Unlike the standard weighted-average-cost method (avg cost per share stays
flat across a partial sell; realized profit is booked separately), this
nets realized profit from every sell straight into the cost basis of the
shares still held -- so the "avg cost" shown is literally how much money
per remaining share is still at risk. Once total_cost drops to zero or
below, the position is "free": historical profit has already paid back the
full original investment.

Pure calculation, no I/O -- callers (kite_sync.py, api/index.py) load the
baseline/trades and pass them in.
"""

from dataclasses import dataclass, field


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


def replay(symbol: str, baseline: dict | None, trades: list[dict]) -> dict:
    """Replays a baseline snapshot (if any) plus trades (chronological) into
    a trade-by-trade ledger and the resulting final state. A lot that's
    fully exited (quantity hits zero) resets to a fresh cost basis on the
    next buy -- lifetime_realized keeps accumulating across lots, but
    cumulative_realized (what backs the current "FREE" check) does not."""
    state = CostBasisState(symbol=symbol)
    ledger: list[LedgerEntry] = []

    if baseline:
        state.quantity = float(baseline["quantity"])
        state.total_cost = float(baseline["quantity"]) * float(baseline["avg_price"])
        ledger.append(LedgerEntry(
            trade_id=None,
            trade_time=baseline.get("as_of_date"),
            side="BASELINE",
            quantity=state.quantity,
            price=float(baseline["avg_price"]),
            realized=0.0,
            quantity_after=state.quantity,
            total_cost_after=state.total_cost,
            avg_cost_after=state.avg_cost,
        ))

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

        if state.quantity <= 0:
            state.quantity = max(state.quantity, 0.0)
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
    result = replay("DEMO", None, [
        {"trade_id": "t1", "trade_time": "2026-01-01", "side": "BUY", "quantity": 100, "price": 10},
        {"trade_id": "t2", "trade_time": "2026-01-02", "side": "SELL", "quantity": 20, "price": 12},
        {"trade_id": "t3", "trade_time": "2026-01-03", "side": "BUY", "quantity": 50, "price": 9.75},
    ])
    assert result["quantity"] == 130
    assert abs(result["total_cost"] - 1247.5) < 1e-9, result["total_cost"]
    assert abs(result["avg_cost"] - 1247.5 / 130) < 1e-9, result["avg_cost"]
    print("OK:", result["avg_cost"])
