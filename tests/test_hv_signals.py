"""
Unit tests for hv_signals.py -- the live tab's setup detector, position
prompts, signal scoring and replay -- on synthetic candles. Pure logic, no
DB or Kite.
"""

import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hv_config import HVConfig
from hv_signals import (build_context, evaluate, manage_position, nifty_state, replay_day,
                        resample, rsi, score_signal, vwap_bands)

TODAY = date(2026, 10, 22)
V = 100_000  # normal volume of one 5-minute bar


def _history():
    """12 sessions oscillating 411.5-428.5 (daily ATR ~17), V per 5-min bar."""
    out, d = [], TODAY - timedelta(days=16)
    while d < TODAY:
        if d.weekday() < 5:
            t0 = datetime.combine(d, datetime.min.time()).replace(hour=9, minute=15)
            for i in range(75):
                mid = 420 + 8 * math.sin(2 * math.pi * i / 75)
                out.append({"ts": t0 + timedelta(minutes=5 * i), "open": mid, "high": mid + 0.5,
                            "low": mid - 0.5, "close": mid, "volume": V})
        d += timedelta(days=1)
    return out


def _bar(hm, o, h, l, c, v, day=TODAY):
    hh, mm = map(int, hm.split(":"))
    return {"ts": datetime(day.year, day.month, day.day, hh, mm), "open": o, "high": h, "low": l,
            "close": c, "volume": v}


def _dip_day(day=TODAY):
    """Range open around 425, a heavy-volume flush to the previous day's
    low, a quieter bar with a long lower wick, then a close above the prior
    bar's high at 9:39."""
    t = datetime(day.year, day.month, day.day, 9, 15)
    bars = []
    for i in range(15):
        mid = 425 + (0.6 if i % 4 < 2 else -0.6)
        bars.append({"ts": t + timedelta(minutes=i), "open": mid, "high": mid + 0.4,
                     "low": mid - 0.4, "close": mid, "volume": V // 5})
    bars += [_bar("09:30", 424.6, 424.8, 421.0, 421.2, 3 * V // 5, day),
             _bar("09:31", 421.2, 421.4, 417.0, 417.3, 3 * V // 5, day),
             _bar("09:32", 417.3, 417.5, 414.0, 414.6, 3 * V // 5, day),
             _bar("09:33", 414.6, 415.0, 413.2, 413.6, 3 * V // 5, day),
             _bar("09:34", 413.6, 414.2, 413.0, 414.0, 3 * V // 5, day),
             _bar("09:35", 414.0, 414.2, 412.6, 412.8, V // 10, day),
             _bar("09:36", 412.8, 413.0, 412.1, 412.5, V // 10, day),
             _bar("09:37", 412.5, 413.6, 412.4, 413.4, V // 10, day),
             _bar("09:38", 413.4, 413.6, 412.8, 413.0, V // 10, day),
             _bar("09:39", 413.0, 415.0, 412.9, 414.8, V // 10, day)]
    return bars


CTX = build_context(_history(), TODAY)


def test_context_from_history():
    assert CTX["sessions"] == 12
    assert 16 < CTX["atr"] < 18
    assert CTX["prev_low"] < 412 and CTX["prev_high"] > 428
    assert CTX["vol_baseline"]["09:30"] == V
    assert len(CTX["prev_closes_5m"]) == 10


def test_entry_zone_with_sized_suggestion():
    ev = evaluate(_dip_day(), CTX, None, None, HVConfig(), datetime(2026, 10, 22, 9, 40, 5))
    assert ev["stage"] == "entry"
    assert ev["day_type"]["state"] == "Range"
    assert set(ev["metrics"]["exhaustion"]) >= {"volume", "wick", "level"}
    assert ev["metrics"]["confirmation"] == "close above prior high"
    s = ev["suggestion"]
    assert s["ok"] and s["entry"] == 414.8 and s["stop"] < 412.1 and s["target"] > 420
    # 1R = 0.25% of 2,00,000 = Rs 500; the loss per share includes 0.2% costs
    assert s["binding_cap"] == "risk"
    assert s["qty"] == math.floor(500 / s["risk_per_share"])
    assert s["net_rr"] >= 1.2


def test_only_stretched_until_the_5min_bars_close():
    # At 9:38 the 9:35 five-minute bar (wick, quieter volume) hasn't closed yet.
    ev = evaluate(_dip_day()[:-2], CTX, None, None, HVConfig(), datetime(2026, 10, 22, 9, 38, 5))
    assert ev["stage"] == "stretched"
    assert ev["metrics"]["exhaustion"] == ["level"]


def test_entry_lapses_after_two_bars():
    bars = _dip_day() + [_bar("09:40", 414.8, 414.9, 414.3, 414.5, V // 10),
                         _bar("09:41", 414.5, 414.6, 414.2, 414.4, V // 10)]
    ev = evaluate(bars, CTX, None, None, HVConfig(), datetime(2026, 10, 22, 9, 42, 5))
    assert ev["stage"] == "armed"
    assert ev["metrics"]["confirm_bars_ago"] >= 2


def test_no_entries_before_920_or_after_1430():
    cfg = HVConfig()
    early = evaluate(_dip_day()[:4], CTX, None, None, cfg, datetime(2026, 10, 22, 9, 19, 5))
    assert early["stage"] == "watching" and "09:20" in early["reasons"][0]
    late = evaluate(_dip_day(), CTX, None, None, cfg, datetime(2026, 10, 22, 14, 31))
    assert late["stage"] == "closed"


def test_nifty_trend_down_blocks_the_dip_buy():
    ev = evaluate(_dip_day(), CTX, None, {"state": "Trend down"}, HVConfig(), datetime(2026, 10, 22, 9, 40, 5))
    assert ev["stage"] == "blocked"
    assert "Nifty trending down" in ev["reasons"]


def test_opening_range_break_that_holds_is_a_trend_day():
    t = datetime(2026, 10, 22, 9, 15)
    bars = [{"ts": t + timedelta(minutes=i), "open": 425, "high": 425.4, "low": 424.6, "close": 425,
             "volume": V // 5} for i in range(5)]
    price = 424.0
    for i in range(5, 40):  # steady slide on heavy volume, never back above the range
        bars.append({"ts": t + timedelta(minutes=i), "open": price, "high": price + 0.2,
                     "low": price - 0.6, "close": price - 0.4, "volume": V})
        price -= 0.4
    ev = evaluate(bars, CTX, None, None, HVConfig(), datetime(2026, 10, 22, 9, 55, 5))
    assert ev["day_type"]["state"] == "Trend down"
    assert ev["stage"] == "blocked"


def test_spread_and_circuit_checks_fail_the_suggestion():
    quote = {"last_price": 414.8, "lower_circuit": 404.0, "upper_circuit": 460.0, "prev_close": 419.3,
             "depth": {"buy": [{"price": 413.5}], "sell": [{"price": 415.5}]}}
    ev = evaluate(_dip_day(), CTX, quote, None, HVConfig(), datetime(2026, 10, 22, 9, 40, 5))
    s = ev["suggestion"]
    assert not s["ok"]
    assert any("spread" in f for f in s["fails"])
    assert any("lower circuit" in f for f in s["fails"])


def test_narrow_price_band_is_never_traded():
    quote = {"last_price": 414.8, "lower_circuit": 398.4, "upper_circuit": 440.3, "prev_close": 419.3}
    ev = evaluate(_dip_day(), CTX, quote, None, HVConfig(), datetime(2026, 10, 22, 9, 40, 5))
    assert ev["stage"] == "blocked"
    assert any("price band" in r for r in ev["reasons"])


def test_nifty_state():
    t = datetime(2026, 10, 22, 9, 15)
    falling = [{"ts": t + timedelta(minutes=i), "open": 25000 - i * 5, "high": 25002 - i * 5,
                "low": 24996 - i * 5, "close": 24998 - i * 5, "volume": 0} for i in range(20)]
    assert nifty_state(falling, t + timedelta(minutes=20))["state"] == "Trend down"
    flat = [{"ts": t + timedelta(minutes=i), "open": 25000, "high": 25004 if i == 3 else 25002,
             "low": 24996 if i == 4 else 24998, "close": 25000, "volume": 0} for i in range(20)]
    assert nifty_state(flat, t + timedelta(minutes=20))["state"] == "Range"


def test_rsi_vwap_and_resample_basics():
    assert rsi([1, 2, 3, 4, 5, 6, 7], 5) == 100.0
    assert rsi([1, 2, 3], 5) is None
    bars = _dip_day()[:10]
    vw, sigma = vwap_bands([{**b, "high": 10, "low": 10, "close": 10} for b in bars])[-1]
    assert vw == 10 and sigma == 0
    five = resample(_dip_day())
    assert [b["ts"].strftime("%H:%M") for b in five] == ["09:15", "09:20", "09:25", "09:30", "09:35"]
    assert five[3]["volume"] == 15 * V // 5 and five[3]["low"] == 413.0


def _sig():
    return {"signal_at": datetime(2026, 10, 22, 9, 40), "entry": 414.8, "stop": 411.85, "target": 420.3}


def _after(prices_lh):
    t = datetime(2026, 10, 22, 9, 40)
    return [{"ts": t + timedelta(minutes=i), "open": (l + h) / 2, "high": h, "low": l,
             "close": (l + h) / 2, "volume": V // 10} for i, (l, h) in enumerate(prices_lh)]


def test_score_target_stop_and_not_filled():
    cfg = HVConfig()
    hit = score_signal(_sig(), _after([(414.5, 415.5), (415.0, 418.0), (417.5, 420.5)]), cfg)
    assert hit["outcome"] == "target" and hit["outcome_r"] > 1
    stop = score_signal(_sig(), _after([(414.5, 415.0), (411.0, 414.0)]), cfg)
    assert stop["outcome"] == "stop" and stop["outcome_r"] == -1.0
    both = score_signal(_sig(), _after([(414.5, 415.0), (411.0, 421.0)]), cfg)
    assert both["outcome"] == "stop"  # same bar: assume the worse
    missed = score_signal(_sig(), _after([(415.0, 416.0), (415.5, 417.0), (414.0, 421.0)]), cfg)
    assert missed["outcome"] == "not_filled"


def test_score_timeout_waits_for_the_full_window():
    cfg = HVConfig()
    drift = [(414.5, 415.5)] * 10
    assert score_signal(_sig(), _after(drift), cfg)["outcome"] is None
    full = score_signal(_sig(), _after([(414.5, 415.5)] * 31), cfg)
    assert full["outcome"] == "timeout"
    assert -0.5 < full["outcome_r"] < 0.5


def test_replay_finds_the_setup_once():
    day = _dip_day() + [_bar(f"09:{m}", 415 + i * 0.3, 415.4 + i * 0.3, 414.8 + i * 0.3, 415.2 + i * 0.3, V // 10)
                        for i, m in enumerate(range(40, 60))]
    signals = replay_day(day, CTX, [], HVConfig())
    assert len(signals) == 1
    assert signals[0]["episode"] == "09:36"
    assert signals[0]["outcome"] in ("target", "timeout")


def test_manage_position_prompts():
    cfg = HVConfig()
    bars = _dip_day() + [_bar("09:40", 414.8, 420.1, 414.7, 420.0, V // 10)]
    now = datetime(2026, 10, 22, 9, 41, 5)
    ev = evaluate(bars, CTX, None, None, cfg, now)
    pos = {"avg_entry": 414.8, "open_qty": 132, "entry_qty": 132, "entry_fills": 1, "planned_stop": 411.85,
           "risk_amount": 499, "opened_at": datetime(2026, 10, 22, 9, 40)}
    out = manage_position(pos, bars, ev, None, cfg, now)
    assert out["r_now"] > 1
    assert any(p["kind"] == "move_stop" for p in out["prompts"])

    late = manage_position(pos, bars, ev, None, cfg, datetime(2026, 10, 22, 10, 12))
    assert not any("Time stop" in w for w in late["warnings"]) and "time_stop_left" not in late
    flat = manage_position(pos, bars, ev, None, cfg, datetime(2026, 10, 22, 15, 0))
    assert any("exit everything" in w for w in flat["warnings"])


def test_signal_summary_groups_by_action():
    from hv_signals import signal_summary
    rows = [{"stage": "entry", "source": "live", "action": "taken", "outcome": "target", "outcome_r": 1.5,
             "metrics": {"confirmation": "higher low"}, "day_type": "Range"},
            {"stage": "entry", "source": "live", "action": "lapsed", "outcome": "stop", "outcome_r": -1.0,
             "metrics": {"confirmation": "higher low"}, "day_type": "Range"},
            {"stage": "entry", "source": "replay", "outcome": "not_filled", "outcome_r": None, "metrics": {}},
            {"stage": "armed", "source": "live", "outcome": "confirmed"},
            {"stage": "armed", "source": "live", "outcome": "no_confirm"}]
    s = signal_summary(rows)
    assert s["by_action"]["taken"]["hit_rate"] == 100.0
    assert s["by_action"]["lapsed"]["avg_r"] == -1.0
    assert s["by_action"]["replay"]["not_filled"] == 1 and s["by_action"]["replay"]["hit_rate"] is None
    assert s["by_confirmation"]["higher low"]["scored"] == 2
    assert s["armed"]["confirm_rate"] == 50.0


def _spike_day(final_price):
    """Opens flat at 425 (VWAP ~425), one thin-volume spike to 440, then sits
    at final_price: 0.4+ daily ATRs under the session high either way."""
    t = datetime(2026, 10, 22, 9, 15)
    bars = [{"ts": t + timedelta(minutes=i), "open": 425, "high": 425.4, "low": 424.6, "close": 425,
             "volume": V // 5} for i in range(15)]
    bars.append(_bar("09:30", 425, 440, 425, final_price, 10))
    for i in range(1, 12):
        bars.append({"ts": datetime(2026, 10, 22, 9, 30) + timedelta(minutes=i), "open": final_price,
                     "high": final_price + 0.2, "low": final_price - 0.2, "close": final_price, "volume": 10})
    return bars


def test_off_the_high_alone_is_not_stretched_while_price_is_above_vwap():
    ev = evaluate(_spike_day(432), CTX, None, None, HVConfig(), datetime(2026, 10, 22, 9, 42, 5))
    assert ev["metrics"]["off_high_atr"] >= HVConfig().stretch_atr   # the ATR test alone would pass
    assert ev["metrics"]["stretch_sigma"] > 0                         # but price is above VWAP
    assert ev["stage"] == "watching"


def test_off_the_high_counts_once_price_is_below_vwap():
    ev = evaluate(_spike_day(418), CTX, None, None, HVConfig(), datetime(2026, 10, 22, 9, 42, 5))
    assert ev["metrics"]["off_high_atr"] >= HVConfig().stretch_atr
    assert ev["metrics"]["stretch_sigma"] < 0
    assert ev["stage"] != "watching"


def test_is_rejected_flags_failed_suggestions_and_targets_not_above_entry():
    from hv_signals import is_rejected
    assert is_rejected({"stage": "entry", "metrics": {"suggestion_ok": False}, "entry": 100, "target": 105})
    assert is_rejected({"stage": "entry", "metrics": {}, "entry": 361.8, "target": 360.25})
    assert is_rejected({"stage": "entry", "metrics": None, "entry": 100, "target": 100})
    assert not is_rejected({"stage": "entry", "metrics": {"suggestion_ok": True}, "entry": 100, "target": 105})
    assert not is_rejected({"stage": "entry", "metrics": {}, "entry": None, "target": None})  # replay row, no data
    assert not is_rejected({"stage": "armed", "metrics": {"suggestion_ok": False}, "entry": 100, "target": 90})


def test_signal_summary_leaves_rejected_entries_out_and_counts_them():
    from hv_signals import signal_summary
    ok = {"stage": "entry", "source": "live", "action": "taken", "outcome": "target", "outcome_r": 1.0,
          "metrics": {"suggestion_ok": True}, "entry": 100, "target": 105, "day_type": "Range"}
    bad = {"stage": "entry", "source": "live", "action": "lapsed", "outcome": "target", "outcome_r": 9.0,
           "metrics": {"suggestion_ok": False}, "entry": 100, "target": 99, "day_type": "Range"}
    s = signal_summary([ok, bad])
    assert s["rejected"] == 1
    assert s["by_action"]["taken"]["signals"] == 1 and "lapsed" not in s["by_action"]
    assert s["by_day_type"]["Range"]["signals"] == 1


def test_repeat_reason_blocks_a_second_entry_within_one_atr():
    from hv_signals import repeat_reason
    cfg = HVConfig()  # reentry_atr = 1.0
    prior = [{"entry": 590.95, "episode": "09:50", "time": "09:57"}]
    # TATAINVEST 17 Nov: second entry 590.05, same day, daily ATR ~ 15
    assert "same setup" in repeat_reason(prior, "09:58", 590.05, 15.0, cfg)
    assert repeat_reason(prior, "09:58", 590.05 - 16, 15.0, cfg) is None   # a full ATR lower: a new setup
    assert repeat_reason(prior, "09:58", 590.95 + 16, 15.0, cfg) is None   # or a full ATR higher


def test_repeat_reason_ignores_the_signals_own_episode_and_missing_data():
    from hv_signals import repeat_reason
    cfg = HVConfig()
    prior = [{"entry": 590.95, "episode": "09:50"}]
    assert repeat_reason(prior, "09:50", 590.95, 15.0, cfg) is None        # the same signal re-shown
    assert repeat_reason([], "09:58", 590.0, 15.0, cfg) is None
    assert repeat_reason(prior, "09:58", None, 15.0, cfg) is None
    assert repeat_reason(prior, "09:58", 590.0, 0.0, cfg) is None
    assert repeat_reason(prior, "09:58", 590.0, 15.0, HVConfig(reentry_atr=0)) is None


def test_repeat_reason_checks_every_earlier_entry():
    from hv_signals import repeat_reason
    prior = [{"entry": 500.0, "episode": "a"}, {"entry": 560.0, "episode": "b"}]
    assert repeat_reason(prior, "c", 565.0, 15.0, HVConfig()) is not None
    assert repeat_reason(prior, "c", 530.0, 15.0, HVConfig()) is None     # 30 from both earlier entries


def test_no_time_stop_exit_prompt_however_long_the_trade_has_been_open():
    cfg = HVConfig()
    bars = _dip_day()
    ev = evaluate(bars, CTX, None, None, cfg, datetime(2026, 10, 22, 9, 40, 5))
    pos = {"avg_entry": 414.8, "open_qty": 132, "entry_qty": 132, "entry_fills": 1, "planned_stop": 411.85,
           "risk_amount": 499, "opened_at": datetime(2026, 10, 22, 9, 40)}
    out = manage_position(pos, bars, ev, None, cfg, datetime(2026, 10, 22, 11, 30))  # 110 minutes later, nowhere near +0.5R
    assert not any("Time stop" in w for w in out["warnings"])
    assert "time_stop_left" not in out
