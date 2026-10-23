"""
Unit tests for hv_risk.py -- position sizing and the day / week / month
limits from the risk review. Pure logic, no DB or Kite.
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hv_config import HVConfig
from hv_journal import build_journal
from hv_risk import day_state, entry_blockers, period_state, size_trade


def test_size_trade_matches_the_docs_worked_example():
    # Rs 2,00,000 capital, 1R = 0.5% = Rs 1,000, entry 420, stop 415.80.
    cfg = HVConfig(capital=200000, risk_pct=0.5)
    s = size_trade(420.0, 415.80, cfg, cfg.one_r, avg_5m_value=1.2e7, exposure_left=400000)
    assert round(s["risk_per_share"], 2) == 5.04
    assert s["caps"] == {"risk": 198, "liquidity": 285, "exposure": 952}
    assert s["qty"] == 198 and s["binding"] == "risk"
    assert round(s["notional"]) == 83160


def test_liquidity_cap_binds_in_thin_stocks():
    cfg = HVConfig(capital=500000, risk_pct=0.5)
    s = size_trade(420.0, 415.80, cfg, cfg.one_r, avg_5m_value=1.2e7)
    assert s["qty"] == 285 and s["binding"] == "liquidity"


def _pos(symbol, entry, exit_, qty=100, exit_at="2026-10-22T04:10:00+00:00", **extra):
    fills = [{"kind": "entry", "qty": qty, "price": entry, "filled_at": "2026-10-22T04:00:00+00:00"}]
    if exit_ is not None:
        fills.append({"kind": "exit", "qty": qty, "price": exit_, "filled_at": exit_at})
    return {"id": 1, "trade_date": "2026-10-22", "symbol": symbol, "side": "long", "fills": fills, **extra}


def _positions(*ps):
    return build_journal(list(ps), HVConfig())["positions"]


NOW = datetime(2026, 10, 22, 9, 50)


def test_two_losses_in_a_row_start_a_cooloff():
    cfg = HVConfig()
    ps = _positions(_pos("A", 100, 99, exit_at="2026-10-22T04:12:00+00:00"),
                    _pos("B", 100, 99, exit_at="2026-10-22T04:15:00+00:00"))  # 9:45 IST
    st = day_state(ps, cfg, NOW)
    assert st["loss_streak"] == 2 and st["lockout"] is None
    assert st["cooloff_until"] == "09:55"
    assert entry_blockers("C", st, cfg)[0].startswith("cool-off")


def test_three_losses_or_the_daily_limit_stop_the_day():
    cfg = HVConfig()
    st = day_state(_positions(*[_pos(s, 100, 99) for s in "ABC"]), cfg, NOW)
    assert "3 losses" in st["lockout"]
    big = day_state(_positions(_pos("A", 100, 95, qty=900)), cfg, NOW)  # -Rs 4,500 > 2% of 2L
    assert "daily loss limit" in big["lockout"]
    assert big["daily_limit_r"] == 8  # 2% at 1R = 0.25%


def test_giving_back_half_the_peak_stops_the_day():
    cfg = HVConfig()  # 1R = 500, trigger +4R = 2,000
    ps = _positions(_pos("A", 100, 122, exit_at="2026-10-22T04:05:00+00:00"),
                    _pos("B", 100, 88, exit_at="2026-10-22T04:20:00+00:00"))
    st = day_state(ps, cfg, NOW)
    assert st["peak_r"] > 4 and "gave back" in st["lockout"]


def test_rule_break_and_flat_time_lock_the_day():
    cfg = HVConfig()
    assert day_state(_positions(_pos("A", 100, 101, rule_broken=True)), cfg, NOW)["lockout"] == "a rule was broken today"
    assert "session over" in day_state([], cfg, datetime(2026, 10, 22, 15, 0))["lockout"]


def test_open_positions_and_open_risk_limit_entries():
    cfg = HVConfig()
    ps = _positions(_pos("A", 100, None, qty=200, planned_stop=98.0, risk_amount=440),
                    _pos("B", 100, None, qty=200, planned_stop=98.0, risk_amount=440))
    st = day_state(ps, cfg, NOW)
    assert st["open_positions"] == 2 and st["exposure"] == 40000
    assert st["open_risk_r"] > 1.5
    blockers = entry_blockers("C", st, cfg)
    assert any("positions open" in b for b in blockers)
    assert any("open risk" in b for b in blockers)


def test_period_state_halves_size_after_a_losing_week():
    cfg = HVConfig()  # weekly limit 4% of 2L = 8,000
    last_week = {"id": 2, "trade_date": "2026-10-14", "symbol": "A", "side": "long",
                 "fills": [{"kind": "entry", "qty": 1000, "price": 100, "filled_at": "2026-10-14T04:00:00+00:00"},
                           {"kind": "exit", "qty": 1000, "price": 91, "filled_at": "2026-10-14T04:30:00+00:00"}]}
    ps = build_journal([last_week], cfg)["positions"]
    st = period_state(ps, cfg, date(2026, 10, 22))
    assert st["size_mult"] == 0.5 and st["size_reasons"]
    assert st["monthly_stop"] is False  # about -9,000, under 6% of 2L (12,000)

    another = {**last_week, "id": 3, "trade_date": "2026-10-20",
               "fills": [{**f, "filled_at": f["filled_at"].replace("10-14", "10-20")} for f in last_week["fills"]]}
    ps = build_journal([last_week, another], cfg)["positions"]
    assert period_state(ps, cfg, date(2026, 10, 22))["monthly_stop"] is True
