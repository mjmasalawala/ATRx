"""
Unit tests for hv_journal.py -- charges, long/short P&L, scale-in and
partial-exit handling, target/break-even prices, day roll-ups. Pure math,
no DB or Kite.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hv_config import HVConfig
from hv_journal import (break_even_price, build_journal, day_summary, fill_charges,
                        position_summary, target_price)


def test_charges_roughly_match_published_zerodha_example():
    # ~35k round trip: brokerage ~10.5+~10.5, STT ~8.8 on the sell, plus
    # small exchange/SEBI/stamp/GST -- total should land around Rs 37.
    buy = fill_charges("long", "entry", 114, 306.0)
    sell = fill_charges("long", "exit", 114, 313.0)
    assert 33 < buy + sell < 42
    # STT only on the sell side for a long
    assert sell > buy


def test_short_puts_stt_on_entry():
    entry = fill_charges("short", "entry", 100, 300.0)
    exit_ = fill_charges("short", "exit", 100, 297.0)
    assert entry > exit_


def test_long_round_trip_net_is_gross_minus_charges():
    fills = [{"kind": "entry", "qty": 100, "price": 306.0},
             {"kind": "exit", "qty": 100, "price": 313.0}]
    s = position_summary("long", fills)
    assert s["status"] == "closed"
    assert s["gross_pnl"] == 700.0
    assert s["charges"] > 0
    assert s["net_pnl"] == pytest.approx(700.0 - s["charges"], abs=0.01)
    assert s["net_pct"] == pytest.approx(s["net_pnl"] / 30600 * 100, abs=0.01)


def test_short_profits_when_price_falls():
    fills = [{"kind": "entry", "qty": 100, "price": 300.0},
             {"kind": "exit", "qty": 100, "price": 294.0}]
    s = position_summary("short", fills)
    assert s["gross_pnl"] == 600.0
    assert s["net_pnl"] < 600.0


def test_scale_in_uses_weighted_average_entry():
    fills = [{"kind": "entry", "qty": 100, "price": 100.0},
             {"kind": "entry", "qty": 100, "price": 96.0},    # averaged down
             {"kind": "exit", "qty": 200, "price": 99.0}]
    s = position_summary("long", fills)
    assert s["avg_entry"] == 98.0
    assert s["gross_pnl"] == 200.0
    assert s["status"] == "closed"


def test_partial_exit_leaves_position_open_and_only_charges_exited_share():
    fills = [{"kind": "entry", "qty": 100, "price": 100.0},
             {"kind": "exit", "qty": 40, "price": 102.0}]
    s = position_summary("long", fills)
    assert s["status"] == "open" and s["open_qty"] == 60
    assert s["gross_pnl"] == 80.0
    full_entry = fill_charges("long", "entry", 100, 100.0)
    exit_c = fill_charges("long", "exit", 40, 102.0)
    assert s["charges"] == pytest.approx(full_entry * 0.4 + exit_c, abs=0.01)


def test_open_position_has_no_pnl_yet():
    s = position_summary("long", [{"kind": "entry", "qty": 10, "price": 50.0}])
    assert s["status"] == "open" and s["gross_pnl"] == 0.0 and s["net_pct"] is None


def test_target_price_yields_requested_net_pct():
    for side in ("long", "short"):
        px = target_price(side, 306.0, 114, 1.0)
        exit_kind = [{"kind": "entry", "qty": 114, "price": 306.0},
                     {"kind": "exit", "qty": 114, "price": px}]
        s = position_summary(side, exit_kind)
        assert s["net_pct"] == pytest.approx(1.0, abs=0.05)
    assert target_price("long", 306.0, 114, 1.0) > 306.0
    assert target_price("short", 306.0, 114, 1.0) < 306.0


def test_break_even_is_slightly_beyond_entry_in_profit_direction():
    assert 306.0 < break_even_price("long", 306.0, 114) < 308.0
    assert 304.0 < break_even_price("short", 306.0, 114) < 306.0


def _pos(date, symbol, side, entry, exit_, qty=100):
    fills = [{"kind": "entry", "qty": qty, "price": entry}]
    if exit_ is not None:
        fills.append({"kind": "exit", "qty": qty, "price": exit_})
    return {"id": 1, "trade_date": date, "symbol": symbol, "side": side, "fills": fills}


def test_day_summary_counts_round_trips_and_flags_loss_limit():
    cfg = HVConfig(capital=25000, daily_loss_pct=2.0, target_round_trips=4, max_tickers=3)  # limit Rs 500
    journal = build_journal([_pos("2026-10-22", "ETERNAL", "long", 306, 313),
                             _pos("2026-10-22", "ETERNAL", "long", 310, 315),
                             _pos("2026-10-22", "ABB", "long", 100, 95)], cfg)
    day = journal["days"][0]
    assert day["round_trips"] == 3 and day["ticker_count"] == 2
    assert day["net_pnl"] == pytest.approx(700 + 500 - 500 - sum(
        p["summary"]["charges"] for p in journal["positions"]), abs=0.01)
    assert not day["loss_limit_hit"]

    bad = build_journal([_pos("2026-10-22", "ABB", "long", 100, 90)], cfg)["days"][0]
    assert bad["loss_limit_hit"]


def test_done_for_day_when_target_round_trips_reached():
    cfg = HVConfig(target_round_trips=2)
    day = build_journal([_pos("2026-10-22", "A", "long", 100, 102),
                         _pos("2026-10-22", "B", "long", 100, 102)], cfg)["days"][0]
    assert day["done_for_day"]


def test_journal_cumulative_net_runs_across_days():
    cfg = HVConfig()
    journal = build_journal([_pos("2026-10-22", "A", "long", 100, 102),
                             _pos("2026-10-21", "A", "long", 100, 101)], cfg)
    assert [d["date"] for d in journal["days"]] == ["2026-10-21", "2026-10-22"]
    assert journal["days"][1]["cumulative_net"] == pytest.approx(journal["totals"]["net_pnl"], abs=0.01)
    assert journal["totals"]["trading_days"] == 2
