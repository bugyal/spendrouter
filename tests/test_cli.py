"""CLI behaviour, including the end-to-end dry-run -> commit -> cap flow."""

from __future__ import annotations

import json

import pytest

from spendrouter.cli import EXIT_OK, EXIT_REFUSED, main


def run(capsys, argv):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def base_args(config_path, tmp_path):
    return ["--config", config_path, "--db", str(tmp_path / "cli.sqlite3")]


def test_route_dry_run_writes_nothing(capsys, config_path, tmp_path):
    argv = base_args(config_path, tmp_path) + [
        "route", "--project", "work", "--model", "big", "--tokens", "100000",
        "--at", "2026-09-28T02:00:00+00:00",
    ]
    code, out, _ = run(capsys, argv)
    assert code == EXIT_OK
    assert "ALLOW" in out
    assert "Dry run: nothing written" in out
    # Confirm nothing landed in the ledger.
    code, out, _ = run(capsys, base_args(config_path, tmp_path) + ["status", "--project", "work"])
    assert "calls tracked:  0" in out


def test_route_commit_records_and_status_reflects_it(capsys, config_path, tmp_path):
    at = ["--at", "2026-09-28T12:00:00+00:00"]
    argv = base_args(config_path, tmp_path) + [
        "route", "--project", "work", "--model", "big", "--tokens", "100000", "--commit",
    ] + at
    code, out, _ = run(capsys, argv)
    assert code == EXIT_OK
    assert "Recorded ledger event #1" in out

    code, out, _ = run(capsys, base_args(config_path, tmp_path) + ["status", "--project", "work"])
    assert "calls tracked:  1" in out
    assert "weekly spend" in out


def test_route_json_output_is_machine_readable(capsys, config_path, tmp_path):
    argv = base_args(config_path, tmp_path) + [
        "route", "--project", "work", "--model", "big", "--tokens", "100000",
        "--at", "2026-09-28T12:00:00+00:00", "--json",
    ]
    code, out, _ = run(capsys, argv)
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["allowed"] is True
    assert payload["tier"] == "subscription"
    assert payload["quotes"]
    assert payload["schedule"]["in_deferred_window"] is False


def test_refusal_exits_with_the_refused_code(capsys, config_path, tmp_path):
    argv = base_args(config_path, tmp_path) + [
        "route", "--project", "work", "--model", "paygonly", "--tokens", "6000000",
        "--at", "2026-09-28T12:00:00+00:00",
    ]
    code, out, _ = run(capsys, argv)
    assert code == EXIT_REFUSED
    assert "REFUSE" in out
    # The confirmation belongs on stdout, after the verdict, so a piped or
    # redirected log reads in order.
    assert "nothing was charged" in out
    assert out.index("REFUSE") < out.index("nothing was charged")


def test_verbose_shows_every_tier_quote(capsys, config_path, tmp_path):
    argv = base_args(config_path, tmp_path) + [
        "route", "--project", "work", "--model", "big", "--tokens", "100000",
        "--at", "2026-09-28T12:00:00+00:00", "-v",
    ]
    code, out, _ = run(capsys, argv)
    assert code == EXIT_OK
    assert "tier quotes:" in out
    assert "unavailable — outside every deferred window" in out
    assert "caps (after this call):" in out


def test_plan_counts_affordable_calls_then_stops(capsys, config_path, tmp_path):
    argv = base_args(config_path, tmp_path) + [
        "plan", "--project", "work", "--model", "paygonly", "--tokens", "500000", "--calls", "20",
        "--at", "2026-09-28T12:00:00+00:00",
    ]
    code, out, _ = run(capsys, argv)
    assert code == EXIT_REFUSED
    # paygonly is $2.00/Mtok; 500k tokens = $1.00 per call against a $5 hourly cap.
    assert "affordable calls: 5" in out
    assert "stopped at call 6" in out
    # A plan must never write to the real ledger.
    code, out, _ = run(capsys, base_args(config_path, tmp_path) + ["status", "--project", "work"])
    assert "calls tracked:  0" in out


def test_plan_within_budget_exits_ok(capsys, config_path, tmp_path):
    argv = base_args(config_path, tmp_path) + [
        "plan", "--project", "work", "--model", "big", "--tokens", "10000", "--calls", "3",
        "--at", "2026-09-28T12:00:00+00:00", "--json",
    ]
    code, out, _ = run(capsys, argv)
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["calls_affordable"] == 3
    assert payload["stop_reason"] is None


def test_caps_command_lists_the_configured_limits(capsys, config_path, tmp_path):
    code, out, _ = run(capsys, base_args(config_path, tmp_path) + ["caps", "--project", "work"])
    assert code == EXIT_OK
    assert "weekly_usd" in out
    assert "peak_multiplier" in out


def test_ledger_command_shows_rollup(capsys, config_path, tmp_path):
    at = ["--at", "2026-09-28T12:00:00+00:00"]
    run(
        capsys,
        base_args(config_path, tmp_path)
        + ["route", "--project", "work", "--model", "big", "--tokens", "100000", "--commit"]
        + at,
    )
    code, out, _ = run(capsys, base_args(config_path, tmp_path) + ["ledger", "--days", "2"])
    assert code == EXIT_OK
    assert "per-day rollup:" in out
    assert "recent events:" in out
    assert "#1" in out


def test_missing_config_returns_config_exit_code(capsys, tmp_path):
    code, _, err = run(capsys, ["--config", str(tmp_path / "ghost.toml"), "status"])
    assert code == 4
    assert "config error" in err
