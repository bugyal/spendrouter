"""The contain verbs: serve, run, creds, report, check, pause, resume, init.

cli.py owns the parser and the route verbs, and hands these subcommands
here. They read the contain sections of spendrouter.yml — and, unlike the
route verbs, also run with no file at all: the proxy then fronts the
built-in openai and anthropic upstreams with no budgets, which still
ledgers and breaker-checks every call. Route cannot invent your plan's
prices, so it keeps requiring a file.

Exit codes are shared with the route verbs so one harness can gate on both
layers: 0 ok · 1 error · 2 usage · 3 refused / hard cap reached · 4 config
error · 5 paused · 6 soft cap exceeded.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .config_contain import ContainConfig, parse_listen
from .contain import Attribution, Containment
from .credentials import Credential
from .errors import ConfigError
from .proxy import HEALTH_PATH, ProxyServer
from .report import EXIT_OK, GROUPS, ReportOptions, build_report, containment_status, render

__all__ = ["EXIT_ERROR", "EXIT_USAGE", "add_commands", "child_env", "parse_duration", "run"]

EXIT_ERROR, EXIT_USAGE, EXIT_CONFIG = 1, 2, 4

TEMPLATE_PATH = Path(__file__).with_name("spendrouter.example.yml")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class UsageError(Exception):
    pass


def run(args: argparse.Namespace) -> int:
    """Run the contain verb argparse selected (``args.contain``)."""
    try:
        if args.contain is cmd_init:  # it writes the config file, so it cannot need one
            return cmd_init(args)
        config = load_config(args.config, required=False)
        # --db moves the whole ledger, as it does for the route verbs.
        contain = replace(config.contain, db_path=args.db) if args.db else config.contain
        return int(args.contain(args, contain))
    except UsageError as exc:
        print(f"spendrouter: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ConfigError as exc:
        print(f"spendrouter: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


# -- helpers -----------------------------------------------------------------


def parse_duration(text: str) -> float:
    """'90s', '15m', '2h', '7d', '1w', or plain seconds."""
    match = _DURATION.match(text or "")
    if not match:
        raise UsageError(f"invalid duration {text!r} (examples: 90s, 15m, 2h, 7d)")
    return float(match.group(1)) * _UNITS[match.group(2)]


def parse_time(text: str | None, tz: str, now: float) -> float | None:
    """An ISO date/time (in the configured zone unless it says otherwise), or a duration ago."""
    if text is None:
        return None
    if _DURATION.match(text):
        return now - parse_duration(text)
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise UsageError(f"invalid time {text!r} (use 2026-10-01, 2026-10-01T09:30, or a duration like 24h)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc) if tz == "utc" else dt.astimezone()
    return dt.timestamp()


def _usd(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}" if value >= 0.01 or value == 0 else f"${value:.4f}"


def _when(ts: float | None) -> str:
    if ts is None:
        return "never"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _scope(args: argparse.Namespace) -> tuple[str, str]:
    if getattr(args, "all", False):
        return "global", "*"
    for scope in ("agent", "customer", "task"):
        value = getattr(args, scope, None)
        if value:
            return scope, value
    raise UsageError("give one of --agent, --customer, --task or --all")


def _print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    for row in [headers] + rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())


# -- commands ----------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        print(f"spendrouter: {path} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_ERROR
    path.write_text(TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {path}")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace, contain: ContainConfig) -> int:
    host, port = contain.proxy.host, contain.proxy.port
    if args.listen:
        try:
            host, port = parse_listen(args.listen, host, "--listen")
        except ConfigError as exc:
            raise UsageError(str(exc)) from None
    engine = Containment(contain)
    try:
        server = ProxyServer(engine, host, port, quiet=args.quiet)
    except OSError as exc:  # port in use, address not on this host
        engine.close()
        print(f"spendrouter: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"spendrouter {__version__} proxy listening on {server.url}", file=sys.stderr)
    print(f"  ledger: {contain.db_path}", file=sys.stderr)
    for name, up in contain.upstreams.items():
        if up.api_key_env and os.environ.get(up.api_key_env):
            key = f"credentials use ${up.api_key_env}"
        elif up.api_key_env:
            key = f"${up.api_key_env} unset: sr_ credentials get 502, passthrough keys still work"
        else:
            key = "passthrough keys only"
        print(f"  {server.url}/{name} -> {up.base_url}  [{up.format}; {key}]", file=sys.stderr)

    def stop(signum: int, frame: Any) -> None:
        # shutdown() waits for serve_forever to return, so it cannot run on
        # the thread serve_forever is blocking.
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.server_close()
        engine.close()
    return EXIT_OK


def cmd_report(args: argparse.Namespace, contain: ContainConfig) -> int:
    by = tuple(b.strip() for b in args.by.split(",") if b.strip())
    unknown = [b for b in by if b not in GROUPS and b != "call"]
    if unknown or not by or ("call" in by and len(by) > 1):
        raise UsageError(f"--by takes 'call' or a comma list of: {', '.join(GROUPS)}")
    with Containment(contain) as engine:
        now = engine.clock()
        opts = ReportOptions(
            period=args.period,
            since=parse_time(args.since, contain.timezone, now),
            until=parse_time(args.until, contain.timezone, now),
            by=by,
            agent=args.agent,
            customer=args.customer,
            task=args.task,
            run=args.run,
            limit=args.limit,
        )
        report = build_report(engine, opts)
    print(render(report, args.format))
    return EXIT_OK if args.exit_zero else report["status"]


def cmd_check(args: argparse.Namespace, contain: ContainConfig) -> int:
    if not (args.agent or args.customer or args.task):
        raise UsageError("give at least one of --agent, --customer, --task")
    with Containment(contain) as engine:
        attr = Attribution(agent=args.agent or "", task=args.task or "", customer=args.customer or "")
        pause = engine.active_pause(attr)
        states = engine.budget_states(attr)
    status = containment_status(states, [pause] if pause else [])
    if not args.quiet:
        if pause is not None:
            target = "everything" if pause.scope == "global" else f"{pause.scope}={pause.value}"
            print(f"PAUSED  {target} — {pause.reason}")
        for s in states:
            if s.level != "ok":
                limit = s.rule.hard_usd if s.level == "hard" else s.rule.soft_usd
                print(f"{s.level.upper():<6}  {s.label} spent {_usd(s.spend)} this {s.rule.period} (cap {_usd(limit)}, {s.rule.name})")
        if status == EXIT_OK:
            print("OK")
    return status


def cmd_pause(args: argparse.Namespace, contain: ContainConfig) -> int:
    scope, value = _scope(args)
    with Containment(contain) as engine:
        until = engine.clock() + parse_duration(args.duration) if args.duration else None
        engine.pause(scope, value, reason=args.reason, until=until)
    target = "everything" if scope == "global" else f"{scope} {value!r}"
    print(f"paused {target}" + (f" until {_when(until)}" if until else " until resumed"))
    return EXIT_OK


def cmd_resume(args: argparse.Namespace, contain: ContainConfig) -> int:
    scope, value = _scope(args)
    target = "everything" if scope == "global" else f"{scope} {value!r}"
    with Containment(contain) as engine:
        resumed = engine.resume(scope, value)
    if resumed:
        print(f"resumed {target}")
        return EXIT_OK
    print(f"spendrouter: {target} was not paused", file=sys.stderr)
    return EXIT_ERROR


def _mint(engine: Containment, args: argparse.Namespace, note: str = "") -> tuple[str, Credential]:
    try:
        return engine.creds.mint(
            agent=args.agent,
            task=args.task or "",
            customer=args.customer or "",
            run=args.run or "",
            ttl_seconds=parse_duration(args.ttl) if args.ttl else None,
            max_usd=args.max_usd,
            upstreams=args.upstream or (),
            note=note or (args.note or ""),
        )
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def cmd_creds_mint(args: argparse.Namespace, contain: ContainConfig) -> int:
    with Containment(contain) as engine:
        token, cred = _mint(engine, args)
    # The token goes to stdout alone, so TOKEN=$(spendrouter creds mint ...) works.
    print(token)
    expiry = f"expires {_when(cred.expires_at)}" if cred.expires_at else "no expiry"
    print(f"spendrouter: minted {cred.id} for agent {cred.agent!r} ({expiry}); the token is shown once", file=sys.stderr)
    return EXIT_OK


def cmd_creds_list(args: argparse.Namespace, contain: ContainConfig) -> int:
    rows = []
    with Containment(contain) as engine:
        now = engine.clock()
        for c in engine.creds.list(include_inactive=args.all):
            calls, spent = engine.creds.stats(c.id)
            state = "revoked" if c.revoked_at else ("expired" if not c.active(now) else "active")
            rows.append(
                [c.id, c.agent, c.task or "-", c.customer or "-", _when(c.expires_at), _usd(c.max_usd), _usd(spent), str(calls), state]
            )
    if not rows:
        print("(no credentials)" if args.all else "(no active credentials; --all shows expired and revoked ones)")
    else:
        _print_table(rows, ["id", "agent", "task", "customer", "expires", "max", "spent", "calls", "status"])
    return EXIT_OK


def cmd_creds_revoke(args: argparse.Namespace, contain: ContainConfig) -> int:
    if not (args.ids or args.agent or args.task):
        raise UsageError("give credential ids, --agent or --task")
    with Containment(contain) as engine:
        revoked = engine.creds.revoke(ids=args.ids, agent=args.agent, task=args.task)
    for cred_id in revoked:
        print(f"revoked {cred_id}")
    if not revoked:
        print("spendrouter: no active credentials matched", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_creds_gc(args: argparse.Namespace, contain: ContainConfig) -> int:
    older_than = parse_duration(args.older_than)
    with Containment(contain) as engine:
        removed = engine.creds.gc(older_than)
    print(f"removed {removed} expired or revoked credential(s)")
    return EXIT_OK


def _proxy_alive(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost via HTTP_PROXY
    try:
        with opener.open(url + HEALTH_PATH, timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def child_env(
    contain: ContainConfig, proxy_url: str, token: str, cred: Credential, base: dict[str, str] | None = None
) -> dict[str, str]:
    """Environment for a contained child: proxy base URLs, and the credential in place of every provider key."""
    env = dict(os.environ if base is None else base)
    for name, up in contain.upstreams.items():
        url = f"{proxy_url}/{name}"
        if up.api_key_env:
            env[up.api_key_env] = token  # the real key never reaches the agent
        env[f"SPENDROUTER_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}_BASE_URL"] = url
        if name == "openai":
            env["OPENAI_BASE_URL"] = url + "/v1"
        elif name == "anthropic":
            env["ANTHROPIC_BASE_URL"] = url
    # The Anthropic SDK sends ANTHROPIC_AUTH_TOKEN as a bearer token; a real
    # one left in the child's environment is a provider key the agent holds.
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # An HTTP(S)_PROXY inherited from the shell must not capture calls to the local proxy.
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    hosts = [h for h in no_proxy.split(",") if h]
    for host in ("127.0.0.1", "localhost"):
        if host not in hosts:
            hosts.append(host)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(hosts)
    env.update(
        SPENDROUTER_PROXY=proxy_url,
        SPENDROUTER_KEY=token,
        SPENDROUTER_CREDENTIAL_ID=cred.id,
        SPENDROUTER_AGENT=cred.agent,
        SPENDROUTER_TASK=cred.task,
        SPENDROUTER_CUSTOMER=cred.customer,
        SPENDROUTER_RUN=cred.run or cred.id,
    )
    return env


def cmd_run(args: argparse.Namespace, contain: ContainConfig) -> int:
    command = list(args.cmd)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise UsageError("nothing to run: spendrouter run --agent NAME [options] -- COMMAND [ARGS...]")
    proxy_url = (args.proxy or contain.proxy_url).rstrip("/")
    if not args.no_check and not _proxy_alive(proxy_url):
        print(f"spendrouter: no proxy answering at {proxy_url} — start one with `spendrouter serve` (or pass --proxy)", file=sys.stderr)
        return EXIT_ERROR
    engine = Containment(contain)
    try:
        token, cred = _mint(engine, args, note=("spendrouter run: " + " ".join(command))[:200])
    except BaseException:
        engine.close()
        raise
    print(
        f"spendrouter: run {cred.run or cred.id} · agent={cred.agent} · credential {cred.id} (expires {_when(cred.expires_at)})",
        file=sys.stderr,
    )
    code = 127
    try:
        try:
            proc = subprocess.Popen(command, env=child_env(contain, proxy_url, token, cred))
        except OSError as exc:
            print(f"spendrouter: cannot run {command[0]!r}: {exc}", file=sys.stderr)
        else:

            def forward(signum: int, frame: Any) -> None:
                proc.send_signal(signum)

            handlers = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                code = proc.wait()
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
    finally:
        engine.creds.revoke(ids=[cred.id])  # the finished task leaves nothing reusable
        calls, spent = engine.creds.stats(cred.id)
        print(f"spendrouter: run finished (exit {code}) · {calls} call(s) · {_usd(spent)} · {cred.id} revoked", file=sys.stderr)
        engine.close()
    return code if code >= 0 else 128 - code


# -- argument parsing ----------------------------------------------------------


def _scope_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agent", required=True)
    p.add_argument("--task")
    p.add_argument("--customer")
    p.add_argument("--run", help="run id for per-run rollups (default: the credential id)")
    p.add_argument("--max-usd", type=float, help="the credential stops working after this much spend")
    p.add_argument("--upstream", action="append", help="limit to this upstream (repeatable)")


def add_commands(sub: Any) -> None:
    """Register the contain verbs on cli.py's subcommand parsers (``args.contain`` = handler)."""
    p = sub.add_parser(
        "serve",
        help="run the metering proxy between agents and providers (foreground)",
        description="Reverse proxy on the agent -> LLM API boundary. Point an SDK's base URL at "
        "http://HOST:PORT/<upstream> (/openai/v1, /anthropic). Every call is attributed, checked against "
        "pauses, the loop circuit breaker and budget caps, forwarded, metered and ledgered.",
    )
    p.add_argument("--listen", metavar="HOST:PORT", help="address to listen on (default: proxy.listen from the config, else 127.0.0.1:8787)")
    p.add_argument("--quiet", action="store_true", help="no per-call log lines")
    p.set_defaults(contain=cmd_serve)

    p = sub.add_parser(
        "run",
        help="run a command with a credential minted for it and revoked when it exits",
        description="Mint an expiring sr_ credential, run COMMAND with provider base URLs pointed at the proxy "
        "and the credential in place of every provider key, then revoke the credential. Exits with COMMAND's code.",
    )
    _scope_args(p)
    p.add_argument("--ttl", default="12h", help="backstop expiry if spendrouter itself dies (default: 12h)")
    p.add_argument("--proxy", help="proxy URL (default: from proxy.listen)")
    p.add_argument("--no-check", action="store_true", help="do not check that the proxy is up first")
    p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- COMMAND [ARGS...]")
    p.set_defaults(contain=cmd_run)

    creds = sub.add_parser("creds", help="scoped, expiring sr_ credentials: mint | list | revoke | gc")
    csub = creds.add_subparsers(dest="creds_command", metavar="ACTION")

    def creds_help(args: argparse.Namespace, contain: ContainConfig) -> int:
        creds.print_help(sys.stderr)
        return EXIT_USAGE

    creds.set_defaults(contain=creds_help)
    p = csub.add_parser("mint", help="mint a credential; prints the token once")
    _scope_args(p)
    p.add_argument("--ttl", help="expire after this long (e.g. 2h); omit for a standing per-agent key")
    p.add_argument("--note")
    p.set_defaults(contain=cmd_creds_mint)
    p = csub.add_parser("list", help="list credentials with their calls and spend")
    p.add_argument("--all", action="store_true", help="include expired and revoked ones")
    p.set_defaults(contain=cmd_creds_list)
    p = csub.add_parser("revoke", help="revoke by id, or every credential of an agent or task")
    p.add_argument("ids", nargs="*")
    p.add_argument("--agent")
    p.add_argument("--task")
    p.set_defaults(contain=cmd_creds_revoke)
    p = csub.add_parser("gc", help="delete expired and revoked credentials")
    p.add_argument("--older-than", default="0s", help="only those dead for longer than this (default: 0s)")
    p.set_defaults(contain=cmd_creds_gc)

    p = sub.add_parser("report", help="proxied spend by customer / agent / task; exit code = containment state")
    p.add_argument("--period", choices=("hour", "day", "week", "month", "total"), default="day", help="current calendar period (default: day)")
    p.add_argument("--since", help="start: ISO date/time, or a duration ago (24h, 7d); overrides --period")
    p.add_argument("--until", help="end: ISO date/time, or a duration ago")
    p.add_argument("--by", default="customer,agent", help=f"group by a comma list of {', '.join(GROUPS)}; or 'call' for per-call rows")
    for name in ("agent", "customer", "task", "run"):
        p.add_argument(f"--{name}", help=f"only this {name}")
    p.add_argument("--limit", type=int, default=50, help="max rows (default: 50)")
    p.add_argument("--format", choices=("text", "md", "json"), default="text")
    p.add_argument("--json", dest="format", action="store_const", const="json", help="same as --format json")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0 (just print the report)")
    p.set_defaults(contain=cmd_report)

    p = sub.add_parser("check", help="gate a run: exit 0 ok, 3 hard cap, 5 paused, 6 soft cap")
    p.add_argument("--agent")
    p.add_argument("--customer")
    p.add_argument("--task")
    p.add_argument("--quiet", action="store_true", help="exit code only")
    p.set_defaults(contain=cmd_check)

    for name, helptext in (("pause", "pause an agent, customer, task or everything behind the proxy"), ("resume", "lift a pause")):
        p = sub.add_parser(name, help=helptext)
        target = p.add_mutually_exclusive_group(required=True)
        target.add_argument("--agent")
        target.add_argument("--customer")
        target.add_argument("--task")
        target.add_argument("--all", action="store_true", help="every call through spendrouter")
        if name == "pause":
            p.add_argument("--reason", default="paused by operator")
            p.add_argument("--for", dest="duration", help="auto-resume after this long (e.g. 30m)")
            p.set_defaults(contain=cmd_pause)
        else:
            p.set_defaults(contain=cmd_resume)

    p = sub.add_parser("init", help="write a commented spendrouter.yml covering both layers")
    p.add_argument("path", nargs="?", default="spendrouter.yml")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(contain=cmd_init)
