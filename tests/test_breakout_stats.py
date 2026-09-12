import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from breakout_stats import horizon_summary, summarize


def make_event(fwd_10d, ctrl_10d):
    return {"fwd_ret_10d": fwd_10d, "control_ret_10d": ctrl_10d}


def test_horizon_summary_empty():
    result = horizon_summary([], 10)
    assert result["n"] == 0
    assert result["hit_rate_pct"] is None


def test_horizon_summary_basic_arithmetic():
    events = [make_event(5.0, 1.0), make_event(-2.0, 0.0), make_event(10.0, 2.0)]
    result = horizon_summary(events, 10)
    assert result["n"] == 3
    assert result["hit_rate_pct"] == round(2 / 3 * 100, 1)
    assert result["avg_fwd_ret_pct"] == round((5.0 - 2.0 + 10.0) / 3, 2)
    assert result["avg_excess_ret_pct"] == round(((5 - 1) + (-2 - 0) + (10 - 2)) / 3, 2)


def test_horizon_summary_ignores_none_forward_returns():
    events = [make_event(5.0, 1.0), {"fwd_ret_10d": None, "control_ret_10d": None}]
    result = horizon_summary(events, 10)
    assert result["n"] == 1


def test_summarize_insufficient_data_verdict():
    events = [make_event(5.0, 1.0)] * 5
    summary = summarize(events, horizons=(10,))
    assert summary["verdict_tag"] == "insufficient_data"


def test_summarize_edge_verdict_when_consistently_positive_excess():
    # Every event has the same +4% excess return -> CI collapses tightly
    # around +4%, well above zero.
    events = [make_event(6.0, 2.0) for _ in range(40)]
    summary = summarize(events, horizons=(10,))
    assert summary["verdict_tag"] == "edge"
    assert summary["by_horizon"][10]["avg_excess_ret_pct"] == 4.0


def test_summarize_no_edge_when_consistently_negative_excess():
    events = [make_event(1.0, 5.0) for _ in range(40)]
    summary = summarize(events, horizons=(10,))
    assert summary["verdict_tag"] == "no_edge"
