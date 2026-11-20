"""hv_screener.build_candidates: the pre-open shortlist only draws from the
High Volatility universe tiers; hand-typed extras are never filtered by tier.
Kite and the DB are replaced with fakes."""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hv_screener
from hv_config import HVConfig

TODAY = date(2026, 11, 19)


def snap(symbol, tier, atr=4.0):
    return {"symbol": symbol, "tier": tier, "atr_pct": atr, "atr_percentile_rank": 90.0, "price": 100.0,
            "atr_period": 14, "run_date": TODAY.isoformat(), "intraday_stats": None, "intraday_stats_date": None}


@pytest.fixture
def captured(monkeypatch):
    seen = {}
    monkeypatch.setattr(hv_screener, "today_ist", lambda: TODAY)
    monkeypatch.setattr(hv_screener, "resolve_tokens", lambda kite, syms: {s: i for i, s in enumerate(syms, 1)})
    monkeypatch.setattr(hv_screener, "fetch_5min", lambda *a, **k: [])
    monkeypatch.setattr(hv_screener, "intraday_stats", lambda *a, **k: {"swing_pct": 1.5, "first_hour_minutes": 60})
    monkeypatch.setattr(hv_screener, "get_quotes", lambda kite, syms, extra: {})
    monkeypatch.setattr(hv_screener.db_store, "save_intraday_stats", lambda *a, **k: None)
    monkeypatch.setattr(hv_screener.time_module, "sleep", lambda s: None)

    def fake_rank(rows, cfg):
        seen["rows"] = rows
        return rows, []
    monkeypatch.setattr(hv_screener, "rank_candidates", fake_rank)
    return seen


def run(monkeypatch, captured, snapshot, extras=()):
    monkeypatch.setattr(hv_screener.db_store, "load_atr_snapshot", lambda: snapshot)
    out = hv_screener.build_candidates(object(), HVConfig(), list(extras))
    return out, {r["symbol"]: r for r in captured["rows"]}


def test_only_allowed_universe_tiers_reach_the_shortlist(monkeypatch, captured):
    snapshot = [snap("N100A", "NIFTY1001"), snap("N100B", "NIFTY1002"), snap("MID1", "NIFTYMIDCAP1501"),
                snap("MID2", "NIFTYMIDCAP1502"), snap("LARGE", "large_cap"), snap("SMALL", "small_cap"),
                snap("MICRO", "NIFTYMICROCAP2501"), snap("OLD", "NIFTYSMALLCAP2501")]
    out, rows = run(monkeypatch, captured, snapshot)
    assert set(rows) == {"N100A", "N100B", "MID1", "MID2"}
    assert out["shortlist_size"] == 4


def test_atr_floor_still_applies_inside_allowed_tiers(monkeypatch, captured):
    out, rows = run(monkeypatch, captured, [snap("HI", "NIFTY1001", 4.0), snap("LO", "NIFTY1001", 0.5)])
    assert set(rows) == {"HI"}


def test_hand_typed_extra_from_a_disallowed_tier_is_never_filtered(monkeypatch, captured):
    out, rows = run(monkeypatch, captured, [snap("LARGE", "large_cap"), snap("N100A", "NIFTY1001")],
                    extras=["LARGE", "NOTINSNAPSHOT"])
    assert set(rows) == {"N100A", "LARGE", "NOTINSNAPSHOT"}
    assert rows["LARGE"]["manual"] and rows["LARGE"]["tier"] == "large_cap"
    assert rows["NOTINSNAPSHOT"]["tier"] == "manual"


def test_extra_already_in_an_allowed_tier_is_marked_manual(monkeypatch, captured):
    out, rows = run(monkeypatch, captured, [snap("N100A", "NIFTY1001")], extras=["N100A"])
    assert rows["N100A"]["manual"] is True


def test_snapshot_with_no_allowed_tier_gives_an_empty_shortlist_not_an_error(monkeypatch, captured):
    out, rows = run(monkeypatch, captured, [snap("LARGE", "large_cap")])
    assert rows == {} and out["shortlist_size"] == 0 and out["snapshot_symbols"] == 1
