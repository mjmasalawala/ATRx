"""
Unit tests for hv_ranking.py on synthetic 5-minute candles -- swing
counting, per-session statistics and the pre-open ranking/filters. No Kite,
no DB, no network.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hv_config import HVConfig
from hv_ranking import count_swings, intraday_stats, rank_candidates


def make_session(day: date, closes_pattern, start_price=100.0, volume=1000):
    """75 five-minute candles for `day`; closes follow closes_pattern
    (list of 75 multipliers on start_price)."""
    start = datetime(day.year, day.month, day.day, 9, 15)
    rows = []
    prev = start_price
    for i, mult in enumerate(closes_pattern):
        close = start_price * mult
        rows.append({"timestamp": start + timedelta(minutes=5 * i),
                     "open": prev, "high": max(prev, close) * 1.0005,
                     "low": min(prev, close) * 0.9995, "close": close, "volume": volume})
        prev = close
    return rows


def zigzag_pattern(amplitude):
    """Down `amplitude`, up, down, up across 75 candles."""
    pts = [1.0, 1 - amplitude, 1.0, 1 - amplitude, 1.0]
    out = []
    per = 75 // (len(pts) - 1)
    for a, b in zip(pts, pts[1:]):
        out += list(np.linspace(a, b, per, endpoint=False))
    out += [pts[-1]] * (75 - len(out))
    return out


def history(days, pattern, volumes=None):
    rows = []
    d = date(2026, 10, 1)
    count = 0
    while count < days:
        if d.weekday() < 5:
            vol = volumes[count] if volumes else 1000
            rows += make_session(d, pattern, volume=vol)
            count += 1
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def test_count_swings_counts_each_leg_over_threshold():
    prices = [100, 98, 100, 98, 100]       # 2% legs: down, up, down, up
    assert count_swings(prices, 1.5) == (2, 2)


def test_count_swings_ignores_moves_below_threshold():
    assert count_swings([100, 100.5, 100.1, 100.6, 100.2], 1.5) == (0, 0)


def test_count_swings_single_directional_trend_is_one_leg():
    assert count_swings([100, 101, 102, 103, 104], 1.5) == (1, 0)


def test_intraday_stats_excludes_today_and_requires_history():
    cfg = HVConfig()
    candles = history(10, zigzag_pattern(0.03))
    last_day = candles["timestamp"].dt.date.max()
    stats = intraday_stats(candles, cfg, today=last_day)
    assert stats["days"] == 9
    assert intraday_stats(history(3, zigzag_pattern(0.03)), cfg, today=date(2027, 1, 1)) is None


def test_intraday_stats_detects_whipsaw_and_swings():
    cfg = HVConfig()
    stats = intraday_stats(history(8, zigzag_pattern(0.03)), cfg, today=date(2027, 1, 1))
    assert stats["two_up_days_pct"] == 100.0 and stats["two_down_days_pct"] == 100.0
    assert stats["avg_whipsaw"] > 0.5
    assert stats["avg_fh_range_pct"] > 1.0


def test_intraday_stats_flat_stock_has_no_swings():
    cfg = HVConfig()
    stats = intraday_stats(history(8, [1.0] * 75), cfg, today=date(2027, 1, 1))
    assert stats["two_up_days_pct"] == 0.0 and stats["avg_fh_range_pct"] < 0.2


def test_volume_ratio_compares_last_session_to_prior_average():
    cfg = HVConfig()
    stats = intraday_stats(history(8, zigzag_pattern(0.03), volumes=[1000] * 7 + [3000]),
                           cfg, today=date(2027, 1, 1))
    assert stats["last_volume_ratio"] == pytest.approx(3.0, abs=0.01)


def _row(symbol, amplitude, price=100.0, upper_band=20.0, atr_pct=3.0, volume=10_000_000):
    stats = intraday_stats(history(8, zigzag_pattern(amplitude), volumes=[volume] * 8),
                           HVConfig(), today=date(2027, 1, 1))
    return {"symbol": symbol, "tier": "large_cap", "atr_pct": atr_pct, "stats": stats,
            "quote": {"last_price": price * 1.01, "prev_close": price,
                      "upper_circuit": price * (1 + upper_band / 100),
                      "lower_circuit": price * (1 - upper_band / 100)}}


def test_rank_prefers_the_more_volatile_whipsaw_stock():
    ranked, excluded = rank_candidates([_row("CALM", 0.005), _row("WILD", 0.04)], HVConfig())
    assert [r["symbol"] for r in ranked][0] == "WILD"
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["why"]


def test_filters_drop_low_atr_thin_and_tight_band_stocks():
    rows = [_row("LOWATR", 0.04, atr_pct=1.0),
            _row("THIN", 0.04, volume=10),
            _row("BAND5", 0.04, upper_band=5.0),
            _row("OK", 0.04)]
    ranked, excluded = rank_candidates(rows, HVConfig())
    assert [r["symbol"] for r in ranked] == ["OK"]
    reasons = {e["symbol"]: e["reason"] for e in excluded}
    assert "ATR%" in reasons["LOWATR"]
    assert "turnover" in reasons["THIN"]
    assert "band" in reasons["BAND5"]


def test_missing_history_is_excluded_not_crashing():
    ranked, excluded = rank_candidates([{"symbol": "NEW", "atr_pct": 3.0, "stats": None}], HVConfig())
    assert ranked == [] and excluded[0]["symbol"] == "NEW"


def test_manual_tickers_skip_the_filters():
    rows = [_row("LOWATR", 0.04, atr_pct=1.0), _row("THIN", 0.04, volume=10),
            _row("BAND5", 0.04, upper_band=5.0)]
    for r in rows:
        r["manual"] = True
    ranked, excluded = rank_candidates(rows, HVConfig())
    assert {r["symbol"] for r in ranked} == {"LOWATR", "THIN", "BAND5"} and not excluded
