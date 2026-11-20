"""screener._persist_atr_snapshot: during NSE market hours the snapshot is
saved from history *without today's partial candle* (it used to be skipped,
leaving the High Volatility page with a stale snapshot)."""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_store
import screener

IST = ZoneInfo("Asia/Kolkata")


def make_history(last_day: str, partial_today: bool, days: int = 40) -> pd.DataFrame:
    """Daily candles ending on `last_day`; every full day has a 2-point range
    around 100. If partial_today, the last candle is a tiny 0.2-point range."""
    dates = pd.bdate_range(end=last_day, periods=days, tz=IST)
    rows = [{"timestamp": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000}
            for d in dates]
    if partial_today:
        rows[-1].update(open=100.0, high=100.1, low=99.9, close=100.0)
    return pd.DataFrame(rows)


class FixedNow:
    def __init__(self, when):
        self.when = when

    def now(self, tz=None):
        return self.when


@pytest.fixture
def saved(monkeypatch):
    calls = []
    monkeypatch.setattr(db_store, "upsert_atr_snapshot", lambda rows, tier, run_date: calls.append((rows, tier, run_date)))
    return calls


def at(monkeypatch, y, mo, d, h, mi):
    monkeypatch.setattr(screener, "datetime", FixedNow(datetime(y, mo, d, h, mi, tzinfo=IST)))


FULL_DAY_ATR_PCT = None  # computed from the clean history below


def test_market_hours_trims_todays_partial_candle(monkeypatch, saved):
    at(monkeypatch, 2026, 11, 19, 11, 0)  # Thursday 11:00 IST, market open
    history = {"AAA": make_history("2026-11-19", partial_today=True)}
    clean = screener.current_atr_and_price(history["AAA"][:-1])
    skewed = screener.current_atr_and_price(history["AAA"])
    assert clean != skewed

    screener._persist_atr_snapshot("NIFTY1001", {"AAA": 0.99}, {"AAA": 1.0}, lambda s: 1.0, history)

    rows, tier, run_date = saved[0]
    assert tier == "NIFTY1001" and run_date.isoformat() == "2026-11-19"
    assert rows[0]["atr_pct"] == round(clean[0] / clean[1] * 100, 2)  # not the 0.99 passed in
    assert rows[0]["price"] == round(clean[1], 2)
    assert rows[0]["atr_percentile_rank"] == 100.0


def test_market_hours_without_history_skips_the_save(monkeypatch, saved):
    at(monkeypatch, 2026, 11, 19, 11, 0)
    screener._persist_atr_snapshot("NIFTY1001", {"AAA": 0.02}, {"AAA": 100.0}, lambda s: 50.0, None)
    assert saved == []


@pytest.mark.parametrize("h,mi", [(8, 30), (15, 31), (20, 0)])
def test_outside_market_hours_saves_the_passed_values_as_is(monkeypatch, saved, h, mi):
    at(monkeypatch, 2026, 11, 19, h, mi)
    screener._persist_atr_snapshot("NIFTY1001", {"AAA": 0.0234}, {"AAA": 123.456}, lambda s: 77.0, None)
    rows = saved[0][0]
    assert rows == [{"symbol": "AAA", "atr_pct": 2.34, "atr_percentile_rank": 77.0, "price": 123.46,
                     "atr_period": screener.CONFIG.atr_period}]


def test_weekend_counts_as_outside_market_hours(monkeypatch, saved):
    at(monkeypatch, 2026, 11, 21, 11, 0)  # Saturday
    screener._persist_atr_snapshot("NIFTY1001", {"AAA": 0.02}, {"AAA": 100.0}, lambda s: 50.0, None)
    assert saved and saved[0][0][0]["atr_pct"] == 2.0


def test_db_failure_is_not_fatal(monkeypatch):
    at(monkeypatch, 2026, 11, 19, 20, 0)

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db_store, "upsert_atr_snapshot", boom)
    screener._persist_atr_snapshot("NIFTY1001", {"AAA": 0.02}, {"AAA": 100.0}, lambda s: 50.0, None)


def test_symbols_with_too_little_history_are_dropped_after_trimming(monkeypatch, saved):
    at(monkeypatch, 2026, 11, 19, 11, 0)
    history = {"AAA": make_history("2026-11-19", True), "TINY": make_history("2026-11-19", True, days=5)}
    screener._persist_atr_snapshot("NIFTY1001", {}, {}, lambda s: 1.0, history)
    assert [r["symbol"] for r in saved[0][0]] == ["AAA"]
