"""The containment engine: admission checks, the spend ledger, pauses, the breaker.

Both front ends share it — the HTTP proxy (``spendrouter serve``) and the
in-process API (:meth:`Containment.call` / :meth:`Containment.tool_result`) —
so a cap, a pause or a breaker trip means the same thing however an agent
reaches the model.

Admission order for every call:

1. credential scope and credential spend cap
2. pauses (agent, customer, task, or everything)
3. circuit breaker — tool-failure loops and identical-request loops
4. budget caps — hard refuses (or pauses), soft alerts once per window

After the call: record cost, trip the breaker on a run of identical upstream
errors, and fire any cap the call just crossed.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import load_config
from .config_contain import BudgetRule, ContainConfig
from .credentials import Credential, Credentials
from .events import EventSink
from .store import Store
from .toolscan import ToolResult, compile_patterns, error_class, scan_request
from .usage import TokenUsage, detect_format, parse_response, to_dict

__all__ = [
    "Attribution",
    "BudgetState",
    "CallHandle",
    "Containment",
    "Pause",
    "SpendBlocked",
    "UNATTRIBUTED",
    "Verdict",
    "hash_body",
    "period_start",
]

UNATTRIBUTED = "unattributed"
_SCOPE_COLUMNS = {"agent": "agent", "customer": "customer", "task": "task", "run": "run", "credential": "credential_id"}


@dataclass(frozen=True)
class Attribution:
    agent: str = UNATTRIBUTED
    task: str = ""
    customer: str = ""
    run: str = ""
    credential_id: str = ""

    def value(self, scope: str) -> str:
        return {"agent": self.agent, "customer": self.customer, "task": self.task}.get(scope, "")


@dataclass(frozen=True)
class Verdict:
    """Whether a call may go upstream. (Not policy.Decision, which picks a route tier.)"""

    allowed: bool
    reason: str = ""  # refusal code: paused | breaker | budget | credential | credential_scope | credential_budget
    message: str = ""
    status: int = 200  # HTTP status the proxy answers a refusal with
    retry: bool = False  # the call resends a recent identical request


ALLOW = Verdict(True)


class SpendBlocked(RuntimeError):
    """Raised by the in-process API when spendrouter refuses a call."""

    def __init__(self, verdict: Verdict):
        super().__init__(verdict.message)
        self.verdict = verdict


@dataclass(frozen=True)
class Pause:
    scope: str
    value: str
    kind: str
    reason: str
    paused_at: float
    until: float | None


@dataclass(frozen=True)
class BudgetState:
    rule: BudgetRule
    value: str
    window_start: float
    spend: float

    @property
    def level(self) -> str:
        if self.rule.hard_usd is not None and self.spend >= self.rule.hard_usd:
            return "hard"
        if self.rule.soft_usd is not None and self.spend >= self.rule.soft_usd:
            return "soft"
        return "ok"

    @property
    def label(self) -> str:
        return "*" if self.rule.scope == "global" else f"{self.rule.scope}={self.value}"


def hash_body(path: str, body: bytes) -> str:
    return hashlib.sha256(path.encode("utf-8") + b"\n" + body).hexdigest()[:32]


def period_start(period: str, now: float, tz: str = "utc") -> float:
    """Start of the calendar period containing ``now`` (UTC or local time)."""
    if period == "total":
        return 0.0
    dt = datetime.fromtimestamp(now) if tz == "local" else datetime.fromtimestamp(now, timezone.utc)
    if period == "hour":
        dt = dt.replace(minute=0, second=0, microsecond=0)
    elif period == "day":
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        dt = (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown period {period!r}")
    return dt.timestamp()


def _usd(value: float) -> str:
    return f"${value:,.2f}" if value >= 0.01 or value == 0 else f"${value:.4f}"


class CallHandle:
    """Yielded by :meth:`Containment.call`; tell it what the call cost."""

    def __init__(self, model: str = ""):
        self.model = model
        self.usage = TokenUsage()
        self.cost_usd = 0.0

    def record_response(self, response: Any) -> None:
        """Read usage from an OpenAI/Anthropic SDK response object or a decoded JSON dict."""
        data = to_dict(response)
        usage, model = parse_response(detect_format(data), data)
        self.usage = usage
        self.model = model or self.model

    def set_usage(self, input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> None:
        self.usage = TokenUsage(int(input_tokens), int(output_tokens), int(cache_read_tokens), int(cache_write_tokens), found=True)


class Containment:
    def __init__(self, config: ContainConfig, *, clock: Callable[[], float] = time.time, store: Store | None = None):
        self.config = config
        self.clock = clock
        self.store = store or Store(config.db_path)
        self.events = EventSink(config, self.store, clock)
        self.patterns = compile_patterns(config.breaker.error_patterns)
        self.creds = Credentials(self.store, self.events, clock, config.upstreams)

    @classmethod
    def load(cls, path: str | None = None, **kwargs: Any) -> Containment:
        """Engine for a config file; with no file anywhere, the built-in defaults."""
        return cls(load_config(path, required=False).contain, **kwargs)

    def close(self) -> None:
        self.events.wait()
        self.store.close()

    def __enter__(self) -> Containment:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- admission -----------------------------------------------------------

    def admit(
        self,
        attr: Attribution,
        *,
        upstream: str = "",
        endpoint: str = "",
        model: str = "",
        tool_results: Sequence[ToolResult] = (),
        body_hash: str = "",
        retry_hint: bool = False,
        credential: Credential | None = None,
    ) -> Verdict:
        """Decide whether a call may go upstream. Refusals are written to the ledger."""
        now = self.clock()
        with self.store.transaction():
            verdict = self._admit(attr, now, upstream, tool_results, body_hash, credential)
            retry = retry_hint or self._is_resend(attr.agent, body_hash, now)
            if not verdict.allowed:
                self._insert_call(
                    attr,
                    now,
                    upstream=upstream,
                    endpoint=endpoint,
                    model=model,
                    outcome="refused",
                    error_class=verdict.reason,
                    retry=retry,
                    tool_error=_tool_error_label(tool_results),
                    body_hash=body_hash,
                )
        return Verdict(verdict.allowed, verdict.reason, verdict.message, verdict.status, retry)

    def record_refusal(self, attr: Attribution, verdict: Verdict, *, upstream: str = "", endpoint: str = "", model: str = "") -> None:
        """Ledger a call refused before admission (bad credential, credential required)."""
        self._insert_call(attr, self.clock(), upstream=upstream, endpoint=endpoint, model=model, outcome="refused", error_class=verdict.reason)

    def verify_credential(self, token: str) -> tuple[Credential | None, Verdict | None]:
        """The credential behind a token, and the refusal if it may not be used."""
        cred, problem = self.creds.check(token)
        return cred, (Verdict(False, "credential", problem, 401) if problem else None)

    def _admit(
        self,
        attr: Attribution,
        now: float,
        upstream: str,
        tool_results: Sequence[ToolResult],
        body_hash: str,
        credential: Credential | None,
    ) -> Verdict:
        if credential is not None:
            if credential.upstreams and upstream not in credential.upstreams:
                return Verdict(
                    False,
                    "credential_scope",
                    f"spendrouter: credential {credential.id} is not valid for upstream {upstream!r} "
                    f"(allowed: {', '.join(credential.upstreams)})",
                    403,
                )
            if credential.max_usd is not None:
                spent = self._spend("credential", credential.id, 0.0)
                if spent >= credential.max_usd:
                    return Verdict(
                        False,
                        "credential_budget",
                        f"spendrouter: credential {credential.id} has spent {_usd(spent)} of its {_usd(credential.max_usd)} limit",
                        402,
                    )
        pause = self.active_pause(attr, now)
        if pause is not None:
            return Verdict(False, "paused", _pause_message(pause), 423)
        if self.config.breaker.enabled:
            trip = self._breaker_admission(attr, now, tool_results, body_hash)
            if trip is not None:
                return trip
        for state in self.budget_states(attr, now):
            if state.level == "hard":
                return self._enforce_hard_cap(state, attr, now)
            if state.level == "soft":
                self._soft_alert(state, now)
        return ALLOW

    def _is_resend(self, agent: str, body_hash: str, now: float) -> bool:
        if not body_hash:
            return False
        since = now - self.config.breaker.retry_window_seconds
        row = self.store.one(
            "SELECT 1 FROM calls WHERE agent = ? AND body_hash = ? AND ts > ? AND outcome != 'refused' LIMIT 1",
            (agent, body_hash, since),
        )
        return row is not None

    # -- circuit breaker -----------------------------------------------------

    def _breaker_floor(self, agent: str, now: float) -> float:
        reset = self.store.scalar("SELECT ts FROM resets WHERE agent = ?", (agent,)) or 0.0
        return max(now - self.config.breaker.window_seconds, reset)

    def _breaker_admission(
        self, attr: Attribution, now: float, tool_results: Sequence[ToolResult], body_hash: str
    ) -> Verdict | None:
        breaker = self.config.breaker
        floor = self._breaker_floor(attr.agent, now)
        for result in tool_results:
            self.store.execute(
                "INSERT OR IGNORE INTO tool_events(ts, agent, tool, error_class, call_ref) VALUES (?, ?, ?, ?, ?)",
                (now, attr.agent, result.tool, result.error_class, result.call_id),
            )
        seen = set()
        for result in tool_results:
            key = (result.tool, result.error_class)
            if not result.is_error or key in seen:
                continue
            seen.add(key)
            last_ok = self.store.scalar(
                "SELECT MAX(ts) FROM tool_events WHERE agent = ? AND tool = ? AND error_class = ''",
                (attr.agent, result.tool),
            )
            # Failures since the tool last succeeded; a success resets the streak.
            count = self.store.scalar(
                "SELECT COUNT(*) FROM tool_events WHERE agent = ? AND tool = ? AND error_class = ? AND ts > ?",
                (attr.agent, result.tool, result.error_class, max(floor, last_ok or 0.0)),
            )
            if count > breaker.max_repeats:
                return self._trip(
                    attr,
                    now,
                    signal="tool_loop",
                    detail=f"tool {result.tool!r} failed {count}x in a row with {result.error_class}",
                    tool=result.tool,
                    error_class=result.error_class,
                    count=count,
                    threshold=breaker.max_repeats,
                )
        if body_hash and breaker.max_identical_requests > 0:
            sent = self.store.scalar(
                "SELECT COUNT(*) FROM calls WHERE agent = ? AND body_hash = ? AND ts > ? AND outcome != 'refused'",
                (attr.agent, body_hash, floor),
            )
            if sent >= breaker.max_identical_requests:
                return self._trip(
                    attr,
                    now,
                    signal="identical_requests",
                    detail=f"the same request was sent {sent + 1}x",
                    tool="request",
                    error_class="identical_request",
                    count=sent + 1,
                    threshold=breaker.max_identical_requests,
                )
        return None

    def _check_api_errors(self, attr: Attribution, upstream: str, error_cls: str, now: float) -> None:
        threshold = self.config.breaker.api_error_repeats
        if not threshold or not self.config.breaker.enabled:
            return
        floor = self._breaker_floor(attr.agent, now)
        last_ok = self.store.scalar(
            "SELECT MAX(ts) FROM calls WHERE agent = ? AND upstream = ? AND outcome = 'ok'", (attr.agent, upstream)
        )
        count = self.store.scalar(
            "SELECT COUNT(*) FROM calls WHERE agent = ? AND upstream = ? AND outcome = 'error' AND error_class = ? AND ts > ?",
            (attr.agent, upstream, error_cls, max(floor, last_ok or 0.0)),
        )
        if count > threshold and self.active_pause(attr, now) is None:
            self._trip(
                attr,
                now,
                signal="api_errors",
                detail=f"upstream {upstream!r} failed {count}x in a row with {error_cls}",
                tool=f"api:{upstream}",
                error_class=error_cls,
                count=count,
                threshold=threshold,
            )

    def _trip(self, attr: Attribution, now: float, *, signal: str, detail: str, **data: Any) -> Verdict:
        cooldown = self.config.breaker.cooldown_seconds
        until = now + cooldown if cooldown > 0 else None
        reason = f"circuit breaker ({signal}): {detail}"
        self._set_pause("agent", attr.agent, kind="breaker", reason=reason, now=now, until=until)
        self.events.emit(
            "breaker_tripped",
            f"spendrouter: agent {attr.agent!r} paused — {detail}",
            agent=attr.agent,
            task=attr.task,
            customer=attr.customer,
            signal=signal,
            **data,
        )
        pause = Pause("agent", attr.agent, "breaker", reason, now, until)
        return Verdict(False, "breaker", _pause_message(pause), 423)

    def tool_result(
        self,
        *,
        agent: str,
        tool: str,
        error: Any = None,
        call_id: str = "",
        task: str = "",
        customer: str = "",
    ) -> None:
        """Report a tool execution from your own harness (no proxy needed).

        ``error`` is None for success, else an exception or message. Raises
        :class:`SpendBlocked` when this failure trips the circuit breaker.
        """
        if error is None:
            cls = ""
        elif isinstance(error, BaseException):
            cls = error_class(f"{type(error).__name__}: {error}")
        else:
            cls = error_class(str(error)) or "error"
        attr = Attribution(agent=agent, task=task, customer=customer)
        now = self.clock()
        verdict: Verdict | None = None
        with self.store.transaction():
            pause = self.active_pause(attr, now)
            if pause is not None:
                verdict = Verdict(False, "paused", _pause_message(pause), 423)
            elif self.config.breaker.enabled:
                verdict = self._breaker_admission(attr, now, [ToolResult(tool, call_id, cls)], "")
        if verdict is not None:
            raise SpendBlocked(verdict)

    # -- pauses --------------------------------------------------------------

    def active_pause(self, attr: Attribution, now: float | None = None) -> Pause | None:
        now = self.clock() if now is None else now
        for scope, value in (("global", "*"), ("agent", attr.agent), ("customer", attr.customer), ("task", attr.task)):
            if not value:
                continue
            row = self.store.one("SELECT * FROM pauses WHERE scope = ? AND value = ?", (scope, value))
            if row is None:
                continue
            if row["until"] is not None and row["until"] <= now:
                self.resume(scope, value, by="cooldown")
                continue
            return Pause(row["scope"], row["value"], row["kind"], row["reason"], row["paused_at"], row["until"])
        return None

    def pauses(self) -> list[Pause]:
        now = self.clock()
        out = []
        for row in self.store.query("SELECT * FROM pauses ORDER BY paused_at"):
            if row["until"] is not None and row["until"] <= now:
                self.resume(row["scope"], row["value"], by="cooldown")
                continue
            out.append(Pause(row["scope"], row["value"], row["kind"], row["reason"], row["paused_at"], row["until"]))
        return out

    def pause(self, scope: str, value: str, *, reason: str = "paused by operator", until: float | None = None) -> None:
        if scope == "global":
            value = "*"
        self._set_pause(scope, value, kind="manual", reason=reason, now=self.clock(), until=until)

    def _set_pause(self, scope: str, value: str, *, kind: str, reason: str, now: float, until: float | None) -> None:
        if scope not in ("agent", "customer", "task", "global"):
            raise ValueError(f"cannot pause scope {scope!r}")
        with self.store.transaction():
            self.store.execute(
                "INSERT OR REPLACE INTO pauses(scope, value, kind, reason, paused_at, until) VALUES (?, ?, ?, ?, ?, ?)",
                (scope, value, kind, reason, now, until),
            )
            what = "everything" if scope == "global" else f"{scope} {value!r}"
            self.events.emit(
                "paused",
                f"spendrouter: paused {what} — {reason}",
                scope=scope,
                value=value,
                pause_kind=kind,
                reason=reason,
                until=until,
                agent=value if scope == "agent" else None,
            )

    def resume(self, scope: str, value: str, *, by: str = "operator") -> bool:
        if scope == "global":
            value = "*"
        now = self.clock()
        with self.store.transaction():
            gone = self.store.execute("DELETE FROM pauses WHERE scope = ? AND value = ?", (scope, value)).rowcount
            if not gone:
                return False
            if scope == "agent":  # breaker counts restart from here, or the next failure would re-trip at once
                self.store.execute("INSERT OR REPLACE INTO resets(agent, ts) VALUES (?, ?)", (value, now))
            what = "everything" if scope == "global" else f"{scope} {value!r}"
            self.events.emit(
                "resumed",
                f"spendrouter: resumed {what} ({by})",
                scope=scope,
                value=value,
                by=by,
                agent=value if scope == "agent" else None,
            )
        return True

    # -- budgets -------------------------------------------------------------

    def _spend(self, scope: str, value: str, since: float) -> float:
        if scope == "global":
            total = self.store.scalar("SELECT SUM(cost_usd) FROM calls WHERE ts >= ?", (since,))
        else:
            column = _SCOPE_COLUMNS[scope]
            total = self.store.scalar(f"SELECT SUM(cost_usd) FROM calls WHERE {column} = ? AND ts >= ?", (value, since))
        return float(total or 0.0)

    def budget_states(self, attr: Attribution, now: float | None = None) -> list[BudgetState]:
        """Every budget rule that applies to this attribution, with current-period spend."""
        now = self.clock() if now is None else now
        states = []
        for rule in self.config.budgets:
            value = "*" if rule.scope == "global" else attr.value(rule.scope)
            if not rule.applies_to(value):
                continue
            start = period_start(rule.period, now, self.config.timezone)
            states.append(BudgetState(rule, value, start, self._spend(rule.scope, value, start)))
        return states

    def budget_overview(self, now: float | None = None) -> list[BudgetState]:
        """Every rule × every value it currently covers (for reports)."""
        now = self.clock() if now is None else now
        states = []
        for rule in self.config.budgets:
            start = period_start(rule.period, now, self.config.timezone)
            if rule.scope == "global":
                values = ["*"]
            elif rule.match != "*":
                values = [rule.match]
            else:
                column = _SCOPE_COLUMNS[rule.scope]
                values = [
                    r[0]
                    for r in self.store.query(
                        f"SELECT DISTINCT {column} FROM calls WHERE ts >= ? AND {column} != '' ORDER BY 1", (start,)
                    )
                ]
            states.extend(BudgetState(rule, v, start, self._spend(rule.scope, v, start)) for v in values)
        return states

    def _alert_once(self, level: str, state: BudgetState, now: float) -> bool:
        key = f"{level}|{state.rule.name}|{state.value}|{int(state.window_start)}"
        cursor = self.store.execute("INSERT OR IGNORE INTO alerts(key, ts) VALUES (?, ?)", (key, now))
        return cursor.rowcount == 1

    def _soft_alert(self, state: BudgetState, now: float) -> None:
        rule = state.rule
        if rule.soft_usd is None or not self._alert_once("soft", state, now):
            return
        self.events.emit(
            "soft_cap_exceeded",
            f"spendrouter: {state.label} spent {_usd(state.spend)} this {rule.period}, over the soft cap of {_usd(rule.soft_usd)} ({rule.name})",
            rule=rule.name,
            scope=rule.scope,
            value=state.value,
            period=rule.period,
            spend_usd=round(state.spend, 6),
            limit_usd=rule.soft_usd,
            agent=state.value if rule.scope == "agent" else None,
        )

    def _enforce_hard_cap(self, state: BudgetState, attr: Attribution, now: float) -> Verdict:
        rule = state.rule
        assert rule.hard_usd is not None
        if self._alert_once("hard", state, now):
            self.events.emit(
                "hard_cap_exceeded",
                f"spendrouter: {state.label} reached the hard cap of {_usd(rule.hard_usd)} this {rule.period} "
                f"({_usd(state.spend)} spent) — calls are {'paused' if rule.action == 'pause' else 'refused'} ({rule.name})",
                rule=rule.name,
                scope=rule.scope,
                value=state.value,
                period=rule.period,
                spend_usd=round(state.spend, 6),
                limit_usd=rule.hard_usd,
                action=rule.action,
                agent=state.value if rule.scope == "agent" else attr.agent,
            )
        if rule.action == "pause":
            scope, value = (rule.scope, state.value) if rule.scope != "global" else ("global", "*")
            reason = f"hard cap {rule.name} reached ({_usd(state.spend)} >= {_usd(rule.hard_usd)})"
            if self.store.one("SELECT 1 FROM pauses WHERE scope = ? AND value = ?", (scope, value)) is None:
                self._set_pause(scope, value, kind="budget", reason=reason, now=now, until=None)
            return Verdict(False, "paused", _pause_message(Pause(scope, value, "budget", reason, now, None)), 423)
        return Verdict(
            False,
            "budget",
            f"spendrouter: {state.label} has spent {_usd(state.spend)} this {rule.period}; hard cap is "
            f"{_usd(rule.hard_usd)} ({rule.name}). Calls resume next {rule.period} or when the cap is raised.",
            402,
        )

    # -- recording -----------------------------------------------------------

    def record_call(
        self,
        attr: Attribution,
        *,
        upstream: str = "",
        endpoint: str = "",
        model: str = "",
        outcome: str = "ok",
        http_status: int | None = None,
        error_class: str = "",
        usage: TokenUsage | None = None,
        estimated: bool = False,
        latency_ms: int = 0,
        body_hash: str = "",
        retry: bool = False,
        tool_error: str = "",
    ) -> float:
        """Write a completed (or failed) call to the ledger; returns its cost in USD."""
        now = self.clock()
        usage = usage or TokenUsage()
        cost, priced = self.config.pricing.cost(model, usage)
        with self.store.transaction():
            self._insert_call(
                attr,
                now,
                upstream=upstream,
                endpoint=endpoint,
                model=model,
                outcome=outcome,
                http_status=http_status,
                error_class=error_class,
                usage=usage,
                cost=cost,
                priced=priced,
                estimated=estimated,
                latency_ms=latency_ms,
                body_hash=body_hash,
                retry=retry,
                tool_error=tool_error,
            )
            if outcome == "error":
                self._check_api_errors(attr, upstream, error_class, now)
            if cost > 0:
                for state in self.budget_states(attr, now):
                    if state.level == "hard":
                        self._enforce_hard_cap(state, attr, now)
                    elif state.level == "soft":
                        self._soft_alert(state, now)
        return cost

    def _insert_call(
        self,
        attr: Attribution,
        now: float,
        *,
        upstream: str = "",
        endpoint: str = "",
        model: str = "",
        outcome: str,
        http_status: int | None = None,
        error_class: str = "",
        usage: TokenUsage | None = None,
        cost: float = 0.0,
        priced: bool = True,
        estimated: bool = False,
        latency_ms: int = 0,
        body_hash: str = "",
        retry: bool = False,
        tool_error: str = "",
    ) -> None:
        u = usage or TokenUsage()
        self.store.execute(
            """INSERT INTO calls(ts, agent, task, customer, run, credential_id, upstream, endpoint, model,
                   outcome, http_status, error_class, retry, tool_error, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, priced, estimated, latency_ms, body_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                attr.agent,
                attr.task,
                attr.customer,
                attr.run,
                attr.credential_id,
                upstream,
                endpoint,
                model,
                outcome,
                http_status,
                error_class,
                int(retry),
                tool_error,
                u.input_tokens,
                u.output_tokens,
                u.cache_read_tokens,
                u.cache_write_tokens,
                cost,
                int(priced),
                int(estimated),
                int(latency_ms),
                body_hash,
            ),
        )

    # -- in-process API ------------------------------------------------------

    @contextmanager
    def call(
        self,
        *,
        agent: str,
        task: str = "",
        customer: str = "",
        run: str = "",
        model: str = "",
        request: Any = None,
        upstream: str = "sdk",
    ) -> Iterator[CallHandle]:
        """Contain one model call made by your own code.

        >>> with containment.call(agent="support-bot", customer="acme", model="claude-opus-5-5") as call:
        ...     response = client.messages.create(...)
        ...     call.record_response(response)

        Raises :class:`SpendBlocked` *before* the call when it is refused. Pass
        the request payload as ``request=`` to get tool-loop detection on it.
        """
        attr = Attribution(agent=agent, task=task, customer=customer, run=run)
        tool_results: list[ToolResult] = []
        body_hash = ""
        if request is not None:
            payload = to_dict(request)
            tool_results = scan_request(payload, self.patterns)
            body_hash = hash_body(upstream, json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
        verdict = self.admit(attr, upstream=upstream, model=model, tool_results=tool_results, body_hash=body_hash)
        if not verdict.allowed:
            raise SpendBlocked(verdict)
        handle = CallHandle(model)
        started = time.monotonic()
        common = dict(upstream=upstream, body_hash=body_hash, retry=verdict.retry, tool_error=_tool_error_label(tool_results))
        try:
            yield handle
        except BaseException as exc:
            status = getattr(exc, "status_code", None)
            handle.cost_usd = self.record_call(
                attr,
                model=handle.model,
                outcome="error",
                http_status=status if isinstance(status, int) else None,
                error_class=f"http_{status}" if isinstance(status, int) else type(exc).__name__,
                usage=handle.usage,
                latency_ms=int((time.monotonic() - started) * 1000),
                **common,  # type: ignore[arg-type]
            )
            raise
        handle.cost_usd = self.record_call(
            attr,
            model=handle.model,
            outcome="ok",
            usage=handle.usage,
            latency_ms=int((time.monotonic() - started) * 1000),
            **common,  # type: ignore[arg-type]
        )


def _tool_error_label(results: Sequence[ToolResult]) -> str:
    for result in results:
        if result.is_error:
            return f"{result.tool}:{result.error_class}"[:200]
    return ""


def _pause_message(pause: Pause) -> str:
    if pause.scope == "global":
        what, flag = "all agents are", "--all"
    else:
        what, flag = f"{pause.scope} {pause.value!r} is", f"--{pause.scope} {pause.value}"
    tail = ""
    if pause.until is not None:
        tail = f" Auto-resumes at {datetime.fromtimestamp(pause.until, timezone.utc).strftime('%H:%M:%S UTC')}."
    return f"spendrouter: {what} paused — {pause.reason}.{tail} Resume with: spendrouter resume {flag}"
