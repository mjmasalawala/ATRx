"""high_volatility.html: every setting on the Settings tab has a tooltip (and
no tooltip is orphaned), and the 10-second poll floor/default are in place.
Parses the page source, so it needs no browser."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hv_config import TUNABLE_FIELDS

HTML = (Path(__file__).resolve().parent.parent / "high_volatility.html").read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = HTML.index(start_marker)
    return HTML[start:HTML.index(end_marker, start)]


def _setting_def_keys() -> list[str]:
    block = _block("const SETTING_DEFS = [", "\n];\nfunction inSession")
    return re.findall(r'\["([a-z_0-9]+)", "[^"]+", ', block)


def _tip_keys() -> list[str]:
    block = _block("const SETTING_TIPS = {", "\n};\nconst SETTING_DEFS")
    return re.findall(r'^  ([a-z_0-9]+): \[', block, re.M)


def test_every_setting_has_a_tooltip_and_none_are_orphaned():
    defs, tips = _setting_def_keys(), _tip_keys()
    assert len(defs) > 40
    assert set(defs) == set(tips)
    assert len(tips) == len(set(tips))  # no duplicate entries


def test_every_setting_key_is_a_real_config_field():
    assert set(_setting_def_keys()) <= set(TUNABLE_FIELDS)


def test_each_tooltip_has_use_and_effect_text():
    block = _block("const SETTING_TIPS = {", "\n};\nconst SETTING_DEFS")
    for key, use, effect in re.findall(r'^  ([a-z_0-9]+): \["((?:[^"\\]|\\.)+)", "((?:[^"\\]|\\.)+)"\],?$', block, re.M):
        assert use.strip() and effect.strip(), key


def test_poll_floor_and_footer_default_are_10_seconds():
    assert "Math.max(10, SETTINGS.poll_seconds)" in HTML
    assert '<span id="pollSecs">10</span>' in HTML


def test_page_has_the_rejected_signal_mirror():
    assert "function isRejectedSignal(x)" in HTML
    assert "suggestion_ok === false" in HTML and "x.target <= x.entry" in HTML
    assert ">Rejected</span>" in HTML
