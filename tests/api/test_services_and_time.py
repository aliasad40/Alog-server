"""Tests for the v1.1.0 additions.

The two things worth testing hard here are the service-control allow-list
(a privilege boundary) and the timezone maths (a correctness boundary --
a lawful-intercept response citing the wrong hour is a serious error).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone as dt_tz
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from app import timezones as tzutil
from app.services import control


# ============================ service control ============================

def test_unknown_unit_is_refused_before_anything_runs():
    for evil in ["sshd", "../../bin/sh", "redis-server; rm -rf /",
                 "network-log-server-api evil", "", "*"]:
        with pytest.raises(ValueError, match="Unknown service"):
            control.control(evil, "restart")


def test_unknown_action_is_refused():
    for evil in ["daemon-reload", "enable", "mask", "restart; rm -rf /", "cat"]:
        with pytest.raises(ValueError, match="Unsupported action"):
            control.control("worker", evil)


def test_allow_list_contents_are_exactly_what_we_expect():
    """If someone adds a unit here, it should be a deliberate decision that
    shows up in review -- not a silent widening of what the web app can do."""
    assert set(control.UNITS) == {
        "receiver", "worker", "api", "redis", "clickhouse", "nginx"}
    assert set(control.ACTIONS) == {"restart", "start", "stop"}


def test_no_unit_name_can_inject_shell_metacharacters():
    for unit in control.UNITS.values():
        assert not any(ch in unit.unit for ch in ";|&$`><\n \t'\"\\")


def test_sudoers_lines_have_no_wildcards():
    """A wildcard in a sudoers rule would let the netlog user run systemctl
    against arbitrary units, which is the whole thing this design avoids."""
    lines = control.sudoers_lines("netlog")
    rules = [ln for ln in lines if not ln.startswith("#")]
    assert rules, "no sudoers rules generated"
    for line in rules:
        assert "*" not in line, line
        assert "ALL=(root) NOPASSWD:" in line
        assert line.startswith("netlog ALL=(root)")


def test_sudoers_covers_every_allowed_combination_and_nothing_more():
    rules = [ln for ln in control.sudoers_lines() if not ln.startswith("#")]
    expected = {(u.unit, a) for u in control.UNITS.values() for a in control.ACTIONS}
    assert len(rules) == len(expected)
    for unit, action in expected:
        assert any(line.endswith(f"--no-block {action} {unit}") for line in rules), \
            f"missing sudoers rule for {action} {unit}"


def test_sudoers_command_matches_what_control_actually_runs(monkeypatch):
    """The sudoers file and the executed argv must agree exactly, or sudo
    refuses every request. Generating both from one place is only useful if
    they genuinely stay in step."""
    captured = {}

    def fake_run(args, timeout=20):
        captured["args"] = args
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(control, "_run", fake_run)
    monkeypatch.setattr(control.time, "sleep", lambda s: None)
    monkeypatch.setattr(control, "status", lambda key: None)

    control.control("worker", "restart")
    argv = captured["args"]
    # sudo -n <systemctl> --no-block restart <unit>
    assert argv[0] == control.SUDO and argv[1] == "-n"
    executed = " ".join(argv[2:])
    rules = [ln for ln in control.sudoers_lines() if not ln.startswith("#")]
    assert any(line.endswith(executed) for line in rules), \
        f"executed {executed!r} is not covered by any sudoers rule"


def test_control_never_uses_a_shell():
    """Checked against the parsed syntax tree, not the text -- the module's own
    docstring mentions shell=True to explain why it is absent, and a grep would
    match that."""
    import ast

    source = open(os.path.join(os.path.dirname(control.__file__), "control.py")).read()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                assert kw.arg != "shell" or not getattr(kw.value, "value", False), \
                    "shell=True found in control.py"
            target = getattr(node.func, "attr", None)
            assert target not in ("system", "popen"), f"os.{target} found in control.py"


def test_status_of_unknown_unit_is_none():
    assert control.status("nope") is None


def test_every_unit_documents_its_restart_impact():
    """The UI shows this before the operator commits. An empty one would mean
    someone restarts ClickHouse mid-investigation without being warned."""
    for unit in control.UNITS.values():
        assert len(unit.impact) > 30, unit.key
        assert len(unit.description) > 20, unit.key


# ================================ timezones ==============================

KARACHI = ZoneInfo("Asia/Karachi")      # UTC+5, no daylight saving
LONDON = ZoneInfo("Europe/London")      # UTC+0/+1, has daylight saving


def test_resolve_falls_back_to_utc_rather_than_raising():
    """A bad value in the settings table must not take the search page down."""
    for bad in ["Not/AZone", "", None, "UTC+5", "PKT"]:
        assert tzutil.resolve(bad) == ZoneInfo("UTC")


def test_resolve_accepts_real_iana_names():
    assert tzutil.resolve("Asia/Karachi") == KARACHI


def test_is_valid_rejects_what_resolve_would_swallow():
    assert tzutil.is_valid("Asia/Karachi")
    assert not tzutil.is_valid("UTC+5")
    assert not tzutil.is_valid("Not/AZone")


def test_operator_input_is_interpreted_on_their_own_clock():
    """Typing 18:00 in Karachi must search 13:00 UTC, not 18:00 UTC.
    Getting this backwards silently returns the wrong five hours of data."""
    typed = datetime(2026, 8, 30, 18, 0, 0)          # naive, as the browser sends
    as_utc = tzutil.to_utc(typed, KARACHI)
    assert as_utc == datetime(2026, 8, 30, 13, 0, 0, tzinfo=dt_tz.utc)


def test_already_aware_input_is_not_shifted_twice():
    aware = datetime(2026, 8, 30, 13, 0, 0, tzinfo=dt_tz.utc)
    assert tzutil.to_utc(aware, KARACHI) == aware


def test_display_renders_on_the_operator_clock_with_no_offset_suffix():
    stored = datetime(2026, 8, 30, 13, 33, 17, tzinfo=dt_tz.utc)
    assert tzutil.format_display(stored, KARACHI) == "2026-08-30 18:33:17"
    assert "+" not in tzutil.format_display(stored, KARACHI)


def test_naive_stored_value_is_assumed_utc():
    stored = datetime(2026, 8, 30, 13, 33, 17)       # legacy row, no tzinfo
    assert tzutil.format_display(stored, KARACHI) == "2026-08-30 18:33:17"


def test_none_stays_none():
    assert tzutil.format_display(None, KARACHI) is None


def test_round_trip_input_to_storage_to_display_is_lossless():
    """What the operator types must be what they see back."""
    for tz in (KARACHI, LONDON, ZoneInfo("America/New_York")):
        typed = datetime(2026, 8, 30, 18, 33, 17)
        stored = tzutil.to_utc(typed, tz)
        assert tzutil.format_display(stored, tz) == "2026-08-30 18:33:17"


def test_daylight_saving_is_handled_not_hardcoded():
    """A fixed offset would be an hour wrong for half the year, which is why
    only IANA names are accepted."""
    winter = datetime(2026, 1, 15, 12, 0)
    summer = datetime(2026, 7, 15, 12, 0)
    assert tzutil.to_utc(winter, LONDON).hour == 12   # GMT
    assert tzutil.to_utc(summer, LONDON).hour == 11   # BST, UTC+1


def test_offset_label_reflects_the_season():
    assert tzutil.offset_label(KARACHI) == "UTC+05:00"
    assert tzutil.offset_label(ZoneInfo("UTC")) == "UTC+00:00"
    winter = datetime(2026, 1, 15, 12, tzinfo=dt_tz.utc)
    summer = datetime(2026, 7, 15, 12, tzinfo=dt_tz.utc)
    assert tzutil.offset_label(LONDON, winter) == "UTC+00:00"
    assert tzutil.offset_label(LONDON, summer) == "UTC+01:00"


def test_negative_offsets_format_correctly():
    label = tzutil.offset_label(ZoneInfo("America/New_York"),
                                datetime(2026, 1, 15, 12, tzinfo=dt_tz.utc))
    assert label == "UTC-05:00"


def test_half_hour_offsets_format_correctly():
    label = tzutil.offset_label(ZoneInfo("Asia/Kolkata"))
    assert label == "UTC+05:30"


def test_common_timezones_are_all_real():
    for name in tzutil.COMMON_TIMEZONES:
        assert tzutil.is_valid(name), name
