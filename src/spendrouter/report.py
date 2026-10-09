"""Rollups of the contain ledger as text, markdown or JSON — plus exit codes.

``spendrouter report`` reads the calls the proxy and the in-process API
ledgered: spend per agent / customer / task / run, how much of it went on
retries, failures and tool-error loops, what was refused and why, budgets
and pauses. The exit code describes the containment state *now*, so a
harness can gate on it (``report`` in cron, ``check`` before a run):

    0  OK      nothing over a cap, nothing paused
    3  HARD    a hard cap is reached and calls are refused — the code
               ``route`` exits with on a refusal, so one check covers both layers
    5  PAUSED  an agent, customer, task or everything is paused
    6  SOFT    a soft cap is exceeded (an alert; calls still flow)

1 is an error, 2 a usage error and 4 a config error, for every verb.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .contain import BudgetState, Containment, Pause, period_start

__all__ = [
    "EXIT_HARD",
    "EXIT_OK",
    "EXIT_PAUSED",
    "EXIT_SOFT",
    "GROUPS",
    "STATUS_NAMES",
    "ReportOptions",
    "build_report",
    "containment_status",
    "render",
]

EXIT_OK, EXIT_HARD, EXIT_PAUSED, EXIT_SOFT = 0, 3, 5, 6
STATUS_NAMES = {EXIT_OK: "OK", EXIT_SOFT: "SOFT CAP EXCEEDED", EXIT_HARD: "HARD CAP REACHED", EXIT_PAUSED: "PAUSED"}

GROUPS = {
    "agent": "agent",
    "customer": "customer",
    "task": "task",
    "run": "run",
    "model": "model",
    "upstream": "upstream",
    "credential": "credential_id",
    "outcome": "outcome",
    "hour": "%Y-%m-%d %H:00",
    "day": "%Y-%m-%d",
    "month": "%Y-%m",
}
_TIME_GROUPS = ("hour", "day", "month")
_PERIOD_LABELS = {"hour": "this hour", "day": "today", "week": "this week", "month": "this month", "total": "all time"}
_EVENT_KINDS = ("breaker_tripped", "hard_cap_exceeded", "soft_cap_exceeded", "paused", "resumed")

# "Waste" is spend on calls that were retries, failures, or carried a failed
# tool result back to the model: the money a loop burns.
_AGGREGATES = """
    COUNT(*) AS calls,
    COALESCE(SUM(outcome = 'ok'), 0) AS ok,
    COALESCE(SUM(outcome = 'error'), 0) AS failed,
    COALESCE(SUM(outcome = 'refused'), 0) AS refused,
    COALESCE(SUM(retry), 0) AS retries,
    COALESCE(SUM(tool_error != ''), 0) AS tool_errors,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
    COALESCE(SUM(cost_usd), 0) AS cost_usd,
    COALESCE(SUM(CASE WHEN retry = 1 OR tool_error != '' OR outcome = 'error' THEN cost_usd ELSE 0 END), 0) AS waste_usd,
    COALESCE(SUM(estimated), 0) AS estimated,
    COALESCE(SUM(priced = 0), 0) AS unpriced
"""


@dataclass
class ReportOptions:
    period: str = "day"  # hour | day | week | month | total (the current calendar period)
    since: float | None = None  # overrides period
    until: float | None = None
    by: tuple[str, ...] = ("customer", "agent")  # or ("call",) for a per-call listing
    agent: str | None = None
    customer: str | None = None
    task: str | None = None
    run: str | None = None
    limit: int = 50


def _relevant(scope: str, value: str, opts: ReportOptions) -> bool:
    filters = {"agent": opts.agent, "customer": opts.customer, "task": opts.task}
    if not any(filters.values()) or scope == "global":
        return True
    return filters.get(scope) is not None and filters[scope] == value


def containment_status(budgets: Sequence[BudgetState], pauses: Sequence[Pause]) -> int:
    if pauses:
        return EXIT_PAUSED
    if any(s.level == "hard" for s in budgets):
        return EXIT_HARD
    if any(s.level == "soft" for s in budgets):
        return EXIT_SOFT
    return EXIT_OK


def build_report(engine: Containment, opts: ReportOptions) -> dict[str, Any]:
    now = engine.clock()
    tz = engine.config.timezone
    since = opts.since if opts.since is not None else period_start(opts.period, now, tz)
    where: list[str] = ["ts >= ?"]
    params: list[Any] = [since]
    if opts.until is not None:
        where.append("ts < ?")
        params.append(opts.until)
    for name in ("agent", "customer", "task", "run"):
        value = getattr(opts, name)
        if value is not None:
            where.append(f"{name} = ?")
            params.append(value)
    clause = " AND ".join(where)
    store = engine.store

    totals = dict(store.one(f"SELECT {_AGGREGATES} FROM calls WHERE {clause}", params))  # type: ignore[arg-type]
    groups: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    group_count = 0
    if opts.by == ("call",):
        calls = [
            dict(r)
            for r in store.query(
                "SELECT ts, agent, task, customer, run, model, outcome, http_status, error_class, retry, tool_error, "
                "input_tokens + cache_read_tokens + cache_write_tokens AS input_tokens, output_tokens, cost_usd, "
                f"estimated FROM calls WHERE {clause} ORDER BY ts DESC, id DESC LIMIT ?",
                params + [opts.limit],
            )
        ]
    else:
        modifier = ", 'localtime'" if tz == "local" else ""
        exprs = [
            f"strftime('{GROUPS[g]}', ts, 'unixepoch'{modifier}) AS \"{g}\"" if g in _TIME_GROUPS else f'{GROUPS[g]} AS "{g}"'
            for g in opts.by
        ]
        keys = ", ".join(f'"{g}"' for g in opts.by)
        order = keys if any(g in _TIME_GROUPS for g in opts.by) else "cost_usd DESC, calls DESC"
        sql = f"SELECT {', '.join(exprs)}, {_AGGREGATES} FROM calls WHERE {clause} GROUP BY {keys} ORDER BY {order}"
        rows = store.query(sql, params)
        group_count = len(rows)
        groups = [dict(r) for r in rows[: opts.limit]]

    budgets = [s for s in engine.budget_overview(now) if _relevant(s.rule.scope, s.value, opts)]
    pauses = [p for p in engine.pauses() if _relevant(p.scope, p.value, opts)]
    kinds = ",".join("?" * len(_EVENT_KINDS))
    events = [
        json.loads(r["data"])
        for r in store.query(
            f"SELECT data FROM contain_events WHERE ts >= ? AND kind IN ({kinds}) ORDER BY ts DESC, id DESC LIMIT 20",
            [since, *_EVENT_KINDS],
        )
    ]
    unpriced = [
        r[0]
        for r in store.query(f"SELECT DISTINCT model FROM calls WHERE {clause} AND priced = 0 ORDER BY 1", params)
    ]
    status = containment_status(budgets, pauses)
    return {
        "generated_at": now,
        "timezone": tz,
        "period": None if opts.since is not None else opts.period,
        "since": since,
        "until": opts.until if opts.until is not None else now,
        "filters": {k: getattr(opts, k) for k in ("agent", "customer", "task", "run") if getattr(opts, k) is not None},
        "by": list(opts.by),
        "totals": totals,
        "groups": groups,
        "group_count": group_count,
        "calls": calls,
        "budgets": [
            {
                "rule": s.rule.name,
                "scope": s.rule.scope,
                "value": s.value,
                "period": s.rule.period,
                "spend_usd": round(s.spend, 6),
                "soft_usd": s.rule.soft_usd,
                "hard_usd": s.rule.hard_usd,
                "action": s.rule.action,
                "level": s.level,
            }
            for s in budgets
        ],
        "paused": [
            {"scope": p.scope, "value": p.value, "kind": p.kind, "reason": p.reason, "since": p.paused_at, "until": p.until}
            for p in pauses
        ],
        "events": events,
        "unpriced_models": unpriced,
        "status": status,
        "status_name": STATUS_NAMES[status],
    }


# -- rendering ---------------------------------------------------------------


def _usd(value: float | None) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def _tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _when(ts: float, tz: str) -> str:
    if tz == "local":
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _range(report: dict[str, Any]) -> str:
    label = _PERIOD_LABELS.get(report["period"] or "", "custom range")
    since = "the beginning" if report["since"] <= 0 else _when(report["since"], report["timezone"])
    return f"{label} ({since} -> {_when(report['until'], report['timezone'])})"


def _group_rows(report: dict[str, Any]) -> tuple[list[str], list[list[str]], str]:
    by = report["by"]
    if by == ["call"]:
        headers = ["time", "agent", "customer", "task", "model", "outcome", "tokens in", "tokens out", "spend", "flags"]
        rows = []
        for c in report["calls"]:
            flags = [f for f, on in (("retry", c["retry"]), ("estimated", c["estimated"])) if on]
            if c["tool_error"]:
                flags.append(f"tool_error={c['tool_error']}")
            outcome = c["outcome"] + (f" ({c['error_class']})" if c["error_class"] else "")
            rows.append(
                [
                    _when(c["ts"], report["timezone"]),
                    c["agent"],
                    c["customer"] or "-",
                    c["task"] or "-",
                    c["model"] or "-",
                    outcome,
                    _tok(c["input_tokens"]),
                    _tok(c["output_tokens"]),
                    _usd(c["cost_usd"]),
                    " ".join(flags),
                ]
            )
        return headers, rows, "llllllrrrl"
    headers = list(by) + ["calls", "failed", "refused", "retries", "tool err", "tokens in", "tokens out", "spend", "retry/fail $"]
    rows = []
    for g in report["groups"]:
        prompt = g["input_tokens"] + g["cache_read_tokens"] + g["cache_write_tokens"]
        rows.append(
            [str(g[k]) if g[k] not in (None, "") else "-" for k in by]
            + [
                f"{g['calls']:,}",
                f"{g['failed']:,}",
                f"{g['refused']:,}",
                f"{g['retries']:,}",
                f"{g['tool_errors']:,}",
                _tok(prompt),
                _tok(g["output_tokens"]),
                _usd(g["cost_usd"]),
                _usd(g["waste_usd"]),
            ]
        )
    return headers, rows, "l" * len(by) + "r" * 9


def _budget_rows(report: dict[str, Any]) -> tuple[list[str], list[list[str]], str]:
    headers = ["rule", "applies to", "period", "spent", "soft", "hard", "used", "status"]
    rows = []
    for b in report["budgets"]:
        limit = b["hard_usd"] if b["hard_usd"] is not None else b["soft_usd"]
        used = f"{b['spend_usd'] / limit * 100:.0f}%" if limit else "-"
        status = {"ok": "ok", "soft": "SOFT", "hard": "HARD" + (" (pause)" if b["action"] == "pause" else "")}[b["level"]]
        target = "*" if b["scope"] == "global" else f"{b['scope']}={b['value']}"
        rows.append([b["rule"], target, b["period"], _usd(b["spend_usd"]), _usd(b["soft_usd"]), _usd(b["hard_usd"]), used, status])
    return headers, rows, "lllrrrrl"


def _text_table(headers: list[str], rows: list[list[str]], align: str) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        parts = [c.rjust(w) if a == "r" else c.ljust(w) for c, w, a in zip(cells, widths, align)]
        return "  ".join(parts).rstrip()

    return [fmt(headers)] + [fmt(r) for r in rows]


def _md_table(headers: list[str], rows: list[list[str]], align: str) -> list[str]:
    def esc(cell: str) -> str:
        return cell.replace("|", "\\|")

    out = ["| " + " | ".join(esc(h) for h in headers) + " |"]
    out.append("|" + "|".join("---:" if a == "r" else "---" for a in align) + "|")
    out.extend("| " + " | ".join(esc(c) for c in r) + " |" for r in rows)
    return out


def _notes(report: dict[str, Any]) -> list[str]:
    notes = []
    t = report["totals"]
    if report["unpriced_models"]:
        notes.append(
            f"{t['unpriced']:,} call(s) were charged the fallback price — unknown model(s): "
            f"{', '.join(m or '(none)' for m in report['unpriced_models'])}. Add them under pricing: in spendrouter.yml."
        )
    if t["estimated"]:
        notes.append(f"{t['estimated']:,} call(s) returned no usage block; their tokens are estimated (~4 chars/token).")
    if report["group_count"] > len(report["groups"]):
        notes.append(f"showing {len(report['groups'])} of {report['group_count']} groups (raise --limit to see more).")
    return notes


def _summary(t: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("spend", f"{_usd(t['cost_usd'])}  (on retries, failures and tool-error loops: {_usd(t['waste_usd'])})"),
        (
            "calls",
            f"{t['calls']:,}  ok {t['ok']:,} · failed {t['failed']:,} · refused {t['refused']:,} · "
            f"retries {t['retries']:,} · carrying tool errors {t['tool_errors']:,}",
        ),
        (
            "tokens",
            f"in {_tok(t['input_tokens'])} · out {_tok(t['output_tokens'])} · "
            f"cache read {_tok(t['cache_read_tokens'])} · cache write {_tok(t['cache_write_tokens'])}",
        ),
    ]


def render_text(report: dict[str, Any]) -> str:
    tz = report["timezone"]
    lines = [f"spendrouter report · {_range(report)}"]
    if report["filters"]:
        lines.append("filter: " + " ".join(f"{k}={v}" for k, v in report["filters"].items()))
    lines.append("")
    lines.extend(f"{label:<7} {value}" for label, value in _summary(report["totals"]))
    headers, rows, align = _group_rows(report)
    title = "calls (most recent first)" if report["by"] == ["call"] else "by " + ", ".join(report["by"])
    lines += ["", title]
    lines.extend(_text_table(headers, rows, align) if rows else ["(no calls)"])
    if report["budgets"]:
        lines += ["", "budgets (current period)"]
        lines.extend(_text_table(*_budget_rows(report)))
    if report["paused"]:
        lines += ["", "paused"]
        for p in report["paused"]:
            target = "everything" if p["scope"] == "global" else f"{p['scope']}={p['value']}"
            until = f" until {_when(p['until'], tz)}" if p["until"] else " until resumed"
            lines.append(f"  {target}  [{p['kind']}] since {_when(p['since'], tz)}{until} — {p['reason']}")
    if report["events"]:
        lines += ["", "recent events"]
        lines.extend(f"  {_when(e['ts'], tz)}  {e['kind']:<17}  {e['text']}" for e in report["events"])
    notes = _notes(report)
    if notes:
        lines.append("")
        lines.extend(f"note: {n}" for n in notes)
    lines += ["", f"status: {report['status_name']} (exit {report['status']})"]
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    tz = report["timezone"]
    lines = [f"# spendrouter report — {_range(report)}", ""]
    if report["filters"]:
        lines += ["Filter: " + ", ".join(f"`{k}={v}`" for k, v in report["filters"].items()), ""]
    lines += ["| | |", "|---|---|"]
    lines.extend(f"| {label} | {value} |" for label, value in _summary(report["totals"]))
    headers, rows, align = _group_rows(report)
    title = "Calls (most recent first)" if report["by"] == ["call"] else "By " + ", ".join(report["by"])
    lines += ["", f"## {title}", ""]
    lines.extend(_md_table(headers, rows, align) if rows else ["_No calls in this range._"])
    if report["budgets"]:
        lines += ["", "## Budgets (current period)", ""]
        lines.extend(_md_table(*_budget_rows(report)))
    if report["paused"]:
        lines += ["", "## Paused", ""]
        for p in report["paused"]:
            target = "everything" if p["scope"] == "global" else f"{p['scope']}={p['value']}"
            until = f"until {_when(p['until'], tz)}" if p["until"] else "until resumed"
            lines.append(f"- `{target}` — {p['kind']}, since {_when(p['since'], tz)}, {until}: {p['reason']}")
    if report["events"]:
        lines += ["", "## Recent events", ""]
        lines.extend(f"- {_when(e['ts'], tz)} · `{e['kind']}` · {e['text']}" for e in report["events"])
    notes = _notes(report)
    if notes:
        lines.append("")
        lines.extend(f"> **Note:** {n}" for n in notes)
    lines += ["", f"**Status: {report['status_name']}** (exit {report['status']})"]
    return "\n".join(lines)


def render(report: dict[str, Any], fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=True, default=str)
    if fmt in ("md", "markdown"):
        return render_markdown(report)
    return render_text(report)
