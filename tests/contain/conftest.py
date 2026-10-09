"""Fixtures for the contain layer: an engine on a throwaway ledger, and a live proxy."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from spendrouter.config import parse_config
from spendrouter.contain import Containment
from spendrouter.proxy import ProxyServer

from tests.contain.fakes import FakeClock, FakeUpstream

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


@pytest.fixture
def upstream() -> Iterator[FakeUpstream]:
    fake = FakeUpstream()
    yield fake
    fake.close()


@pytest.fixture
def make_proxy(tmp_path: Any, upstream: FakeUpstream, monkeypatch: Any) -> Iterator[Callable[..., SimpleNamespace]]:
    """A running proxy in front of FakeUpstream; returns (url, engine, server, upstream)."""
    monkeypatch.setenv("OPENAI_API_KEY", "real-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-anthropic-key")
    started: list[SimpleNamespace] = []

    def factory(extra: dict[str, Any] | None = None) -> SimpleNamespace:
        data: dict[str, Any] = {
            "db_path": str(tmp_path / f"proxy{len(started)}" / "ledger.sqlite3"),
            "upstreams": {
                "openai": {"base_url": upstream.url},
                "anthropic": {"base_url": upstream.url},
            },
        }
        data.update(extra or {})
        engine = Containment(parse_config(data).contain)
        server = ProxyServer(engine, "127.0.0.1", 0, quiet=True)
        server.start_background()
        env = SimpleNamespace(url=server.url, engine=engine, server=server, upstream=upstream)
        started.append(env)
        return env

    yield factory
    for env in started:
        env.server.stop()
        env.engine.close()
