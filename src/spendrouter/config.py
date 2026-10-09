"""Configuration loading for spendrouter.

One file configures both layers. ``spendrouter.yml`` is the canonical form,
read with the stdlib-only mini-YAML parser; a 0.1 ``spendrouter.toml`` (or
any non-YAML path, which 0.1 read as TOML) still loads unchanged. Shape:

    db_path: ~/.local/share/spendrouter/ledger.sqlite3   # one ledger, both layers

    # ROUTE: which tier pays for a call, and the per-project hard caps
    default_project: default
    quota_unit_value_usd: 0.02
    models:
      claude-opus-5:
        subscription: {quota_per_1k: 4.0}
        deferred: {usd_per_mtok: 1.20}
        payg: {usd_per_mtok: 15.0}
    projects:
      default:
        allowed_models: [claude-opus-5, glm-5.3-flash]
        tier_order: [subscription, deferred, payg, local]
        hourly_usd: 5.0
        weekly_usd: 40.0
        hourly_quota: 500.0
        weekly_quota: 4000.0
        deferred_budget_usd: 25.0
        peak_hours: [9, 10, 11, 12, 13, 14, 15, 16, 17]
        peak_multiplier: 3.5
        deferred_windows: ["00:00-06:00"]
        deferred_multiplier: 0.5
        quota_unit_value_usd: 0.02

    # CONTAIN: the `spendrouter serve` proxy — see config_contain.py
    proxy: {listen: "127.0.0.1:8787"}
    upstreams / breaker / budgets / pricing / hooks / timezone

Every limit is optional; ``null``/absent means "no cap for this dimension".
"""

from __future__ import annotations

import sys

# tomllib is stdlib only from 3.11. Fail loudly and usefully rather than letting
# the tomllib import below raise a bare ModuleNotFoundError somewhere confusing —
# and note that an old interpreter also comes with an old pip that cannot do
# PEP 660 editable installs, which is the other half of the trap.
if sys.version_info < (3, 11):  # pragma: no cover - depends on the interpreter
    raise RuntimeError(
        f"spendrouter needs Python 3.11 or newer; this is {sys.version.split()[0]}"
        f" ({sys.executable}).\n"
        "Create the environment with a 3.11+ interpreter, e.g.:\n"
        "    python3.13 -m venv .venv && .venv/bin/pip install -e .\n"
        "On macOS, `python3` is often the 3.9 system build; pick a versioned name."
    )

import json
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import miniyaml
from .config_contain import CONTAIN_KEYS, ContainConfig, parse_contain
from .errors import ConfigError

TIERS = ("subscription", "deferred", "payg", "local")

DEFAULT_TIER_ORDER = list(TIERS)

DEFAULT_DB_PATH = "~/.local/share/spendrouter/ledger.sqlite3"

CONFIG_ENV = "SPENDROUTER_CONFIG"

# Looked for in the working directory, in this order, when no path is given.
# The TOML name is the 0.1 config: it still loads, so existing setups keep
# routing after an upgrade.
DEFAULT_CONFIG_NAMES = ("spendrouter.yml", "spendrouter.yaml", "spendrouter.toml")

ROUTE_KEYS = ("default_project", "quota_unit_value_usd", "db_path", "models", "projects")


@dataclass(frozen=True)
class TierOffer:
    """One way of paying for a model: how it is priced on a given tier."""

    tier: str
    quota_per_1k: float | None = None
    usd_per_mtok: float | None = None


@dataclass(frozen=True)
class ModelSpec:
    name: str
    offers: dict[str, TierOffer] = field(default_factory=dict)

    def offer(self, tier: str) -> TierOffer | None:
        return self.offers.get(tier)


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    allowed_models: tuple[str, ...] | None = None
    tier_order: tuple[str, ...] = tuple(DEFAULT_TIER_ORDER)
    hourly_usd: float | None = None
    weekly_usd: float | None = None
    hourly_quota: float | None = None
    weekly_quota: float | None = None
    deferred_budget_usd: float | None = None
    peak_hours: tuple[int, ...] = ()
    peak_multiplier: float = 1.0
    deferred_windows: tuple[str, ...] = ()
    deferred_multiplier: float = 1.0
    quota_unit_value_usd: float = 0.02


def _default_contain() -> ContainConfig:
    return parse_contain({}, db_path=DEFAULT_DB_PATH)


@dataclass(frozen=True)
class Config:
    projects: dict[str, ProjectConfig]
    models: dict[str, ModelSpec]
    default_project: str = "default"
    quota_unit_value_usd: float = 0.02
    db_path: str = DEFAULT_DB_PATH
    source_path: str | None = None
    contain: ContainConfig = field(default_factory=_default_contain)

    def project(self, name: str | None = None) -> ProjectConfig:
        key = name or self.default_project
        try:
            return self.projects[key]
        except KeyError:
            if not self.projects:
                # A contain-only file (just a proxy setup) loads fine; say what
                # the route verbs need instead of "unknown project 'default'".
                where = self.source_path or "the config"
                raise ConfigError(
                    f"no projects configured in {where}; add a projects section to use the route verbs"
                ) from None
            known = ", ".join(sorted(self.projects)) or "(none)"
            raise ConfigError(f"unknown project {key!r}; configured: {known}") from None

    def model(self, name: str) -> ModelSpec:
        try:
            return self.models[name]
        except KeyError:
            known = ", ".join(sorted(self.models)) or "(none)"
            raise ConfigError(f"unknown model {name!r}; configured: {known}") from None


def _as_float(value: Any, *, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _as_tuple_str(value: Any, *, where: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
    return tuple(value)


def _flatten_dotted_keys(raw: dict) -> dict:
    """Merge TOML's split-on-dots tables back into single keys.

    A model named ``glm-5.3-flash`` unquoted becomes ``{"glm-5": {"3-flash": {...}}}``
    — valid TOML, and a baffling "unknown tier" error if taken literally. Rejoin
    the parts so the user gets a working model instead of a puzzle.

    Only a genuine dot-split is merged: every value must itself be a table, and
    none of the inner keys may be a tier name. A real model's values *are* tier
    tables (so it is left alone), and a project's values include lists and
    numbers (so projects are never touched).
    """

    merged: dict = {}
    for key, value in raw.items():
        split = (
            isinstance(value, dict)
            and value
            and all(isinstance(v, dict) for v in value.values())
            and not (set(value) & set(TIERS))
        )
        if split:
            for sub_key, sub_value in _flatten_dotted_keys(value).items():
                merged[f"{key}.{sub_key}"] = sub_value
        else:
            merged[key] = value
    return merged


def _parse_model(name: str, raw: Any) -> ModelSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"models.{name}: expected a table")
    offers: dict[str, TierOffer] = {}
    for tier, spec in raw.items():
        if tier not in TIERS:
            raise ConfigError(
                f"models.{name}.{tier}: unknown tier (expected one of {', '.join(TIERS)})"
            )
        if spec is None or spec is False:
            continue
        if spec is True:
            # Shorthand: `local = true` / `deferred = true` means "this tier is
            # on, priced by default" — only meaningful for local, which has no
            # rate to declare.
            if tier != "local":
                raise ConfigError(
                    f"models.{name}.{tier}: `= true` is only valid for local; "
                    "give an explicit rate table instead"
                )
            offers[tier] = TierOffer(tier=tier)
            continue
        if not isinstance(spec, dict):
            raise ConfigError(f"models.{name}.{tier}: expected a table")
        offers[tier] = TierOffer(
            tier=tier,
            quota_per_1k=_as_float(spec.get("quota_per_1k"), where=f"models.{name}.{tier}.quota_per_1k"),
            usd_per_mtok=_as_float(spec.get("usd_per_mtok"), where=f"models.{name}.{tier}.usd_per_mtok"),
        )
    if not offers:
        raise ConfigError(f"models.{name}: no priced tiers (every tier was null/false)")
    return ModelSpec(name=name, offers=offers)


def _parse_project(name: str, raw: Any, default_quota_value: float) -> ProjectConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"projects.{name}: expected a table")
    tier_order = _as_tuple_str(raw.get("tier_order"), where=f"projects.{name}.tier_order") or tuple(
        DEFAULT_TIER_ORDER
    )
    for tier in tier_order:
        if tier not in TIERS:
            raise ConfigError(
                f"projects.{name}.tier_order: unknown tier {tier!r} (expected {', '.join(TIERS)})"
            )
    peak_hours_raw = raw.get("peak_hours") or []
    if not isinstance(peak_hours_raw, list) or not all(
        isinstance(h, int) and 0 <= h <= 23 for h in peak_hours_raw
    ):
        raise ConfigError(f"projects.{name}.peak_hours: expected a list of hours 0-23")
    windows = _as_tuple_str(raw.get("deferred_windows"), where=f"projects.{name}.deferred_windows") or ()
    for window in windows:
        try:
            start, end = window.split("-")
            for part in (start, end):
                hh, mm = part.split(":")
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    raise ValueError
        except ValueError:
            raise ConfigError(
                f"projects.{name}.deferred_windows: {window!r} is not HH:MM-HH:MM"
            ) from None
    quota_value = _as_float(
        raw.get("quota_unit_value_usd"), where=f"projects.{name}.quota_unit_value_usd"
    )
    return ProjectConfig(
        name=name,
        allowed_models=_as_tuple_str(raw.get("allowed_models"), where=f"projects.{name}.allowed_models"),
        tier_order=tier_order,
        hourly_usd=_as_float(raw.get("hourly_usd"), where=f"projects.{name}.hourly_usd"),
        weekly_usd=_as_float(raw.get("weekly_usd"), where=f"projects.{name}.weekly_usd"),
        hourly_quota=_as_float(raw.get("hourly_quota"), where=f"projects.{name}.hourly_quota"),
        weekly_quota=_as_float(raw.get("weekly_quota"), where=f"projects.{name}.weekly_quota"),
        deferred_budget_usd=_as_float(
            raw.get("deferred_budget_usd"), where=f"projects.{name}.deferred_budget_usd"
        ),
        peak_hours=tuple(peak_hours_raw),
        peak_multiplier=_as_float(raw.get("peak_multiplier"), where=f"projects.{name}.peak_multiplier")
        or 1.0,
        deferred_windows=windows,
        deferred_multiplier=_as_float(
            raw.get("deferred_multiplier"), where=f"projects.{name}.deferred_multiplier"
        )
        or 1.0,
        quota_unit_value_usd=default_quota_value if quota_value is None else quota_value,
    )


def find_config(path: str | None = None) -> str | None:
    """The file to read: ``path``, else $SPENDROUTER_CONFIG, else the first of
    DEFAULT_CONFIG_NAMES in the working directory; None when there is none."""

    explicit = path or os.environ.get(CONFIG_ENV)
    if explicit:
        expanded = os.path.expanduser(explicit)
        if not os.path.isfile(expanded):
            raise ConfigError(f"config file not found: {expanded} (write one, or set {CONFIG_ENV})")
        return expanded
    return next((name for name in DEFAULT_CONFIG_NAMES if os.path.isfile(name)), None)


def load_config(path: str | None = None, *, required: bool = True) -> Config:
    """Load a config file. ``path`` defaults to $SPENDROUTER_CONFIG, then
    ./spendrouter.yml, then the 0.1 ./spendrouter.toml.

    ``required=False`` is for the contain verbs: with no file at all the proxy
    runs on built-in defaults (openai + anthropic upstreams, no budgets). The
    route verbs always need one — they cannot invent your plan's prices.
    """

    found = find_config(path)
    if found is None:
        if required:
            raise ConfigError(
                f"config file not found: no {' / '.join(DEFAULT_CONFIG_NAMES)} in {os.getcwd()} "
                f"(write one with `spendrouter init`, or set {CONFIG_ENV})"
            )
        return parse_config({})
    raw, from_toml = _read(found)
    return parse_config(raw, source_path=found, from_toml=from_toml)


def _read(path: str) -> tuple[dict, bool]:
    """Decode a config file by its extension; returns (data, read_as_toml)."""

    suffix = os.path.splitext(path)[1].lower()
    if suffix not in (".yml", ".yaml", ".json"):
        # 0.1 read every path as TOML whatever its name, so anything that is
        # not explicitly YAML or JSON still is.
        with open(path, "rb") as handle:
            try:
                return tomllib.load(handle), True
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    with open(path, "rb") as handle:
        data = handle.read()
    kind = "JSON" if suffix == ".json" else "YAML"
    try:
        text = data.decode("utf-8")
        raw = json.loads(text) if kind == "JSON" else miniyaml.loads(text)
    except ValueError as exc:  # YAMLError, bad JSON, or bytes that are not UTF-8
        raise ConfigError(f"{path}: invalid {kind}: {exc}") from None
    if raw is None:  # an empty or all-comment file
        return {}, False
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping of sections at the top level")
    return raw, False


def parse_config(raw: dict, *, source_path: str | None = None, from_toml: bool = False) -> Config:
    """Validate decoded config data — both layers — into a :class:`Config`.

    YAML and JSON reject unknown top-level keys: a misspelt ``budgets:`` must
    fail loudly, not leave every agent uncapped. TOML keeps 0.1's habit of
    ignoring them, so a config that routed before the upgrade still routes.
    """

    if not from_toml:
        unknown = sorted(set(raw) - set(ROUTE_KEYS) - set(CONTAIN_KEYS))
        if unknown:
            raise ConfigError(
                f"unknown key(s) {', '.join(unknown)} (allowed: {', '.join(ROUTE_KEYS + CONTAIN_KEYS)})"
            )
    # Only TOML splits a key like glm-5.3-flash on its dots; YAML keys are literal.
    rejoin = _flatten_dotted_keys if from_toml else dict

    default_quota_value = _as_float(
        raw.get("quota_unit_value_usd"), where="quota_unit_value_usd"
    )
    default_quota_value = 0.02 if default_quota_value is None else default_quota_value

    models_raw = raw.get("models") or {}
    if not isinstance(models_raw, dict):
        raise ConfigError("models: expected a table")
    models = {
        name: _parse_model(name, spec) for name, spec in rejoin(models_raw).items()
    }
    projects_raw = raw.get("projects") or {}
    if not isinstance(projects_raw, dict):
        raise ConfigError("projects: expected a table")
    projects = rejoin(projects_raw)
    projects = {
        # A bare `name:` in YAML is an empty project, as `[projects.name]` is in TOML.
        name: _parse_project(name, {} if spec is None else spec, default_quota_value)
        for name, spec in projects.items()
    }

    if not any(key in raw for key in ("models", "projects", "default_project")):
        # A contain-only file (just a proxy setup) is complete without route
        # sections; the route verbs say what is missing when they run.
        default_project = "default"
    else:
        default_project = raw.get("default_project")
        if default_project is None:
            # Fall back to a project literally named "default" if one exists,
            # otherwise the only project — a single-project config should not need
            # to name it twice.
            if "default" in projects:
                default_project = "default"
            elif len(projects) == 1:
                default_project = next(iter(projects))
            else:
                known = ", ".join(sorted(projects)) or "(none)"
                raise ConfigError(
                    "default_project is required when several projects are configured; "
                    f"configured: {known}"
                )
        if default_project not in projects:
            raise ConfigError(f"default_project {default_project!r} is not defined under [projects]")
        if not projects:
            projects = {default_project: ProjectConfig(name=default_project)}

    db_path = raw.get("db_path") or DEFAULT_DB_PATH
    if not isinstance(db_path, str):
        raise ConfigError("db_path: expected a string")

    return Config(
        projects=projects,
        models=models,
        default_project=default_project,
        quota_unit_value_usd=default_quota_value,
        db_path=db_path,
        source_path=source_path,
        contain=parse_contain({key: raw[key] for key in CONTAIN_KEYS if key in raw}, db_path=db_path),
    )
