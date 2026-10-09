"""Fixtures for the contain layer: a Containment engine on a throwaway ledger."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from spendrouter.config import parse_config
from spendrouter.contain import Containment

from tests.contain.fakes import FakeClock

# Prices that make test arithmetic checkable by hand: 1M input tokens = $1.
TEST_PRICING = {"test-model": {"input": 1.0, "output": 2.0}}


@pytest.fixture
def make_containment(tmp_path: Any) -> Iterator[Callable[..., Containment]]:
    engines: list[Containment] = []

    def factory(extra: dict[str, Any] | None = None, clock: FakeClock | None = None) -> Containment:
        data: dict[str, Any] = {
            "db_path": str(tmp_path / f"state{len(engines)}" / "ledger.sqlite3"),
            "pricing": dict(TEST_PRICING),
        }
        data.update(extra or {})
        engine = Containment(parse_config(data).contain, clock=clock or FakeClock())
        engines.append(engine)
        return engine

    yield factory
    for engine in engines:
        engine.close()
