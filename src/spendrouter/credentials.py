"""Scoped, expiring spend credentials (``sr_...``).

A credential stands in for the real provider key. The agent sends it as its
API key; the proxy pins the call's attribution (agent, task, customer, run)
to it and swaps in the real key upstream, so the agent never holds the
provider key. Credentials can be limited to some upstreams, capped in USD,
and set to expire, so a finished task leaves nothing reusable behind. Only a
SHA-256 of each token is stored; the token itself is shown once, at mint.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .events import EventSink
from .store import Store

__all__ = ["PREFIX", "Credential", "Credentials", "token_hash"]

# Tokens start with this so the proxy can tell a spendrouter credential from
# a provider key arriving in the same Authorization header.
PREFIX = "sr_"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Credential:
    id: str
    agent: str
    task: str
    customer: str
    run: str
    created_at: float
    expires_at: float | None
    revoked_at: float | None
    max_usd: float | None
    upstreams: tuple[str, ...]
    note: str

    @classmethod
    def from_row(cls, row: Any) -> Credential:
        return cls(
            id=row["id"],
            agent=row["agent"],
            task=row["task"],
            customer=row["customer"],
            run=row["run"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            max_usd=row["max_usd"],
            upstreams=tuple(u for u in row["upstreams"].split(",") if u),
            note=row["note"],
        )

    def active(self, now: float) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


class Credentials:
    """Mint, check, list and revoke credentials in the contain ledger."""

    def __init__(self, store: Store, events: EventSink, clock: Callable[[], float], upstreams: Collection[str]):
        self.store = store
        self.events = events
        self.clock = clock
        self.upstreams = upstreams  # the configured upstream names a credential may be scoped to

    def mint(
        self,
        *,
        agent: str,
        task: str = "",
        customer: str = "",
        run: str = "",
        ttl_seconds: float | None = None,
        max_usd: float | None = None,
        upstreams: Sequence[str] = (),
        note: str = "",
    ) -> tuple[str, Credential]:
        """Create a scoped credential. The token is returned once and never stored."""
        if not agent:
            raise ValueError("a credential needs an agent")
        unknown = [u for u in upstreams if u not in self.upstreams]
        if unknown:
            raise ValueError(f"unknown upstream(s): {', '.join(unknown)}")
        now = self.clock()
        token = PREFIX + secrets.token_urlsafe(32)
        cred = Credential(
            id="cred_" + secrets.token_hex(6),
            agent=agent,
            task=task,
            customer=customer,
            run=run,
            created_at=now,
            expires_at=now + ttl_seconds if ttl_seconds else None,
            revoked_at=None,
            max_usd=max_usd,
            upstreams=tuple(upstreams),
            note=note,
        )
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO credentials(id, token_hash, agent, task, customer, run, created_at, expires_at,
                       revoked_at, max_usd, upstreams, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                (cred.id, token_hash(token), agent, task, customer, run, now, cred.expires_at, max_usd, ",".join(upstreams), note),
            )
            self.events.emit(
                "credential_minted",
                f"spendrouter: minted {cred.id} for agent {agent!r}",
                id=cred.id,
                agent=agent,
                task=task,
                customer=customer,
                expires_at=cred.expires_at,
                max_usd=max_usd,
            )
        return token, cred

    def check(self, token: str) -> tuple[Credential | None, str]:
        """(credential, problem); problem is "" when the token may be used."""
        row = self.store.one("SELECT * FROM credentials WHERE token_hash = ?", (token_hash(token),))
        if row is None:
            return None, "spendrouter: unknown credential"
        cred = Credential.from_row(row)
        if cred.revoked_at is not None:
            return cred, f"spendrouter: credential {cred.id} was revoked"
        if cred.expires_at is not None and cred.expires_at <= self.clock():
            when = datetime.fromtimestamp(cred.expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            return cred, f"spendrouter: credential {cred.id} expired at {when}"
        return cred, ""

    def list(self, *, include_inactive: bool = False) -> list[Credential]:
        now = self.clock()
        creds = [Credential.from_row(r) for r in self.store.query("SELECT * FROM credentials ORDER BY created_at")]
        return creds if include_inactive else [c for c in creds if c.active(now)]

    def stats(self, cred_id: str) -> tuple[int, float]:
        """(calls, USD) recorded against a credential."""
        row = self.store.one("SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM calls WHERE credential_id = ?", (cred_id,))
        return (int(row[0]), float(row[1])) if row is not None else (0, 0.0)

    def revoke(self, *, ids: Sequence[str] = (), agent: str | None = None, task: str | None = None) -> list[str]:
        """Revoke by id, or every active credential of an agent or task. Returns revoked ids."""
        now = self.clock()
        clauses: list[str] = []
        params: list[Any] = []
        if ids:
            clauses.append(f"id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if task:
            clauses.append("task = ?")
            params.append(task)
        if not clauses:
            return []
        where = " OR ".join(clauses)
        with self.store.transaction():
            rows = self.store.query(f"SELECT id, agent FROM credentials WHERE revoked_at IS NULL AND ({where})", params)
            for row in rows:
                self.store.execute("UPDATE credentials SET revoked_at = ? WHERE id = ?", (now, row["id"]))
                self.events.emit(
                    "credential_revoked", f"spendrouter: revoked {row['id']}", id=row["id"], agent=row["agent"]
                )
        return [r["id"] for r in rows]

    def gc(self, older_than_seconds: float = 0.0) -> int:
        """Delete credentials that expired or were revoked more than ``older_than_seconds`` ago."""
        cutoff = self.clock() - older_than_seconds
        return self.store.execute(
            "DELETE FROM credentials WHERE (revoked_at IS NOT NULL AND revoked_at <= ?) "
            "OR (expires_at IS NOT NULL AND expires_at <= ?)",
            (cutoff, cutoff),
        ).rowcount
