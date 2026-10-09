"""One spendrouter.yml configures both layers; a 0.1 spendrouter.toml still loads."""

from __future__ import annotations

from pathlib import Path

import pytest

import spendrouter
from spendrouter.cli import EXIT_CONFIG, EXIT_OK, main
from spendrouter.config import ConfigError, load_config
from spendrouter.policy import route

from tests.conftest import OUTSIDE_WINDOW_OFF_PEAK

TEMPLATE = Path(spendrouter.__file__).with_name("spendrouter.example.yml")

# The conftest TOML fixture, in YAML, plus one contain section. Note the
# dotted model name needs no quoting in YAML (TOML splits it into tables).
BOTH_LAYERS_YAML = """
default_project: work
db_path: {db}

models:
  big:
    subscription: {{quota_per_1k: 4.0}}
    deferred: {{usd_per_mtok: 1.0}}
    payg: {{usd_per_mtok: 10.0}}
  glm-5.3-flash:
    payg: {{usd_per_mtok: 0.30}}

projects:
  work:
    allowed_models: [big, glm-5.3-flash]
    hourly_usd: 5.0
    weekly_usd: 10.0
    hourly_quota: 500.0
    weekly_quota: 1000.0
    deferred_windows: ["00:00-06:00"]
    deferred_multiplier: 0.5

budgets:
  - scope: customer
    match: acme
    hard_usd: 15
"""


def run(capsys, argv):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def both_layers(tmp_path):
    path = tmp_path / "spendrouter.yml"
    path.write_text(BOTH_LAYERS_YAML.format(db=tmp_path / "ledger.sqlite3"))
    return load_config(str(path))


def test_yaml_config_drives_the_route_layer(both_layers):
    decision = route(both_layers, project="work", model="big", tokens=100_000, when=OUTSIDE_WINDOW_OFF_PEAK)
    assert decision.tier == "subscription"
    assert decision.quota_units == pytest.approx(400.0)  # 100k tokens at 4 quota/1k
    flash = route(both_layers, project="work", model="glm-5.3-flash", tokens=1_000_000, when=OUTSIDE_WINDOW_OFF_PEAK)
    assert (flash.tier, flash.usd) == ("payg", pytest.approx(0.30))  # 1 Mtok at $0.30


def test_yaml_config_drives_the_contain_layer(both_layers, tmp_path):
    (rule,) = both_layers.contain.budgets
    assert (rule.scope, rule.match, rule.hard_usd) == ("customer", "acme", 15.0)
    assert both_layers.contain.db_path == str(tmp_path / "ledger.sqlite3")  # the route ledger file


def test_toml_from_0_1_gets_contain_defaults(config, tmp_path):
    assert set(config.contain.upstreams) == {"openai", "anthropic"}
    assert config.contain.budgets == []
    assert config.contain.db_path == str(tmp_path / "ledger.sqlite3")


def test_toml_can_carry_contain_sections(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.p]\n\n'
        '[breaker]\nmax_repeats = 3\n\n[[budgets]]\nscope = "agent"\nmatch = "bot"\nhard_usd = 2.0\n'
    )
    cfg = load_config(str(path))
    assert cfg.contain.breaker.max_repeats == 3
    assert cfg.contain.budgets[0].name == "agent:bot:day"
    assert cfg.project().name == "p"


def test_yaml_rejects_unknown_top_level_keys(tmp_path):
    """A misspelt `budgets:` must fail loudly, not leave every agent uncapped."""

    path = tmp_path / "spendrouter.yml"
    path.write_text("budget:\n  - scope: global\n    hard_usd: 5\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(str(path))


def test_toml_keeps_ignoring_unknown_top_level_keys(tmp_path):
    """0.1 ignored stray top-level TOML keys; a config that routed then still does."""

    path = tmp_path / "c.toml"
    path.write_text('colour = "blue"\n\n[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.p]\n')
    assert load_config(str(path)).project().name == "p"


def test_invalid_yaml_is_a_clear_error(tmp_path):
    path = tmp_path / "spendrouter.yml"
    path.write_text("models:\n\tbig: 1\n")
    with pytest.raises(ConfigError, match="invalid YAML: line 2"):
        load_config(str(path))


def test_empty_yaml_project_is_an_empty_table(tmp_path):
    path = tmp_path / "spendrouter.yml"
    path.write_text("models:\n  m:\n    payg: {usd_per_mtok: 1.0}\nprojects:\n  solo:\n")
    cfg = load_config(str(path))
    assert cfg.default_project == "solo"
    assert cfg.project().hourly_usd is None


def test_discovery_prefers_yml_over_the_legacy_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPENDROUTER_CONFIG", raising=False)
    (tmp_path / "spendrouter.toml").write_text('[models.m]\npayg = { usd_per_mtok = 1.0 }\n\n[projects.old]\n')
    yml = tmp_path / "spendrouter.yml"
    yml.write_text("models:\n  m:\n    payg: {usd_per_mtok: 1.0}\nprojects:\n  new:\n")
    assert load_config().default_project == "new"
    yml.unlink()
    assert load_config().default_project == "old"


def test_route_verbs_need_a_file_contain_verbs_do_not(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPENDROUTER_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="config file not found"):
        load_config()
    assert load_config(required=False).projects == {}


def test_contain_only_config_tells_route_what_is_missing(capsys, tmp_path):
    path = tmp_path / "spendrouter.yml"
    path.write_text("budgets:\n  - scope: global\n    hard_usd: 5\n")
    code, _, err = run(
        capsys,
        ["--config", str(path), "--db", str(tmp_path / "l.sqlite3"), "route", "--model", "m", "--tokens", "10"],
    )
    assert code == EXIT_CONFIG
    assert "no projects configured" in err


@pytest.mark.parametrize(
    "at, tier",
    [
        # 02:00 is off-peak: 120k tokens at 4 quota/1k = 480 units, inside the 500/hour quota.
        ("2026-09-28T02:00:00", "subscription"),
        # 10:00 is peak: 480 x 3.5 = 1680 units breaks the hourly quota, deferred is
        # closed, so it falls through to payg: 0.12 Mtok x $15 = $1.80, inside $5/hour.
        ("2026-09-28T10:00:00", "payg"),
    ],
)
def test_shipped_template_routes_the_documented_call(capsys, tmp_path, at, tier):
    argv = ["--config", str(TEMPLATE), "--db", str(tmp_path / "l.sqlite3"), "route", "--project", "work",
            "--model", "claude-opus-5", "--tokens", "120000", "--dry-run", "--at", at]
    code, out, _ = run(capsys, argv)
    assert code == EXIT_OK
    assert out.startswith("ALLOW")
    assert f"tier: {tier}" in out


def test_shipped_template_agrees_with_pyyaml():
    yaml = pytest.importorskip("yaml")
    from spendrouter.miniyaml import loads

    text = TEMPLATE.read_text()
    assert loads(text) == yaml.safe_load(text)
