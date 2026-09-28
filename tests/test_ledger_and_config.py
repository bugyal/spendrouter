"""Ledger rolling windows and config validation."""

from __future__ import annotations

import time

import pytest

from spendrouter.config import ConfigError, load_config


def test_rolling_windows_exclude_old_events(ledger):
    now = time.time()
    ledger.record(project="p", model="m", tier="payg", usd=1.0, ts=now - 30)          # last hour
    ledger.record(project="p", model="m", tier="payg", usd=2.0, ts=now - 2 * 3600)    # last week
    ledger.record(project="p", model="m", tier="payg", usd=4.0, ts=now - 8 * 24 * 3600)  # older
    usage = ledger.usage("p", now=now)
    assert usage.hour_usd == pytest.approx(1.0)
    assert usage.week_usd == pytest.approx(3.0)
    assert usage.calls == 2


def test_usage_is_per_project(ledger):
    now = time.time()
    ledger.record(project="a", model="m", tier="payg", usd=5.0, ts=now)
    ledger.record(project="b", model="m", tier="payg", usd=7.0, ts=now)
    assert ledger.usage("a", now=now).week_usd == pytest.approx(5.0)
    assert ledger.usage("b", now=now).week_usd == pytest.approx(7.0)


def test_deferred_spend_tracked_separately(ledger):
    now = time.time()
    ledger.record(project="p", model="m", tier="deferred", usd=3.0, ts=now)
    ledger.record(project="p", model="m", tier="payg", usd=4.0, ts=now)
    usage = ledger.usage("p", now=now)
    assert usage.week_deferred_usd == pytest.approx(3.0)
    assert usage.week_usd == pytest.approx(7.0)


def test_ledger_persists_across_connections(tmp_path):
    from spendrouter.ledger import Ledger

    path = str(tmp_path / "persist.sqlite3")
    with Ledger(path) as first:
        first.record(project="p", model="m", tier="payg", usd=2.5)
    with Ledger(path) as second:
        assert second.usage("p").week_usd == pytest.approx(2.5)


def test_daily_rollup_groups_by_day_and_project(ledger):
    now = time.time()
    ledger.record(project="p", model="m", tier="payg", usd=1.0, tokens_in=10, tokens_out=5, ts=now)
    ledger.record(project="q", model="m", tier="payg", usd=2.0, tokens_in=20, tokens_out=10, ts=now)
    rows = ledger.daily(days=2)
    by_project = {row["project"]: row for row in rows}
    assert by_project["p"]["usd"] == pytest.approx(1.0)
    assert by_project["p"]["tokens"] == 15
    assert by_project["q"]["tokens"] == 30


# -- config validation ---------------------------------------------------


def test_missing_config_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(str(tmp_path / "absent.toml"))


def test_invalid_toml_is_a_clear_error(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not = = toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(str(bad))


def test_unknown_tier_in_model_is_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[models.m]\nmagic = { usd_per_mtok = 1.0 }\n')
    with pytest.raises(ConfigError, match="unknown tier"):
        load_config(str(path))


def test_model_with_every_tier_null_is_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[models.m]\nsubscription = { quota_per_1k = 1.0 }\ntierx = 0\n')
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_unknown_tier_in_tier_order_is_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.p]\ntier_order = ["subscription", "magic"]\n'
    )
    with pytest.raises(ConfigError, match="unknown tier"):
        load_config(str(path))


def test_bad_peak_hour_is_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.p]\npeak_hours = [25]\n'
    )
    with pytest.raises(ConfigError, match="peak_hours"):
        load_config(str(path))


def test_malformed_deferred_window_is_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.p]\ndeferred_windows = ["9am-5pm"]\n'
    )
    with pytest.raises(ConfigError, match="HH:MM-HH:MM"):
        load_config(str(path))


def test_bad_default_project_is_rejected(tmp_path):
    """Naming a project that does not exist is a config error, not a silent fallback."""

    path = tmp_path / "c.toml"
    # NB: default_project must sit before any [table] header or TOML binds it to
    # the last table — the loader reads it as a top-level key.
    path.write_text(
        'default_project = "ghost"\n\n[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n'
        '[projects.p]\nallowed_models = ["m"]\n'
    )
    with pytest.raises(ConfigError, match="ghost"):
        load_config(str(path))


def test_ambiguous_default_project_is_rejected(tmp_path):
    """Two projects and no default_project is ambiguous — say so, don't guess."""

    path = tmp_path / "c.toml"
    path.write_text(
        '[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.alpha]\n\n[projects.beta]\n'
    )
    with pytest.raises(ConfigError, match="default_project is required"):
        load_config(str(path))


def test_single_project_config_needs_no_default_project(tmp_path):
    """One project should not have to be named twice."""

    path = tmp_path / "c.toml"
    path.write_text('[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.solo]\n')
    cfg = load_config(str(path))
    assert cfg.default_project == "solo"
    assert cfg.project().name == "solo"


def test_project_named_default_still_wins_as_the_default(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.default]\n\n[projects.other]\n'
    )
    cfg = load_config(str(path))
    assert cfg.default_project == "default"


def test_non_numeric_cap_is_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.p]\nhourly_usd = "lots"\n'
    )
    with pytest.raises(ConfigError, match="expected a number"):
        load_config(str(path))
