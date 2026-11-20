"""hv_config.py: defaults introduced for the live tab and the universe filter,
and config_from_dict coercion."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hv_config import HV_UNIVERSE_TIERS, RISK_FIELDS, TUNABLE_FIELDS, HVConfig, config_from_dict


def test_poll_default_is_10_seconds():
    assert HVConfig().poll_seconds == 10


def test_universe_tiers_are_nifty100_and_midcap150_batches():
    assert HV_UNIVERSE_TIERS == ("NIFTY1001", "NIFTY1002", "NIFTYMIDCAP1501", "NIFTYMIDCAP1502")


def test_universe_tiers_are_not_a_tunable_setting():
    assert "HV_UNIVERSE_TIERS" not in TUNABLE_FIELDS and "universe_tiers" not in TUNABLE_FIELDS


def test_config_from_dict_coerces_and_ignores_unknown_keys():
    cfg = config_from_dict({"poll_seconds": "15", "capital": "250000", "longs_only": 0,
                            "entry_end": 1430, "not_a_field": 1, "min_atr_pct": None})
    assert cfg.poll_seconds == 15 and isinstance(cfg.poll_seconds, int)
    assert cfg.capital == 250000.0
    assert cfg.longs_only is False
    assert cfg.entry_end == "1430"
    assert cfg.min_atr_pct == HVConfig().min_atr_pct  # None keeps the default


def test_target_pcts_round_trip_as_list():
    cfg = config_from_dict({"target_pcts": ["1", "2.5"]})
    assert cfg.target_pcts == (1.0, 2.5) and cfg.to_dict()["target_pcts"] == [1.0, 2.5]


def test_risk_fields_are_all_real_settings():
    assert set(RISK_FIELDS) <= set(TUNABLE_FIELDS)


def test_derived_risk_values():
    cfg = HVConfig(capital=200000, risk_pct=0.25, daily_loss_pct=2.0)
    assert cfg.one_r == 500 and cfg.daily_loss_limit == 4000
