"""Shared fixtures.

The config here is deliberately small and its arithmetic is checkable by hand:

  big      subscription 4.0 quota/1k   deferred $1.00/Mtok   payg $10.00/Mtok
  small    subscription 1.0 quota/1k   payg $1.00/Mtok
  localonly                            local only
  paygonly                             payg $2.00/Mtok

  work:    hourly_quota 500, weekly_quota 1000, hourly_usd $5, weekly_usd $10,
           deferred_budget $5, peak hour 09 at x4, deferred window 00:00-06:00 @ x0.5
"""

from __future__ import annotations

import datetime as _dt

import pytest

from spendrouter.config import load_config

CONFIG_TOML = """
default_project = "work"
quota_unit_value_usd = 0.02
db_path = "{db}"

[models.big]
subscription = {{ quota_per_1k = 4.0 }}
deferred = {{ usd_per_mtok = 1.0 }}
payg = {{ usd_per_mtok = 10.0 }}

[models.small]
subscription = {{ quota_per_1k = 1.0 }}
payg = {{ usd_per_mtok = 1.0 }}

[models.localonly]
local = true

[models.paygonly]
payg = {{ usd_per_mtok = 2.0 }}

[projects.work]
allowed_models = ["big", "small", "localonly", "paygonly"]
tier_order = ["subscription", "deferred", "payg", "local"]
hourly_usd = 5.0
weekly_usd = 10.0
hourly_quota = 500.0
weekly_quota = 1000.0
deferred_budget_usd = 5.0
peak_hours = [9]
peak_multiplier = 4.0
deferred_windows = ["00:00-06:00"]
deferred_multiplier = 0.5

[projects.nocaps]
allowed_models = ["big"]
tier_order = ["subscription", "deferred", "payg"]
"""


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "spendrouter.toml"
    path.write_text(CONFIG_TOML.format(db=tmp_path / "ledger.sqlite3"))
    return str(path)


@pytest.fixture
def config(config_path):
    return load_config(config_path)


# Fixed moments so schedule behaviour never depends on when the tests run.
IN_WINDOW_OFF_PEAK = _dt.datetime(2026, 9, 28, 2, 0, tzinfo=_dt.timezone.utc)
OUTSIDE_WINDOW_OFF_PEAK = _dt.datetime(2026, 9, 28, 12, 0, tzinfo=_dt.timezone.utc)
PEAK_OUTSIDE_WINDOW = _dt.datetime(2026, 9, 28, 9, 30, tzinfo=_dt.timezone.utc)


@pytest.fixture
def ledger(tmp_path):
    from spendrouter.ledger import Ledger

    led = Ledger(str(tmp_path / "test.sqlite3"))
    yield led
    led.close()
