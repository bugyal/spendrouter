"""Packaging and interpreter guards."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


def test_package_declares_the_311_floor():
    """requires-python must match the tomllib dependency it actually has."""

    pyproject = (SRC.parent / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.11"' in pyproject


def test_config_module_guards_against_an_old_interpreter():
    """The 3.11 guard must be present and must precede the real import."""

    source = (SRC / "spendrouter" / "config.py").read_text()
    guard = "sys.version_info < (3, 11)"
    assert guard in source
    # Match the statement, not a passing mention in a comment.
    import_lines = [
        i for i, line in enumerate(source.splitlines()) if line.strip() == "import tomllib"
    ]
    assert import_lines, "config.py must import tomllib"
    guard_lines = [i for i, line in enumerate(source.splitlines()) if guard in line]
    assert guard_lines, f"missing the {guard} guard"
    assert guard_lines[0] < import_lines[0], (
        "the guard must run before tomllib is imported, or it can never fire"
    )


def test_cli_runs_as_a_module():
    """`python -m spendrouter.cli --version` must work — the documented way in."""

    result = subprocess.run(
        [sys.executable, "-m", "spendrouter.cli", "--version"],
        capture_output=True,
        text=True,
        cwd=str(SRC.parent),
    )
    assert result.returncode == 0, result.stderr
    assert "spendrouter" in result.stdout
