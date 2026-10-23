"""
Risk limits for the High Volatility page: position sizing from the stop,
and the day / week / month state that decides whether new setups may arm.
Pure functions over journal positions (the dicts build_journal returns,
each with a `summary`), so the rules are unit-testable. The page never
places or blocks orders -- a lockout only stops setups from arming and is
recorded as a rule break if a trade is logged anyway.

R: 1R is the planned loss of one trade (capital x risk_pct). A position's
R result is its net P&L / the risk amount stored when it was opened.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from hv_config import HVConfig

IST = ZoneInfo("Asia/Kolkata")


def to_ist_naive(iso: str) -> datetime:
    """A stored timestamp (ISO, usually with an offset) as naive IST."""
    dt = datetime.fromisoformat(iso)
    return dt.astimezone(IST).replace(tzinfo=None) if dt.tzinfo else dt


def size_trade(entry: float, stop: float, cfg: HVConfig, one_r: float, avg_5m_value: float,
               exposure_left: float | None = None) -> dict:
    """Shares = the smallest of the risk, liquidity and exposure caps. The
    loss per share includes cfg.cost_pct of round-trip costs."""
    rps = (entry - stop) + entry * cfg.cost_pct / 100
    caps = {}
    caps["risk"] = math.floor(one_r / rps) if rps > 0 else 0
    if avg_5m_value:
        caps["liquidity"] = math.floor(avg_5m_value * cfg.liquidity_pct / 100 / entry)
    if exposure_left is not None:
        caps["exposure"] = max(0, math.floor(exposure_left / entry))
    binding = min(caps, key=caps.get)
    qty = max(0, caps[binding])
    return {"qty": qty, "risk_per_share": round(rps, 4), "risk_amount": round(qty * rps, 2),
            "notional": round(qty * entry, 2), "caps": caps, "binding": binding}


def _closed_at(p: dict) -> str | None:
    exits = [f["filled_at"] for f in p["fills"] if f["kind"] == "exit"]
    return max(exits) if exits else None


def _opened_at(p: dict) -> str | None:
    entries = [f["filled_at"] for f in p["fills"] if f["kind"] == "entry"]
    return min(entries) if entries else None


def position_r(p: dict, cfg: HVConfig) -> float | None:
    risk = p.get("risk_amount") or cfg.one_r
    if not risk or p["summary"]["exit_qty"] == 0:
        return None
    return round(p["summary"]["net_pnl"] / risk, 2)


def open_risk(p: dict, cfg: HVConfig) -> float:
    """Rupees lost if the open quantity hit the planned stop now."""
    s = p["summary"]
    if not s["open_qty"] or not s["avg_entry"]:
        return 0.0
    stop = p.get("planned_stop")
    if not stop:
        return p.get("risk_amount") or cfg.one_r
    per_share = (s["avg_entry"] - stop) if p["side"] == "long" else (stop - s["avg_entry"])
    return max(0.0, per_share * s["open_qty"] + s["avg_entry"] * s["open_qty"] * cfg.cost_pct / 100)


def day_state(positions_today: list[dict], cfg: HVConfig, now: datetime, one_r: float | None = None) -> dict:
    """Today's risk bar and lockout. now: naive IST datetime."""
    one_r = one_r or cfg.one_r
    closed = sorted((p for p in positions_today if p["summary"]["status"] == "closed"),
                    key=lambda p: _closed_at(p) or "")
    opened = [p for p in positions_today if p["summary"]["status"] == "open"]
    realized = sum(p["summary"]["net_pnl"] for p in positions_today)

    streak = 0
    for p in reversed(closed):
        if p["summary"]["net_pnl"] < 0:
            streak += 1
        else:
            break
    cum, peak = 0.0, 0.0
    for p in closed:
        cum += p["summary"]["net_pnl"]
        peak = max(peak, cum)
    trips_by_stock: dict[str, int] = {}
    for p in closed:
        trips_by_stock[p["symbol"]] = trips_by_stock.get(p["symbol"], 0) + 1

    exposure = sum(p["summary"]["open_qty"] * (p["summary"]["avg_entry"] or 0) for p in opened)
    risk_open = sum(open_risk(p, cfg) for p in opened)
    rule_breaks = [p for p in positions_today if p.get("rule_broken")]

    lockout = None
    limit = cfg.daily_loss_limit
    if realized <= -limit:
        lockout = f"daily loss limit hit ({realized / one_r:+.1f}R, limit -{limit / one_r:g}R)"
    elif streak >= cfg.stop_after_losses:
        lockout = f"{streak} losses in a row"
    elif peak >= cfg.giveback_trigger_r * one_r and cum <= peak * (1 - cfg.giveback_frac):
        lockout = f"gave back half of the day's +{peak / one_r:.1f}R peak"
    elif len(closed) >= cfg.max_trips_per_day:
        lockout = f"{len(closed)} round trips -- the daily maximum"
    elif rule_breaks:
        lockout = "a rule was broken today"

    flat = datetime.combine(now.date(), datetime.strptime(cfg.flat_by, "%H:%M").time())
    if lockout is None and now >= flat:
        lockout = f"session over (flat by {cfg.flat_by})"

    cooloff_until = None
    if lockout is None and streak >= cfg.cooloff_after_losses and closed:
        until = to_ist_naive(_closed_at(closed[-1])) + timedelta(minutes=cfg.cooloff_minutes)
        if now < until:
            cooloff_until = until.strftime("%H:%M")

    return {
        "one_r": round(one_r, 2),
        "realized": round(realized, 2),
        "day_r": round(realized / one_r, 2) if one_r else 0.0,
        "peak_r": round(peak / one_r, 2) if one_r else 0.0,
        "loss_streak": streak,
        "round_trips": len(closed),
        "max_round_trips": cfg.max_trips_per_day,
        "trips_by_stock": trips_by_stock,
        "open_positions": len(opened),
        "open_risk_r": round(risk_open / one_r, 2) if one_r else 0.0,
        "exposure": round(exposure, 2),
        "exposure_cap": round(cfg.capital * cfg.gross_exposure_x, 2),
        "exposure_left": round(max(0.0, cfg.capital * cfg.gross_exposure_x - exposure), 2),
        "daily_limit": round(limit, 2),
        "daily_limit_r": round(limit / one_r, 2) if one_r else 0.0,
        "rule_breaks": len(rule_breaks),
        "lockout": lockout,
        "cooloff_until": cooloff_until,
    }


def entry_blockers(symbol: str, state: dict, cfg: HVConfig, nifty_down: bool = False) -> list[str]:
    """Why a new entry on `symbol` is not allowed right now (empty = ok)."""
    out = []
    if state.get("lockout"):
        out.append("Day stopped: " + state["lockout"])
    if state.get("cooloff_until"):
        out.append(f"cool-off until {state['cooloff_until']}")
    max_open = 1 if nifty_down else cfg.max_open_positions
    if state["open_positions"] >= max_open:
        out.append(f"{state['open_positions']} positions open (max {max_open})")
    if state["trips_by_stock"].get(symbol, 0) >= cfg.max_trips_per_stock:
        out.append(f"{cfg.max_trips_per_stock} round trips on {symbol} already")
    if state["open_positions"] and state["open_risk_r"] >= cfg.max_correlated_r:
        out.append(f"open risk {state['open_risk_r']:.1f}R -- wait for a trade to close")
    return out


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def period_state(positions: list[dict], cfg: HVConfig, today: date) -> dict:
    """Weekly / monthly limits and the rolling-20 expectancy, from every
    position in roughly the last two months. size_mult halves 1R after a
    losing week (weekly limit hit) or while the last 20 trades are negative;
    monthly_stop is set once the month's drawdown from its peak reaches
    monthly_dd_pct of capital."""
    def d_of(p):
        return date.fromisoformat(p["trade_date"])

    this_week, last_week = _week_start(today), _week_start(today) - timedelta(days=7)
    week_net = sum(p["summary"]["net_pnl"] for p in positions if d_of(p) >= this_week)
    last_week_net = sum(p["summary"]["net_pnl"] for p in positions if last_week <= d_of(p) < this_week)

    month = sorted((p for p in positions if d_of(p).year == today.year and d_of(p).month == today.month),
                   key=lambda p: (p["trade_date"], _closed_at(p) or ""))
    cum = peak = dd = 0.0
    for p in month:
        cum += p["summary"]["net_pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)

    closed = sorted((p for p in positions if p["summary"]["status"] == "closed"),
                    key=lambda p: (p["trade_date"], _closed_at(p) or ""))
    last20 = [r for r in (position_r(p, cfg) for p in closed[-20:]) if r is not None]
    rolling = round(sum(last20) / len(last20), 2) if last20 else None

    weekly_limit = cfg.capital * cfg.weekly_loss_pct / 100
    mult, why = 1.0, []
    if last_week_net <= -weekly_limit:
        mult, why = 0.5, why + ["last week hit the weekly loss limit"]
    if rolling is not None and len(last20) >= 20 and rolling < 0:
        mult, why = 0.5, why + ["the last 20 trades average below 0R"]
    monthly_limit = cfg.capital * cfg.monthly_dd_pct / 100
    return {
        "week_net": round(week_net, 2),
        "last_week_net": round(last_week_net, 2),
        "weekly_limit": round(weekly_limit, 2),
        "month_drawdown": round(dd, 2),
        "monthly_limit": round(monthly_limit, 2),
        "monthly_stop": dd >= monthly_limit,
        "rolling20_r": rolling,
        "rolling20_n": len(last20),
        "trades_total": len(closed),
        "size_mult": mult,
        "size_reasons": why,
    }
