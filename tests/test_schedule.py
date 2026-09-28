"""Peak-hour and deferred-window classification."""

from __future__ import annotations

import datetime as _dt

from spendrouter.schedule import evaluate, parse_window

from tests.conftest import IN_WINDOW_OFF_PEAK, OUTSIDE_WINDOW_OFF_PEAK, PEAK_OUTSIDE_WINDOW


def test_parse_window_to_minutes():
    assert parse_window("00:00-06:00") == (0, 360)
    assert parse_window("22:30-23:59") == (1350, 1439)


def test_peak_hour_detected(config):
    project = config.project("work")
    peak = evaluate(project, PEAK_OUTSIDE_WINDOW)
    assert peak.is_peak
    assert peak.quota_multiplier == 4.0
    assert not peak.in_deferred_window


def test_deferred_window_detected_and_discounted(config):
    project = config.project("work")
    state = evaluate(project, IN_WINDOW_OFF_PEAK)
    assert state.in_deferred_window
    assert state.deferred_discount == 0.5
    assert not state.is_peak


def test_outside_every_window_means_deferred_is_unavailable(config):
    project = config.project("work")
    state = evaluate(project, OUTSIDE_WINDOW_OFF_PEAK)
    assert not state.in_deferred_window
    assert state.deferred_discount == 0.0
    assert "deferred spend unavailable" in state.note


def test_window_wrapping_past_midnight(config, tmp_path):
    text = (tmp_path / "wrap.toml")
    text.write_text(
        """
[models.m]
payg = { usd_per_mtok = 1.0 }

[projects.p]
deferred_windows = ["22:00-02:00"]
deferred_multiplier = 0.25
"""
    )
    from spendrouter.config import load_config

    cfg = load_config(str(text))
    project = cfg.project("p")
    # 23:30 is inside a window that wraps to 02:00.
    late = evaluate(project, _dt.datetime(2026, 9, 28, 23, 30))
    # 01:00 is also inside it.
    early = evaluate(project, _dt.datetime(2026, 9, 28, 1, 0))
    # 03:00 is outside.
    after = evaluate(project, _dt.datetime(2026, 9, 28, 3, 0))
    assert late.in_deferred_window
    assert early.in_deferred_window
    assert not after.in_deferred_window


def test_project_without_windows_treats_deferred_as_always_open(config_path, tmp_path):
    from spendrouter.config import load_config

    base = (tmp_path / "spendrouter.toml").read_text()
    path = tmp_path / "nowin.toml"
    path.write_text(base.replace('deferred_windows = ["00:00-06:00"]', "# no windows declared"))
    cfg = load_config(str(path))
    project = cfg.project("work")
    state = evaluate(project, OUTSIDE_WINDOW_OFF_PEAK)
    assert state.in_deferred_window
    assert state.deferred_discount == 0.5


def test_peak_inside_a_window_keeps_both_effects(config, tmp_path):
    from spendrouter.config import load_config

    base = (tmp_path / "spendrouter.toml").read_text()
    path = tmp_path / "both.toml"
    path.write_text(base.replace('deferred_windows = ["00:00-06:00"]', 'deferred_windows = ["09:00-10:00"]'))
    cfg = load_config(str(path))
    state = evaluate(cfg.project("work"), _dt.datetime(2026, 9, 28, 9, 15))
    assert state.is_peak
    assert state.in_deferred_window
    assert state.quota_multiplier == 4.0
    assert state.deferred_discount == 0.5
    assert "quota burns harder" in state.note
