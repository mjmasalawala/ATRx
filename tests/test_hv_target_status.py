"""Unit tests for hv_signals.target_status: did price reach the suggested
target the same day (hit), or not by the close (failed)? Pure logic."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hv_signals import target_status

D = datetime(2026, 11, 19)
SIG = {"signal_at": D.replace(hour=9, minute=40), "target": 105.0}
CLOSE = D.replace(hour=15, minute=30)


def bar(h, m, high):
    return {"ts": D.replace(hour=h, minute=m), "high": high, "low": high - 1, "open": high - 1, "close": high - 0.5}


def test_hit_when_a_bar_after_the_signal_reaches_target():
    r = target_status(SIG, [bar(9, 41, 104), bar(10, 5, 105.2)], None, D.replace(hour=10, minute=6), CLOSE)
    assert r == {"status": "hit", "at": D.replace(hour=10, minute=5)}


def test_bars_before_the_signal_do_not_count():
    r = target_status(SIG, [bar(9, 30, 106)], None, D.replace(hour=10), CLOSE)
    assert r is None


def test_live_price_counts_before_the_bar_closes():
    now = D.replace(hour=10, minute=0)
    assert target_status(SIG, [bar(9, 41, 104)], 105.0, now, CLOSE)["status"] == "hit"


def test_open_until_the_session_ends_then_failed():
    bars = [bar(9, 41, 104)]
    assert target_status(SIG, bars, 103, D.replace(hour=14, minute=59), CLOSE) is None
    assert target_status(SIG, bars, 103, CLOSE, CLOSE) == {"status": "failed"}


def test_multi_day_window_a_later_days_bar_counts():
    nxt = datetime(2026, 11, 20, 11, 0)
    later = {"ts": nxt, "high": 106.0, "low": 104.0, "open": 104.0, "close": 105.0}
    deadline = datetime(2026, 11, 24, 15, 30)
    assert target_status(SIG, [bar(9, 41, 104), later], None, nxt, deadline) == {"status": "hit", "at": nxt}


def test_multi_day_window_not_failed_before_the_deadline():
    assert target_status(SIG, [bar(9, 41, 104)], 103, datetime(2026, 11, 23, 15, 45), datetime(2026, 11, 24, 15, 30)) is None


def test_no_target_no_status():
    assert target_status({**SIG, "target": None}, [bar(10, 0, 200)], None, CLOSE, CLOSE) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
