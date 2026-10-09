"""The contain verbs of the CLI: serve, run, creds, report, check, pause, resume, init."""

from __future__ import annotations

import json
import sys

import pytest

from spendrouter.cli import main
from spendrouter.config import load_config
from spendrouter.contain import Attribution, Containment
from spendrouter.usage import TokenUsage

SIXTY_CENTS = TokenUsage(input_tokens=600_000, found=True)  # test-model: $1 per 1M input tokens


@pytest.fixture
def contain_config(tmp_path):
    """Write a contain-only config (JSON, to keep YAML quoting out of it) with its ledger in tmp_path."""

    def write(**sections):
        data = {"db_path": str(tmp_path / "ledger.sqlite3"), "pricing": {"test-model": {"input": 1.0, "output": 2.0}}}
        data.update(sections)
        path = tmp_path / "spendrouter.json"
        path.write_text(json.dumps(data))
        return str(path)

    return write


def cli(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def spend(config_path, attr, usage=SIXTY_CENTS):
    with Containment(load_config(config_path).contain) as engine:
        engine.record_call(attr, upstream="openai", model="test-model", usage=usage)


def test_serve_help_lists_the_proxy_flags(capsys):
    with pytest.raises(SystemExit) as info:
        main(["serve", "--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "--listen" in out and "--quiet" in out


def test_serve_rejects_a_bad_listen_address_before_binding(capsys, contain_config):
    code, _, err = cli(capsys, "--config", contain_config(), "serve", "--listen", "nowhere")
    assert code == 2 and "host:port" in err


def test_creds_mint_list_revoke_gc(capsys, contain_config):
    config = contain_config()
    code, out, err = cli(capsys, "--config", config, "creds", "mint", "--agent", "support-bot", "--customer", "acme", "--ttl", "2h", "--max-usd", "5")
    token = out.strip()
    assert code == 0 and token.startswith("sr_") and "shown once" in err  # stdout is the token alone
    with Containment(load_config(config).contain) as engine:
        cred, refusal = engine.verify_credential(token)
    assert refusal is None and (cred.agent, cred.customer, cred.max_usd) == ("support-bot", "acme", 5.0)
    code, out, _ = cli(capsys, "--config", config, "creds", "list")
    assert code == 0 and cred.id in out and "active" in out
    code, out, _ = cli(capsys, "--config", config, "creds", "revoke", "--agent", "support-bot")
    assert code == 0 and f"revoked {cred.id}" in out
    assert "no active credentials" in cli(capsys, "--config", config, "creds", "list")[1]
    assert cli(capsys, "--config", config, "creds", "revoke", cred.id)[0] == 1  # nothing left to revoke
    code, out, _ = cli(capsys, "--config", config, "creds", "gc")
    assert code == 0 and "removed 1" in out


def test_pause_resume_and_check(capsys, contain_config):
    config = contain_config()
    assert cli(capsys, "--config", config, "check", "--agent", "bot")[:2] == (0, "OK\n")
    code, out, _ = cli(capsys, "--config", config, "pause", "--agent", "bot", "--reason", "investigating")
    assert code == 0 and "paused agent 'bot'" in out
    code, out, _ = cli(capsys, "--config", config, "check", "--agent", "bot")
    assert code == 5 and "PAUSED" in out and "investigating" in out
    assert cli(capsys, "--config", config, "check", "--agent", "other-bot")[0] == 0
    assert cli(capsys, "--config", config, "resume", "--agent", "bot")[0] == 0
    code, _, err = cli(capsys, "--config", config, "resume", "--agent", "bot")
    assert code == 1 and "was not paused" in err
    assert cli(capsys, "--config", config, "check", "--agent", "bot")[0] == 0


def test_check_exit_codes_follow_the_caps(capsys, contain_config):
    config = contain_config(budgets=[{"scope": "customer", "match": "acme", "soft_usd": 0.5, "hard_usd": 1.0}])
    acme = Attribution(agent="bot", customer="acme")
    spend(config, acme)  # $0.60: over the soft cap
    code, out, _ = cli(capsys, "--config", config, "check", "--customer", "acme")
    assert code == 6 and out.startswith("SOFT")
    spend(config, acme)  # $1.20: the hard cap is reached
    code, out, _ = cli(capsys, "--config", config, "check", "--customer", "acme")
    assert code == 3 and out.startswith("HARD")  # 3, the code route exits with on a refusal
    assert cli(capsys, "--config", config, "check", "--customer", "globex")[0] == 0


def test_report_attributes_spend_and_exits_with_the_containment_state(capsys, contain_config):
    config = contain_config(budgets=[{"scope": "agent", "match": "*", "hard_usd": 1.0}])
    spend(config, Attribution(agent="support-bot", customer="acme"))
    spend(config, Attribution(agent="support-bot", customer="acme"))
    spend(config, Attribution(agent="researcher", customer="globex"))
    code, out, _ = cli(capsys, "--config", config, "report", "--by", "agent", "--json")
    report = json.loads(out)
    assert code == 3 == report["status"]  # support-bot is at its hard cap
    assert report["totals"]["calls"] == 3 and report["totals"]["cost_usd"] == pytest.approx(1.8)
    assert [(g["agent"], g["calls"]) for g in report["groups"]] == [("support-bot", 2), ("researcher", 1)]
    code, out, _ = cli(capsys, "--config", config, "report", "--exit-zero")
    assert code == 0 and "spendrouter report" in out and "support-bot" in out


def test_run_hands_the_child_a_credential_and_revokes_it(capsys, contain_config, tmp_path):
    config = contain_config()
    seen = tmp_path / "child-env.json"
    script = (
        "import json, os, sys; keys = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_BASE_URL', 'SPENDROUTER_AGENT', "
        "'SPENDROUTER_TASK'); json.dump({k: os.environ.get(k, '') for k in keys}, open(sys.argv[1], 'w')); sys.exit(7)"
    )
    code, _, err = cli(
        capsys, "--config", config, "run", "--agent", "nightly", "--task", "T-5", "--no-check",
        "--proxy", "http://127.0.0.1:9", "--", sys.executable, "-c", script, str(seen),
    )
    assert code == 7  # the child's exit code
    env = json.loads(seen.read_text())
    assert env["OPENAI_API_KEY"].startswith("sr_")  # the real key never reaches the agent
    assert (env["OPENAI_BASE_URL"], env["ANTHROPIC_BASE_URL"]) == ("http://127.0.0.1:9/openai/v1", "http://127.0.0.1:9/anthropic")
    assert (env["SPENDROUTER_AGENT"], env["SPENDROUTER_TASK"]) == ("nightly", "T-5")
    with Containment(load_config(config).contain) as engine:
        assert "revoked" in engine.verify_credential(env["OPENAI_API_KEY"])[1].message
    assert "revoked" in err


def test_init_writes_a_config_for_both_layers(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = cli(capsys, "init")
    assert code == 0 and "wrote spendrouter.yml" in out
    config = load_config("spendrouter.yml")
    assert config.projects and config.contain.upstreams
    assert cli(capsys, "init")[0] == 1  # never clobbers an existing file
    assert cli(capsys, "init", "--force")[0] == 0


def test_contain_verbs_run_without_a_config_file(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPENDROUTER_CONFIG", raising=False)
    db = str(tmp_path / "ledger.sqlite3")
    code, out, _ = cli(capsys, "--db", db, "creds", "list")
    assert code == 0 and "no active credentials" in out
    code, _, err = cli(capsys, "--db", db, "status")
    assert code == 4 and "config file not found" in err  # the route verbs still need one
