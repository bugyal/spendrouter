"""The contain half of spendrouter.yml: proxy, upstreams, breaker, budgets, pricing, hooks.

config.py reads the file and hands the contain sections here. Validation is
strict on purpose: an unknown key is an error, not a warning. A misspelt
``budgets:`` or ``hard_usd`` would otherwise leave an agent running with no
cap while its owner believes there is one.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ConfigError
from .pricing import BUILTIN_PRICES, FALLBACK_PRICE, Price, PriceTable
from .toolscan import DEFAULT_ERROR_PATTERNS

__all__ = [
    "CONTAIN_KEYS",
    "BreakerConfig",
    "BudgetRule",
    "ContainConfig",
    "Hook",
    "ProxyConfig",
    "Upstream",
    "parse_contain",
]

# The top-level keys of spendrouter.yml that belong to the contain layer.
CONTAIN_KEYS = ("timezone", "proxy", "upstreams", "breaker", "budgets", "pricing", "hooks")

SCOPES = ("agent", "customer", "task", "global")
PERIODS = ("hour", "day", "week", "month", "total")
ACTIONS = ("refuse", "pause")
FORMATS = ("openai", "anthropic")
AUTH_STYLES = ("bearer", "x-api-key", "api-key")
EVENT_KINDS = (
    "soft_cap_exceeded",
    "hard_cap_exceeded",
    "breaker_tripped",
    "paused",
    "resumed",
    "credential_minted",
    "credential_revoked",
)

DEFAULT_UPSTREAMS: dict[str, dict[str, str]] = {
    "openai": {"base_url": "https://api.openai.com", "format": "openai", "api_key_env": "OPENAI_API_KEY", "auth": "bearer"},
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "format": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "auth": "x-api-key",
    },
}


@dataclass(frozen=True)
class Upstream:
    name: str
    base_url: str
    format: str = "openai"
    api_key_env: str = ""  # env var (in the daemon) holding the real provider key
    auth: str = "bearer"  # how that key is attached upstream


@dataclass(frozen=True)
class BudgetRule:
    name: str
    scope: str
    match: str
    period: str
    soft_usd: float | None
    hard_usd: float | None
    action: str = "refuse"

    def applies_to(self, value: str) -> bool:
        if self.scope == "global":
            return True
        return bool(value) and (self.match == "*" or self.match == value)


@dataclass(frozen=True)
class BreakerConfig:
    enabled: bool = True
    max_repeats: int = 10  # same tool + error class fed back this many times in a row is allowed; one more trips
    api_error_repeats: int = 20  # same upstream error class in a row, with no success between
    max_identical_requests: int = 10  # byte-identical request resent this many times is allowed
    window_seconds: float = 3600.0  # failures older than this are forgotten
    cooldown_seconds: float = 0.0  # 0 = stay paused until `spendrouter resume`
    retry_window_seconds: float = 300.0  # identical request within this window counts as a retry
    error_patterns: tuple[str, ...] = DEFAULT_ERROR_PATTERNS


@dataclass(frozen=True)
class ProxyConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    require_credential: bool = False
    inject_stream_usage: bool = True
    upstream_timeout: float = 600.0


@dataclass(frozen=True)
class Hook:
    events: tuple[str, ...] = ("*",)  # event kinds; "*" = all
    command: tuple[str, ...] = ()
    url: str = ""
    timeout: float = 10.0

    def wants(self, kind: str) -> bool:
        return "*" in self.events or kind in self.events


@dataclass(frozen=True)
class ContainConfig:
    # Shared with the route layer: proxied calls go into the same sqlite file
    # as routed ones, so one path (and one --db) moves the whole ledger.
    db_path: str
    timezone: str = "utc"
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    upstreams: dict[str, Upstream] = field(default_factory=dict)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    budgets: list[BudgetRule] = field(default_factory=list)
    pricing: PriceTable = field(default_factory=PriceTable)
    hooks: list[Hook] = field(default_factory=list)

    @property
    def events_path(self) -> Path:
        """The append-only event log, kept next to the ledger file."""
        return Path(os.path.expanduser(self.db_path)).parent / "events.jsonl"

    @property
    def proxy_url(self) -> str:
        host = self.proxy.host
        if host in ("0.0.0.0", "", "::"):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.proxy.port}"


# -- validation helpers ------------------------------------------------------


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping")
    return value


def _only(data: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(allowed)})")


def _number(value: Any, where: str, *, minimum: float = 0.0, allow_none: bool = True) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value!r}")
    return float(value)


def _int(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value!r}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _str(value: Any, where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConfigError(f"{where}: expected a string, got {value!r}")
    return str(value)


def _choice(value: Any, choices: tuple[str, ...], where: str) -> str:
    text = _str(value, where)
    if text not in choices:
        raise ConfigError(f"{where}: must be one of {', '.join(choices)}, got {text!r}")
    return text


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list of strings")
    return tuple(_str(v, f"{where}[{i}]") for i, v in enumerate(value))


# -- sections ----------------------------------------------------------------


def parse_contain(data: dict[str, Any], *, db_path: str) -> ContainConfig:
    """Validate the contain sections of a config file; the caller checks the top level."""
    return ContainConfig(
        db_path=db_path,
        timezone=_choice(data.get("timezone", "utc"), ("utc", "local"), "timezone"),
        proxy=_parse_proxy(_mapping(data.get("proxy"), "proxy")),
        upstreams=_parse_upstreams(data.get("upstreams")),
        breaker=_parse_breaker(_mapping(data.get("breaker"), "breaker")),
        budgets=_parse_budgets(data.get("budgets")),
        pricing=_parse_pricing(_mapping(data.get("pricing"), "pricing")),
        hooks=_parse_hooks(data.get("hooks")),
    )


def _parse_proxy(raw: dict[str, Any]) -> ProxyConfig:
    _only(raw, ("listen", "require_credential", "inject_stream_usage", "upstream_timeout"), "proxy")
    defaults = ProxyConfig()
    host, port = defaults.host, defaults.port
    if "listen" in raw:
        host, port = parse_listen(_str(raw["listen"], "proxy.listen"), defaults.host, "proxy.listen")
    return ProxyConfig(
        host=host,
        port=port,
        require_credential=_bool(raw.get("require_credential", defaults.require_credential), "proxy.require_credential"),
        inject_stream_usage=_bool(raw.get("inject_stream_usage", defaults.inject_stream_usage), "proxy.inject_stream_usage"),
        upstream_timeout=float(_number(raw.get("upstream_timeout", defaults.upstream_timeout), "proxy.upstream_timeout", minimum=1, allow_none=False)),  # type: ignore[arg-type]
    )


def parse_listen(listen: str, default_host: str, where: str) -> tuple[str, int]:
    """'host:port' (or '[::1]:port', or ':port') -> (host, port)."""
    host_part, sep, port_part = listen.rpartition(":")
    if not sep or not port_part.isdigit():
        raise ConfigError(f"{where}: expected host:port, got {listen!r}")
    port = int(port_part)
    if not 0 <= port <= 65535:
        raise ConfigError(f"{where}: port out of range: {port}")
    return host_part.strip("[]") or default_host, port


def _parse_upstreams(raw: Any) -> dict[str, Upstream]:
    merged: dict[str, dict[str, Any] | None] = {k: dict(v) for k, v in DEFAULT_UPSTREAMS.items()}
    for name, spec in _mapping(raw, "upstreams").items():
        if spec is None:  # "openai: null" removes a built-in upstream
            merged[name] = None
            continue
        where = f"upstreams.{name}"
        spec = _mapping(spec, where)
        _only(spec, ("base_url", "format", "api_key_env", "auth"), where)
        merged[name] = {**(merged.get(name) or {}), **spec}
    out: dict[str, Upstream] = {}
    for name, spec in merged.items():
        if spec is None:
            continue
        where = f"upstreams.{name}"
        if not name or "/" in name or name.startswith("_"):
            raise ConfigError(f"{where}: upstream names must be non-empty, without '/', not starting with '_'")
        if "base_url" not in spec:
            raise ConfigError(f"{where}: base_url is required")
        base_url = _str(spec["base_url"], f"{where}.base_url").rstrip("/")
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ConfigError(f"{where}.base_url: expected an http(s) URL, got {base_url!r}")
        fmt = _choice(spec.get("format", "anthropic" if name == "anthropic" else "openai"), FORMATS, f"{where}.format")
        out[name] = Upstream(
            name=name,
            base_url=base_url,
            format=fmt,
            api_key_env=_str(spec.get("api_key_env") or "", f"{where}.api_key_env"),
            auth=_choice(spec.get("auth", "x-api-key" if fmt == "anthropic" else "bearer"), AUTH_STYLES, f"{where}.auth"),
        )
    return out


def _parse_breaker(raw: dict[str, Any]) -> BreakerConfig:
    allowed = (
        "enabled",
        "max_repeats",
        "api_error_repeats",
        "max_identical_requests",
        "window_seconds",
        "cooldown_seconds",
        "retry_window_seconds",
        "error_patterns",
    )
    _only(raw, allowed, "breaker")
    d = BreakerConfig()
    patterns = d.error_patterns
    if "error_patterns" in raw:
        patterns = _str_list(raw["error_patterns"], "breaker.error_patterns")
        try:
            for p in patterns:
                re.compile(p)
        except re.error as exc:
            raise ConfigError(f"breaker.error_patterns: invalid regex: {exc}") from None
    return BreakerConfig(
        enabled=_bool(raw.get("enabled", d.enabled), "breaker.enabled"),
        max_repeats=_int(raw.get("max_repeats", d.max_repeats), "breaker.max_repeats", minimum=1),
        api_error_repeats=_int(raw.get("api_error_repeats", d.api_error_repeats), "breaker.api_error_repeats", minimum=0),
        max_identical_requests=_int(raw.get("max_identical_requests", d.max_identical_requests), "breaker.max_identical_requests", minimum=0),
        window_seconds=float(_number(raw.get("window_seconds", d.window_seconds), "breaker.window_seconds", minimum=1, allow_none=False)),  # type: ignore[arg-type]
        cooldown_seconds=float(_number(raw.get("cooldown_seconds", d.cooldown_seconds), "breaker.cooldown_seconds", allow_none=False)),  # type: ignore[arg-type]
        retry_window_seconds=float(_number(raw.get("retry_window_seconds", d.retry_window_seconds), "breaker.retry_window_seconds", allow_none=False)),  # type: ignore[arg-type]
        error_patterns=patterns,
    )


def _parse_budgets(raw: Any) -> list[BudgetRule]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("budgets: expected a list of rules")
    rules: list[BudgetRule] = []
    names = set()
    for i, spec in enumerate(raw):
        where = f"budgets[{i}]"
        spec = _mapping(spec, where)
        _only(spec, ("name", "scope", "match", "period", "soft_usd", "hard_usd", "action"), where)
        if "scope" not in spec:
            raise ConfigError(f"{where}: scope is required ({', '.join(SCOPES)})")
        scope = _choice(spec["scope"], SCOPES, f"{where}.scope")
        if scope == "global":
            match = "*"
        elif "match" not in spec:
            raise ConfigError(f'{where}: match is required (a {scope} name, or "*" for each {scope} separately)')
        else:
            match = _str(spec["match"], f"{where}.match")
        period = _choice(spec.get("period", "day"), PERIODS, f"{where}.period")
        soft = _number(spec.get("soft_usd"), f"{where}.soft_usd")
        hard = _number(spec.get("hard_usd"), f"{where}.hard_usd")
        if soft is None and hard is None:
            raise ConfigError(f"{where}: set soft_usd, hard_usd, or both")
        if soft is not None and hard is not None and soft > hard:
            raise ConfigError(f"{where}: soft_usd ({soft}) is above hard_usd ({hard})")
        name = _str(spec.get("name") or f"{scope}:{match}:{period}", f"{where}.name")
        if name in names:
            raise ConfigError(f"{where}: duplicate rule name {name!r} (set a distinct name:)")
        names.add(name)
        rules.append(
            BudgetRule(
                name=name,
                scope=scope,
                match=match,
                period=period,
                soft_usd=soft,
                hard_usd=hard,
                action=_choice(spec.get("action", "refuse"), ACTIONS, f"{where}.action"),
            )
        )
    return rules


def _parse_price(spec: Any, where: str) -> Price:
    spec = _mapping(spec, where)
    _only(spec, ("input", "output", "cache_read", "cache_write"), where)
    for key in ("input", "output"):
        if key not in spec:
            raise ConfigError(f"{where}: {key} (USD per 1M tokens) is required")
    return Price(
        input=float(_number(spec["input"], f"{where}.input", allow_none=False)),  # type: ignore[arg-type]
        output=float(_number(spec["output"], f"{where}.output", allow_none=False)),  # type: ignore[arg-type]
        cache_read=_number(spec.get("cache_read"), f"{where}.cache_read"),
        cache_write=_number(spec.get("cache_write"), f"{where}.cache_write"),
    )


def _parse_pricing(raw: dict[str, Any]) -> PriceTable:
    prices = dict(BUILTIN_PRICES)
    fallback = FALLBACK_PRICE
    for model, spec in raw.items():
        if model == "default":
            fallback = _parse_price(spec, "pricing.default")
        else:
            prices[model.lower()] = _parse_price(spec, f"pricing.{model}")
    return PriceTable(prices, fallback)


def _parse_hooks(raw: Any) -> list[Hook]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("hooks: expected a list")
    hooks = []
    for i, spec in enumerate(raw):
        where = f"hooks[{i}]"
        spec = _mapping(spec, where)
        _only(spec, ("events", "command", "url", "timeout"), where)
        events = _str_list(spec.get("events", ["*"]), f"{where}.events")
        for kind in events:
            if kind != "*" and kind not in EVENT_KINDS:
                raise ConfigError(f"{where}.events: unknown event {kind!r} (events: {', '.join(EVENT_KINDS)})")
        command: tuple[str, ...] = ()
        if spec.get("command") is not None:
            if isinstance(spec["command"], str):
                command = tuple(shlex.split(spec["command"]))
            else:
                command = _str_list(spec["command"], f"{where}.command")
        url = _str(spec.get("url") or "", f"{where}.url")
        if url and urlsplit(url).scheme not in ("http", "https"):
            raise ConfigError(f"{where}.url: expected an http(s) URL")
        if not command and not url:
            raise ConfigError(f"{where}: set command, url, or both")
        hooks.append(
            Hook(
                events=events,
                command=command,
                url=url,
                timeout=float(_number(spec.get("timeout", 10.0), f"{where}.timeout", minimum=0.1, allow_none=False)),  # type: ignore[arg-type]
            )
        )
    return hooks
