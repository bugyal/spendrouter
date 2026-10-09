"""The contain half of spendrouter.yml: proxy, upstreams, breaker, budgets, pricing, hooks."""

import pytest

from spendrouter.config import ConfigError, load_config, parse_config


def test_defaults_without_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPENDROUTER_CONFIG", raising=False)
    config = load_config(required=False)
    assert config.source_path is None
    contain = config.contain
    assert contain.db_path == "~/.local/share/spendrouter/ledger.sqlite3"
    assert set(contain.upstreams) == {"openai", "anthropic"}
    assert contain.upstreams["anthropic"].format == "anthropic"
    assert contain.upstreams["anthropic"].auth == "x-api-key"
    assert contain.upstreams["openai"].auth == "bearer"
    assert contain.budgets == []
    assert contain.breaker.max_repeats == 10
    assert contain.proxy_url == "http://127.0.0.1:8787"


def test_env_config_shares_one_ledger_and_sets_listen(tmp_path, monkeypatch):
    cfg = tmp_path / "conf" / "spendrouter.yml"
    cfg.parent.mkdir()
    db = tmp_path / "data" / "ledger.sqlite3"
    cfg.write_text(f"db_path: {db}\nproxy:\n  listen: 0.0.0.0:9999\n")
    monkeypatch.setenv("SPENDROUTER_CONFIG", str(cfg))
    config = load_config(required=False)
    assert config.source_path == str(cfg)
    assert config.contain.db_path == config.db_path == str(db)  # one ledger file for both layers
    assert config.contain.events_path == db.parent / "events.jsonl"
    assert config.contain.proxy.port == 9999
    assert config.contain.proxy_url == "http://127.0.0.1:9999"  # clients never dial 0.0.0.0


def test_missing_explicit_file_is_an_error_even_when_optional(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.yml"), required=False)


def test_json_config_is_accepted(tmp_path):
    cfg = tmp_path / "spendrouter.json"
    cfg.write_text('{"budgets": [{"scope": "agent", "match": "bot", "hard_usd": 3}]}')
    config = load_config(str(cfg))
    assert config.contain.budgets[0].name == "agent:bot:day"


def test_upstreams_merge_add_and_remove():
    contain = parse_config(
        {
            "upstreams": {
                "openai": None,
                "anthropic": {"base_url": "http://localhost:9000/"},
                "openrouter": {"base_url": "https://openrouter.ai/api", "api_key_env": "OPENROUTER_API_KEY"},
            }
        }
    ).contain
    assert "openai" not in contain.upstreams
    assert contain.upstreams["anthropic"].base_url == "http://localhost:9000"
    assert contain.upstreams["anthropic"].api_key_env == "ANTHROPIC_API_KEY"  # merged with the default
    assert contain.upstreams["openrouter"].format == "openai"


def test_budget_rules():
    contain = parse_config(
        {
            "budgets": [
                {"scope": "customer", "match": "*", "soft_usd": 5, "hard_usd": 15},
                {"scope": "global", "period": "month", "hard_usd": 400, "action": "pause"},
            ]
        }
    ).contain
    each, everything = contain.budgets
    assert (each.name, each.period, each.action) == ("customer:*:day", "day", "refuse")
    assert each.applies_to("acme") and not each.applies_to("")
    assert everything.match == "*" and everything.applies_to("*")


@pytest.mark.parametrize(
    "data, message",
    [
        ({"budget": []}, "unknown key"),  # the typo that would silently disable every cap
        ({"budgets": [{"scope": "customer", "match": "a", "hard": 5}]}, "unknown key"),
        ({"budgets": [{"scope": "team", "match": "a", "hard_usd": 5}]}, "must be one of"),
        ({"budgets": [{"scope": "customer", "hard_usd": 5}]}, "match is required"),
        ({"budgets": [{"scope": "customer", "match": "a"}]}, "soft_usd, hard_usd"),
        ({"budgets": [{"scope": "customer", "match": "a", "soft_usd": 9, "hard_usd": 5}]}, "above hard_usd"),
        ({"budgets": [{"scope": "customer", "match": "a", "hard_usd": -1}]}, ">= 0"),
        ({"budgets": [{"scope": "customer", "match": "a", "hard_usd": "lots"}]}, "expected a number"),
        ({"budgets": [{"scope": "agent", "match": "a", "hard_usd": 1}, {"scope": "agent", "match": "a", "hard_usd": 2}]}, "duplicate rule"),
        ({"breaker": {"max_repeats": 0}}, ">= 1"),
        ({"breaker": {"error_patterns": ["("]}}, "invalid regex"),
        ({"proxy": {"listen": "localhost"}}, "host:port"),
        ({"upstreams": {"x": {"base_url": "ftp://x"}}}, r"http\(s\) URL"),
        ({"upstreams": {"x": {"format": "openai"}}}, "base_url is required"),
        ({"hooks": [{"events": ["nope"], "command": "true"}]}, "unknown event"),
        ({"hooks": [{"events": ["paused"]}]}, "command, url"),
        ({"pricing": {"m": {"input": 1}}}, "output"),
        ({"timezone": "PST"}, "must be one of"),
    ],
)
def test_validation_errors(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_hook_command_string_is_split_without_a_shell():
    hooks = parse_config({"hooks": [{"events": "paused", "command": "notify --channel 'ops room'"}]}).contain.hooks
    assert hooks[0].command == ("notify", "--channel", "ops room")
    assert hooks[0].wants("paused") and not hooks[0].wants("resumed")


def test_pricing_overrides_and_default():
    pricing = parse_config({"pricing": {"gpt-4o": {"input": 1, "output": 1}, "default": {"input": 99, "output": 99}}}).contain.pricing
    assert pricing.lookup("gpt-4o")[0].input == 1
    assert pricing.lookup("mystery")[0].input == 99
