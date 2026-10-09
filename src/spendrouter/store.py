"""SQLite persistence for the contain layer: calls, breaker state, pauses, credentials.

These tables live in the same file as the route layer's ``events`` table —
one ledger for the whole tool — which is why the contain event log is
``contain_events`` rather than ``events``. WAL mode lets the ``serve`` daemon
write while ``report`` / ``pause`` / ``creds`` (and ``route --commit``) read
and write from other processes. Credential tokens are never stored — only
their SHA-256.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = ["Store"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    task TEXT NOT NULL DEFAULT '',
    customer TEXT NOT NULL DEFAULT '',
    run TEXT NOT NULL DEFAULT '',
    credential_id TEXT NOT NULL DEFAULT '',
    upstream TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,                 -- ok | error | refused
    http_status INTEGER,
    error_class TEXT NOT NULL DEFAULT '',  -- http_503, upstream_unreachable, paused, budget, ...
    retry INTEGER NOT NULL DEFAULT 0,
    tool_error TEXT NOT NULL DEFAULT '',   -- "tool:error_class" fed back in this request
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    priced INTEGER NOT NULL DEFAULT 1,     -- 0: model unknown, fallback price used
    estimated INTEGER NOT NULL DEFAULT 0,  -- 1: no usage block, tokens estimated
    latency_ms INTEGER NOT NULL DEFAULT 0,
    body_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_agent_ts ON calls(agent, ts);
CREATE INDEX IF NOT EXISTS calls_customer_ts ON calls(customer, ts);
CREATE INDEX IF NOT EXISTS calls_task_ts ON calls(task, ts);
CREATE INDEX IF NOT EXISTS calls_run_ts ON calls(run, ts);
CREATE INDEX IF NOT EXISTS calls_credential ON calls(credential_id, ts);
CREATE INDEX IF NOT EXISTS calls_body ON calls(agent, body_hash, ts);
CREATE INDEX IF NOT EXISTS calls_upstream ON calls(agent, upstream, outcome, ts);

CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    tool TEXT NOT NULL,
    error_class TEXT NOT NULL DEFAULT '',  -- '' = the tool succeeded
    call_ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tool_events_key ON tool_events(agent, tool, error_class, ts);
CREATE UNIQUE INDEX IF NOT EXISTS tool_events_ref ON tool_events(agent, call_ref) WHERE call_ref != '';

CREATE TABLE IF NOT EXISTS pauses (
    scope TEXT NOT NULL,       -- agent | customer | task | global
    value TEXT NOT NULL,
    kind TEXT NOT NULL,        -- breaker | budget | manual
    reason TEXT NOT NULL,
    paused_at REAL NOT NULL,
    until REAL,                -- NULL: until resumed
    PRIMARY KEY (scope, value)
);

CREATE TABLE IF NOT EXISTS resets (
    agent TEXT PRIMARY KEY,    -- breaker counters only look at events after this
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    agent TEXT NOT NULL,
    task TEXT NOT NULL DEFAULT '',
    customer TEXT NOT NULL DEFAULT '',
    run TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL,
    max_usd REAL,
    upstreams TEXT NOT NULL DEFAULT '',  -- comma-separated allow-list; '' = any
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,      -- level|rule|value|window start: one alert per window
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contain_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    agent TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contain_events_ts ON contain_events(ts);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(os.path.expanduser(str(path)))
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)  # credential hashes and customer names live here
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant write transaction (BEGIN IMMEDIATE serialises writers across processes)."""
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outermost:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]
