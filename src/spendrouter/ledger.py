"""Local sqlite ledger of quota burn and money spent.

One row per routed call. Rolling windows (last hour / last 7 days) are what the
hard caps are enforced against, so a runaway job gets stopped mid-flight rather
than being discovered at the end of the calendar day.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

HOUR_SECONDS = 3600
WEEK_SECONDS = 7 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    quota_units REAL NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    multiplier REAL NOT NULL DEFAULT 1.0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_project_ts ON events (project, ts);
"""


@dataclass(frozen=True)
class Usage:
    """Rolling-window totals for one project."""

    project: str
    hour_quota: float
    hour_usd: float
    week_quota: float
    week_usd: float
    week_deferred_usd: float
    tokens_in: int
    tokens_out: int
    calls: int


class Ledger:
    def __init__(self, path: str):
        expanded = os.path.expanduser(path)
        if expanded != ":memory:":
            parent = os.path.dirname(expanded)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.path = expanded
        self._conn = sqlite3.connect(expanded)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------
    def record(
        self,
        *,
        project: str,
        model: str,
        tier: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        quota_units: float = 0.0,
        usd: float = 0.0,
        multiplier: float = 1.0,
        note: str = "",
        ts: float | None = None,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO events (ts, project, model, tier, tokens_in, tokens_out,"
            " quota_units, usd, multiplier, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                time.time() if ts is None else ts,
                project,
                model,
                tier,
                int(tokens_in),
                int(tokens_out),
                float(quota_units),
                float(usd),
                float(multiplier),
                note,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    # -- reads ----------------------------------------------------------
    def usage(self, project: str, now: float | None = None) -> Usage:
        moment = time.time() if now is None else now
        row = self._conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN ts >= ? THEN quota_units END), 0) AS hour_quota,
              COALESCE(SUM(CASE WHEN ts >= ? THEN usd END), 0)         AS hour_usd,
              COALESCE(SUM(CASE WHEN ts >= ? THEN quota_units END), 0) AS week_quota,
              COALESCE(SUM(CASE WHEN ts >= ? THEN usd END), 0)         AS week_usd,
              COALESCE(SUM(CASE WHEN ts >= ? AND tier = 'deferred' THEN usd END), 0)
                                                                       AS week_deferred_usd,
              COALESCE(SUM(CASE WHEN ts >= ? THEN tokens_in END), 0)   AS tokens_in,
              COALESCE(SUM(CASE WHEN ts >= ? THEN tokens_out END), 0)  AS tokens_out,
              COUNT(CASE WHEN ts >= ? THEN 1 END)                      AS calls
            FROM events WHERE project = ?
            """,
            (
                moment - HOUR_SECONDS,
                moment - HOUR_SECONDS,
                moment - WEEK_SECONDS,
                moment - WEEK_SECONDS,
                moment - WEEK_SECONDS,
                moment - WEEK_SECONDS,
                moment - WEEK_SECONDS,
                moment - WEEK_SECONDS,
                project,
            ),
        ).fetchone()
        return Usage(
            project=project,
            hour_quota=float(row["hour_quota"]),
            hour_usd=float(row["hour_usd"]),
            week_quota=float(row["week_quota"]),
            week_usd=float(row["week_usd"]),
            week_deferred_usd=float(row["week_deferred_usd"]),
            tokens_in=int(row["tokens_in"]),
            tokens_out=int(row["tokens_out"]),
            calls=int(row["calls"]),
        )

    def recent(self, project: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if project:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE project = ? ORDER BY id DESC LIMIT ?",
                (project, limit),
            )
        else:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            )
        return list(rows)

    def daily(self, project: str | None = None, days: int = 7) -> list[sqlite3.Row]:
        """Per-day rollup for the ledger table in the CLI/README."""

        cutoff = time.time() - days * 24 * 3600
        params: list[object] = [cutoff]
        where = "ts >= ?"
        if project:
            where += " AND project = ?"
            params.append(project)
        return list(
            self._conn.execute(
                f"""
                SELECT date(ts, 'unixepoch', 'localtime') AS day,
                       project,
                       SUM(quota_units) AS quota_units,
                       SUM(usd) AS usd,
                       SUM(tokens_in + tokens_out) AS tokens,
                       COUNT(*) AS calls
                FROM events WHERE {where}
                GROUP BY day, project ORDER BY day DESC
                """,
                params,
            )
        )
