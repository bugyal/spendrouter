"""spendrouter CLI.

    spendrouter route   --project work --model claude-opus-5 --tokens 200000 [--dry-run|--commit]
    spendrouter plan    --project work --model m --tokens N --calls 12
    spendrouter ledger  [--project work] [--days 7]
    spendrouter status  [--project work]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time

from . import __version__
from .config import ConfigError, load_config
from .ledger import WEEK_SECONDS, Ledger
from .policy import CapExceeded, Decision, commit, enforce, route
from .schedule import evaluate

EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_CONFIG = 4


def _human_usd(value: float) -> str:
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.5f}"
    return f"${value:,.2f}"


def _describe(decision: Decision, config, verbose: bool) -> str:
    lines: list[str] = []
    verdict = "ALLOW" if decision.allowed else "REFUSE"
    lines.append(
        f"{verdict}  project={decision.project}  model={decision.model}  "
        f"~{decision.tokens_in:,} in / {decision.tokens_out:,} out tokens"
    )
    lines.append(f"  reason: {decision.reason}")
    if decision.schedule:
        lines.append(
            f"  schedule: {decision.schedule.note} ({decision.schedule.when:%Y-%m-%d %H:%M %Z})"
        )
    if decision.allowed:
        lines.append(
            f"  tier: {decision.tier}   effective cost this call: "
            f"{_human_usd(decision.effective_usd)}"
            + (f"   quota burn: {decision.quota_units:.2f}" if decision.quota_units else "")
        )
    if verbose:
        lines.append("  tier quotes:")
        for quote in decision.quotes:
            lines.append(f"    {quote.describe()}")
        if decision.caps:
            lines.append("  caps (after this call):")
            for check in decision.caps:
                flag = "BREACH" if check.breached else "ok"
                lines.append(f"    [{flag}] {check.describe()}")
        if decision.usage:
            u = decision.usage
            lines.append(
                f"  rolling usage: hour {u.hour_quota:.1f} quota / {_human_usd(u.hour_usd)}, "
                f"week {u.week_quota:.1f} quota / {_human_usd(u.week_usd)} "
                f"({u.calls} calls)"
            )
    return "\n".join(lines)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", "-p", default=None, help="project name from the config")
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def _token_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", "-m", required=True, help="model name from the config")
    parser.add_argument("--tokens", type=int, default=None, help="total estimated tokens")
    parser.add_argument("--tokens-in", type=int, default=None)
    parser.add_argument("--tokens-out", type=int, default=None)
    parser.add_argument("--at", default=None, help="ISO timestamp to price against (default now)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spendrouter",
        description="Route model calls to the cheapest acceptable spend tier and hold the line on hard caps.",
    )
    parser.add_argument("--version", action="version", version=f"spendrouter {__version__}")
    # Config/db are top-level so they can precede the subcommand. Subcommand-
    # local copies would fight over defaults, so they live here only.
    parser.add_argument("--config", "-c", default=None, help="config TOML path")
    parser.add_argument("--db", default=None, help="override ledger sqlite path")
    sub = parser.add_subparsers(dest="command", required=True)

    route_p = sub.add_parser("route", help="decide (and optionally record) one call")
    _add_common(route_p)
    _token_args(route_p)
    mode = route_p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="pre-flight only, write nothing (this is the default)",
    )
    mode.add_argument(
        "--commit",
        action="store_true",
        help="record the call in the ledger after allowing it",
    )
    route_p.add_argument("--actual-usd", type=float, default=None, help="real cost, if it landed differently")
    route_p.add_argument("--actual-tokens-in", type=int, default=None)
    route_p.add_argument("--actual-tokens-out", type=int, default=None)
    route_p.add_argument("-v", "--verbose", action="store_true", help="show every tier quote and cap")

    plan_p = sub.add_parser("plan", help="cost a batch of calls before starting a long agent run")
    _add_common(plan_p)
    _token_args(plan_p)
    plan_p.add_argument("--calls", type=int, default=1, help="how many calls of this size")
    plan_p.add_argument("-v", "--verbose", action="store_true")

    status_p = sub.add_parser("status", help="rolling quota/spend for a project")
    _add_common(status_p)

    ledger_p = sub.add_parser("ledger", help="recent events and per-day rollup")
    _add_common(ledger_p)
    ledger_p.add_argument("--days", type=int, default=7)
    ledger_p.add_argument("--limit", type=int, default=10)

    cap_p = sub.add_parser("caps", help="show the caps configured for a project")
    _add_common(cap_p)

    return parser


def _parse_when(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    moment = _dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
    return moment


def _resolve_db(args, config) -> Ledger:
    return Ledger(args.db or config.db_path)


def cmd_route(args, config) -> int:
    ledger = _resolve_db(args, config)
    try:
        decision = route(
            config,
            project=args.project,
            model=args.model,
            tokens=args.tokens,
            tokens_in=args.tokens_in,
            tokens_out=args.tokens_out,
            when=_parse_when(args.at),
            ledger=ledger,
        )
        if args.json:
            print(json.dumps(_decision_json(decision), indent=2))
        else:
            print(_describe(decision, config, args.verbose))
        if not decision.allowed:
            if not args.json:
                print("\nNo ledger entry written; nothing was charged.")
            return EXIT_REFUSED
        if args.commit:
            row_id = commit(
                ledger,
                decision,
                tokens_in=args.actual_tokens_in,
                tokens_out=args.actual_tokens_out,
                actual_usd=args.actual_usd,
            )
            if not args.json:
                print(f"\nRecorded ledger event #{row_id}.")
        elif not args.json:
            print("\nDry run: nothing written. Re-run with --commit once the call actually happens.")
        return EXIT_OK
    finally:
        ledger.close()


def _decision_json(decision: Decision) -> dict:
    return {
        "project": decision.project,
        "model": decision.model,
        "allowed": decision.allowed,
        "tier": decision.tier,
        "tokens_in": decision.tokens_in,
        "tokens_out": decision.tokens_out,
        "effective_usd": round(decision.effective_usd, 6),
        "quota_units": round(decision.quota_units, 6),
        "usd": round(decision.usd, 6),
        "multiplier": decision.multiplier,
        "reason": decision.reason,
        "schedule": (
            {
                "is_peak": decision.schedule.is_peak,
                "in_deferred_window": decision.schedule.in_deferred_window,
                "quota_multiplier": decision.schedule.quota_multiplier,
                "deferred_discount": decision.schedule.deferred_discount,
                "note": decision.schedule.note,
            }
            if decision.schedule
            else None
        ),
        "quotes": [
            {
                "tier": q.tier,
                "available": q.available,
                "effective_usd": round(q.effective_usd, 6),
                "reason": q.reason,
            }
            for q in decision.quotes
        ],
        "caps": [
            {
                "name": c.name,
                "limit": c.limit,
                "current": round(c.current, 6),
                "projected": round(c.projected, 6),
                "breached": c.breached,
            }
            for c in decision.caps
        ],
    }


def _simulation_ledger(real: Ledger, project: str) -> Ledger:
    """A throwaway in-memory copy of ``project``'s recent events.

    ``plan`` must answer "how many of these calls can I afford?" without
    touching the real ledger, and cap checks read usage from the ledger, so the
    simulation needs a live copy of the project's rolling window — not a
    different project key.
    """

    sim = Ledger(":memory:")
    cutoff = time.time() - WEEK_SECONDS
    for row in real._conn.execute(
        "SELECT * FROM events WHERE project = ? AND ts >= ? ORDER BY id", (project, cutoff)
    ):
        sim.record(
            project=project,
            model=row["model"],
            tier=row["tier"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            quota_units=row["quota_units"],
            usd=row["usd"],
            multiplier=row["multiplier"],
            note="simulation seed",
            ts=row["ts"],
        )
    return sim


def cmd_plan(args, config) -> int:
    """Cost N identical calls, routing each against a simulated ledger."""

    real = _resolve_db(args, config)
    sim = _simulation_ledger(real, config.project(args.project).name)
    try:
        moment = _parse_when(args.at)
        total_effective = 0.0
        total_quota = 0.0
        total_usd = 0.0
        tiers: dict[str, int] = {}
        first_refusal: Decision | None = None
        done = 0
        for _index in range(args.calls):
            decision = route(
                config,
                project=args.project,
                model=args.model,
                tokens=args.tokens,
                tokens_in=args.tokens_in,
                tokens_out=args.tokens_out,
                when=moment,
                ledger=sim,
            )
            if not decision.allowed:
                first_refusal = decision
                break
            # Advance the simulation so call N+1 sees call N's spend. The
            # recorded quota/cash must reflect the *real* estimate, so bypass
            # commit's actual-token reconciliation and write the quote directly.
            sim.record(
                project=decision.project,
                model=decision.model,
                tier=decision.tier or "",
                tokens_in=decision.tokens_in,
                tokens_out=decision.tokens_out,
                quota_units=decision.quota_units,
                usd=decision.usd,
                multiplier=decision.multiplier,
                note="plan simulation",
            )
            total_effective += decision.effective_usd
            total_quota += decision.quota_units
            total_usd += decision.usd
            key = decision.tier or "?"
            tiers[key] = tiers.get(key, 0) + 1
            done += 1

        if args.json:
            print(
                json.dumps(
                    {
                        "project": args.project,
                        "model": args.model,
                        "calls_requested": args.calls,
                        "calls_affordable": done,
                        "tiers": tiers,
                        "estimated_effective_usd": round(total_effective, 6),
                        "estimated_quota_units": round(total_quota, 4),
                        "estimated_cash_usd": round(total_usd, 6),
                        "stopped_at_call": None if first_refusal is None else done + 1,
                        "stop_reason": None if first_refusal is None else first_refusal.reason,
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"plan: project={args.project or config.default_project} model={args.model} "
                f"calls={args.calls} (~{args.tokens or (args.tokens_in or 0) + (args.tokens_out or 0):,} tokens each)"
            )
            print(f"  affordable calls: {done}")
            if tiers:
                print("  tiers used: " + ", ".join(f"{k} x{v}" for k, v in sorted(tiers.items())))
            print(f"  estimated effective cost: {_human_usd(total_effective)}")
            print(f"  estimated quota burn:     {total_quota:.2f} units")
            print(f"  estimated cash spend:     {_human_usd(total_usd)}")
            if first_refusal:
                print(f"  stopped at call {done + 1}: {first_refusal.reason}")
        return EXIT_OK if first_refusal is None else EXIT_REFUSED
    finally:
        sim.close()
        real.close()


def cmd_status(args, config) -> int:
    project_cfg = config.project(args.project)
    ledger = _resolve_db(args, config)
    try:
        usage = ledger.usage(project_cfg.name)
        state = evaluate(project_cfg)
        if args.json:
            print(
                json.dumps(
                    {
                        "project": project_cfg.name,
                        "hour_quota": round(usage.hour_quota, 4),
                        "hour_usd": round(usage.hour_usd, 6),
                        "week_quota": round(usage.week_quota, 4),
                        "week_usd": round(usage.week_usd, 6),
                        "week_deferred_usd": round(usage.week_deferred_usd, 6),
                        "tokens_in": usage.tokens_in,
                        "tokens_out": usage.tokens_out,
                        "calls": usage.calls,
                        "schedule": {
                            "is_peak": state.is_peak,
                            "in_deferred_window": state.in_deferred_window,
                            "note": state.note,
                        },
                        "db_path": ledger.path,
                    },
                    indent=2,
                )
            )
            return EXIT_OK
        print(f"project: {project_cfg.name}   db: {ledger.path}")
        print(f"  schedule: {state.note}")
        rows = [
            ("hourly quota", usage.hour_quota, project_cfg.hourly_quota, "units"),
            ("hourly spend", usage.hour_usd, project_cfg.hourly_usd, "usd"),
            ("weekly quota", usage.week_quota, project_cfg.weekly_quota, "units"),
            ("weekly spend", usage.week_usd, project_cfg.weekly_usd, "usd"),
            ("weekly deferred", usage.week_deferred_usd, project_cfg.deferred_budget_usd, "usd"),
        ]
        for label, used, limit, unit in rows:
            if limit is None:
                print(f"  {label:<15} {used:>10.2f} {unit}   (no cap)")
            else:
                pct = 100.0 * used / limit if limit else 0.0
                bar = "#" * int(min(pct, 100) / 5) + "." * (20 - int(min(pct, 100) / 5))
                print(f"  {label:<15} {used:>10.2f} / {limit:<10.2f} {unit}  [{bar}] {pct:.0f}%")
        print(f"  calls tracked:  {usage.calls}   tokens: {usage.tokens_in:,} in / {usage.tokens_out:,} out")
        return EXIT_OK
    finally:
        ledger.close()


def cmd_ledger(args, config) -> int:
    ledger = _resolve_db(args, config)
    try:
        rows = ledger.daily(args.project, days=args.days)
        recent = ledger.recent(args.project, limit=args.limit)
        if args.json:
            print(
                json.dumps(
                    {
                        "daily": [dict(r) for r in rows],
                        "recent": [dict(r) for r in recent],
                    },
                    indent=2,
                )
            )
            return EXIT_OK
        print("per-day rollup:")
        if not rows:
            print("  (no events recorded yet)")
        for row in rows:
            print(
                f"  {row['day']}  {row['project']:<12} quota {row['quota_units']:>9.2f}  "
                f"{_human_usd(row['usd']):>9}  {row['tokens']:>10,} tokens  {row['calls']:>4} calls"
            )
        print("\nrecent events:")
        if not recent:
            print("  (none)")
        for row in recent:
            stamp = _dt.datetime.fromtimestamp(row["ts"]).strftime("%m-%d %H:%M")
            print(
                f"  #{row['id']:<4} {stamp}  {row['project']:<12} {row['model']:<20} "
                f"{row['tier']:<13} {_human_usd(row['usd']):>9}  {row['quota_units']:>8.2f}q"
            )
        return EXIT_OK
    finally:
        ledger.close()


def cmd_caps(args, config) -> int:
    project_cfg = config.project(args.project)
    payload = {
        "project": project_cfg.name,
        "allowed_models": list(project_cfg.allowed_models) if project_cfg.allowed_models else None,
        "tier_order": list(project_cfg.tier_order),
        "hourly_usd": project_cfg.hourly_usd,
        "weekly_usd": project_cfg.weekly_usd,
        "hourly_quota": project_cfg.hourly_quota,
        "weekly_quota": project_cfg.weekly_quota,
        "deferred_budget_usd": project_cfg.deferred_budget_usd,
        "peak_hours": list(project_cfg.peak_hours),
        "peak_multiplier": project_cfg.peak_multiplier,
        "deferred_windows": list(project_cfg.deferred_windows),
        "deferred_multiplier": project_cfg.deferred_multiplier,
        "quota_unit_value_usd": project_cfg.quota_unit_value_usd,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print(f"project: {project_cfg.name}")
    for key, value in payload.items():
        if key == "project":
            continue
        print(f"  {key:<20} {value}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"spendrouter: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    handlers = {
        "route": cmd_route,
        "plan": cmd_plan,
        "status": cmd_status,
        "ledger": cmd_ledger,
        "caps": cmd_caps,
    }
    try:
        return handlers[args.command](args, config)
    except CapExceeded as exc:
        print(f"spendrouter: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ConfigError as exc:
        print(f"spendrouter: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
