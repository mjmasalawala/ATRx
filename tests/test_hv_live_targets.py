"""hv_live.py target-status plumbing (T+3 window): deadline, intraday hits,
and the end-of-day sweep. DB and Kite are replaced with in-memory fakes."""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hv_live


class FakeDB:
    def __init__(self, signals=None, holidays=()):
        self.signals = signals or []
        self.holidays = set(holidays)
        self.saved = []
        self.contexts = {}

    def is_nse_holiday(self, d):
        return d in self.holidays

    def list_hv_signals(self, from_date, to_date=None, source=None):
        return [s for s in self.signals if (source is None or s["source"] == source)]

    def save_hv_signal_target_status(self, updates):
        self.saved.extend(updates)

    def load_watch_context(self, d):
        return self.contexts


class NoPacer:
    def wait(self):
        pass


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(hv_live, "db_store", fake)
    monkeypatch.setattr(hv_live, "_Pacer", NoPacer)
    return fake


def signal(sid=1, trade_date="2026-11-18", at="2026-11-18T09:40:00+05:30", target=105.0, metrics=None,
           symbol="ABC", source="live"):
    return {"id": sid, "source": source, "trade_date": trade_date, "symbol": symbol, "stage": "entry",
            "signal_at": at, "target": target, "metrics": metrics or {}}


def bar(d, h, m, high):
    return {"ts": datetime(d.year, d.month, d.day, h, m), "high": high, "low": high - 1, "open": high - 1,
            "close": high - 0.5}


# ------------------------------------------------------------- deadline

def test_deadline_is_close_of_third_trading_session_after_the_signal_day(db):
    # Mon 16 Nov -> Tue, Wed, Thu
    assert hv_live.target_deadline(date(2026, 11, 16)) == datetime(2026, 11, 19, 15, 30)


def test_deadline_skips_weekends(db):
    # Thu 19 Nov -> Fri 20, Mon 23, Tue 24
    assert hv_live.target_deadline(date(2026, 11, 19)) == datetime(2026, 11, 24, 15, 30)


def test_deadline_skips_exchange_holidays(db):
    db.holidays = {date(2026, 11, 20)}
    # Wed 18 Nov -> Thu 19, (Fri 20 holiday), Mon 23, Tue 24
    assert hv_live.target_deadline(date(2026, 11, 18)) == datetime(2026, 11, 24, 15, 30)


def test_deadline_treats_days_as_trading_if_holiday_lookup_fails(monkeypatch):
    class Broken:
        def is_nse_holiday(self, d):
            raise RuntimeError("db down")
    monkeypatch.setattr(hv_live, "db_store", Broken())
    assert hv_live.target_deadline(date(2026, 11, 16)) == datetime(2026, 11, 19, 15, 30)


# ------------------------------------------------- intraday _resolve_targets

TODAY = date(2026, 11, 19)
NOW = datetime(2026, 11, 19, 11, 0)


def test_intraday_hit_is_recorded_immediately(db):
    db.signals = [signal(trade_date="2026-11-19", at="2026-11-19T09:40:00+05:30")]
    n = hv_live._resolve_targets(TODAY, NOW, {"ABC": [bar(TODAY, 10, 15, 105.5)]}, {})
    assert n == 1
    patch = db.saved[0]["patch"]
    assert db.saved[0]["id"] == 1 and patch["target_status"] == "hit"
    assert patch["target_hit_at"] == "2026-11-19T10:15:00"


def test_earlier_days_signal_inside_window_can_hit_today(db):
    db.signals = [signal(trade_date="2026-11-17", at="2026-11-17T09:40:00+05:30")]
    hv_live._resolve_targets(TODAY, NOW, {"ABC": [bar(TODAY, 10, 0, 106)]}, {})
    assert db.saved and db.saved[0]["patch"]["target_status"] == "hit"


def test_never_fails_intraday_even_when_the_deadline_has_passed(db):
    db.signals = [signal(trade_date="2026-11-10", at="2026-11-10T09:40:00+05:30")]
    assert hv_live._resolve_targets(TODAY, datetime(2026, 11, 19, 15, 45), {"ABC": [bar(TODAY, 10, 0, 90)]}, {}) == 0
    assert db.saved == []


def test_live_price_marks_hit_before_the_bar_closes(db):
    db.signals = [signal(trade_date="2026-11-19", at="2026-11-19T09:40:00+05:30")]
    hv_live._resolve_targets(TODAY, NOW, {"ABC": []}, {"ABC": 105.0})
    assert db.saved[0]["patch"]["target_status"] == "hit"


def test_symbol_without_candles_is_left_alone(db):
    db.signals = [signal(trade_date="2026-11-19", at="2026-11-19T09:40:00+05:30")]
    assert hv_live._resolve_targets(TODAY, NOW, {}, {"ABC": 999}) == 0


def test_resolved_replay_and_targetless_signals_are_skipped(db):
    db.signals = [signal(1, metrics={"target_status": "hit"}), signal(2, source="replay"), signal(3, target=None)]
    assert hv_live._resolve_targets(TODAY, NOW, {"ABC": [bar(TODAY, 10, 0, 500)]}, {}) == 0
    assert db.saved == []


# ----------------------------------------------------------- sweep_targets

def sweep_setup(db, monkeypatch, bars):
    db.contexts = {"ABC": {"instrument_token": 7}}
    monkeypatch.setattr(hv_live, "fetch_candles", lambda kite, token, start, end, interval: bars)


AFTER_CLOSE = datetime(2026, 11, 24, 15, 45)  # Tue 24 Nov: T+3 of Wed 18 Nov (Fri holiday)


def test_sweep_marks_failed_once_t_plus_3_has_closed(db, monkeypatch):
    db.holidays = {date(2026, 11, 20)}
    db.signals = [signal()]
    sweep_setup(db, monkeypatch, [bar(date(2026, 11, 18), 10, 0, 101)])
    assert hv_live.sweep_targets(object(), AFTER_CLOSE) == 1
    assert db.saved == [{"id": 1, "patch": {"target_status": "failed"}}]


def test_sweep_marks_hit_reached_on_a_later_day(db, monkeypatch):
    db.signals = [signal()]
    sweep_setup(db, monkeypatch, [bar(date(2026, 11, 18), 10, 0, 101), bar(date(2026, 11, 19), 10, 0, 105.2)])
    hv_live.sweep_targets(object(), datetime(2026, 11, 19, 15, 45))
    assert db.saved[0]["patch"]["target_status"] == "hit"
    assert db.saved[0]["patch"]["target_hit_at"] == "2026-11-19T10:00:00"


def test_sweep_keeps_signal_pending_inside_window_and_marks_checked(db, monkeypatch):
    db.signals = [signal()]
    sweep_setup(db, monkeypatch, [bar(date(2026, 11, 18), 10, 0, 101)])
    hv_live.sweep_targets(object(), datetime(2026, 11, 19, 15, 45))  # T+1 closed, deadline is T+3
    assert db.saved == [{"id": 1, "patch": {"target_checked": "2026-11-19"}}]


def test_sweep_skips_signals_already_checked_today(db, monkeypatch):
    db.signals = [signal(metrics={"target_checked": "2026-11-19"})]
    sweep_setup(db, monkeypatch, [])
    assert hv_live.sweep_targets(object(), datetime(2026, 11, 19, 15, 45)) == 0
    assert db.saved == []


def test_sweep_intraday_does_not_mark_checked(db, monkeypatch):
    db.signals = [signal(trade_date="2026-11-19", at="2026-11-19T09:40:00+05:30")]
    sweep_setup(db, monkeypatch, [bar(TODAY, 10, 0, 101)])
    hv_live.sweep_targets(object(), NOW)  # 11:00, market still open
    assert db.saved == []


def test_sweep_survives_a_failed_candle_fetch(db, monkeypatch):
    db.signals = [signal()]
    db.contexts = {"ABC": {"instrument_token": 7}}

    def boom(*a, **k):
        raise RuntimeError("kite down")
    monkeypatch.setattr(hv_live, "fetch_candles", boom)
    assert hv_live.sweep_targets(object(), AFTER_CLOSE) == 0
    assert db.saved == []  # nothing marked failed because data was missing


def test_sweep_skips_symbols_with_no_watch_context(db, monkeypatch):
    db.signals = [signal()]
    db.contexts = {}
    monkeypatch.setattr(hv_live, "fetch_candles", lambda *a, **k: pytest.fail("should not fetch"))
    assert hv_live.sweep_targets(object(), AFTER_CLOSE) == 0


# ------------------------------------------------------ rejected signals

def test_rejected_signals_are_never_resolved_intraday(db):
    db.signals = [signal(1, trade_date="2026-11-19", at="2026-11-19T09:40:00+05:30", target=360.25,
                         metrics={"suggestion_ok": False, "fails": ["net R:R -1.72 under 1.2"]})]
    db.signals[0]["entry"] = 361.8
    assert hv_live._resolve_targets(TODAY, NOW, {"ABC": [bar(TODAY, 10, 0, 500)]}, {"ABC": 500}) == 0
    assert db.saved == []


def test_target_at_or_below_entry_is_rejected_even_without_the_flag(db):
    db.signals = [signal(1, trade_date="2026-11-19", at="2026-11-19T09:40:00+05:30", target=360.25)]
    db.signals[0]["entry"] = 361.8
    assert hv_live._resolve_targets(TODAY, NOW, {"ABC": [bar(TODAY, 10, 0, 500)]}, {}) == 0


def test_rejected_signals_are_skipped_by_the_sweep(db, monkeypatch):
    db.signals = [signal(metrics={"suggestion_ok": False})]
    db.signals[0]["entry"] = 100.0
    sweep_setup(db, monkeypatch, [])
    monkeypatch.setattr(hv_live, "fetch_candles", lambda *a, **k: pytest.fail("should not fetch"))
    assert hv_live.sweep_targets(object(), AFTER_CLOSE) == 0
