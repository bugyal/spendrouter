"""The policy engine: pick the cheapest tier that will not break a hard cap.

Order of operations for one call:

1. Work out what each *available* tier would cost for the estimated tokens.
   Availability is not just "does the model offer this tier" — deferred spend is
   unavailable outside its window, subscription quota can be exhausted, and the
   deferred budget can be spent down.
2. Sort the available tiers by effective cost (ties broken by the project's
   declared ``tier_order``).
3. Walk that list cheapest-first and take the first tier whose projected spend
   stays inside every hard cap. This is what stops a runaway job *before* it
   bills: if even the cheapest tier would break a cap, the call is refused.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from .config import Config, ModelSpec, ProjectConfig
from .ledger import Ledger, Usage
from .schedule import ScheduleState, evaluate

MILLION = 1_000_000.0
THOUSAND = 1_000.0


class CapExceeded(RuntimeError):
    """Raised by :func:`enforce` when a call would break a hard cap."""


@dataclass(frozen=True)
class CapCheck:
    name: str
    limit: float
    current: float
    projected: float

    @property
    def headroom(self) -> float:
        return self.limit - self.current

    @property
    def breached(self) -> bool:
        return self.projected > self.limit + 1e-9

    def describe(self) -> str:
        pct = 0.0 if self.limit <= 0 else 100.0 * self.current / self.limit
        return (
            f"{self.name}: {self.current:.2f}/{self.limit:.2f} used ({pct:.0f}%), "
            f"this call +{self.projected - self.current:.2f}"
        )


@dataclass
class TierQuote:
    tier: str
    available: bool
    effective_usd: float
    quota_units: float
    usd: float
    reason: str
    multiplier: float = 1.0

    def describe(self) -> str:
        if not self.available:
            return f"{self.tier:<13} unavailable — {self.reason}"
        return (
            f"{self.tier:<13} ${self.effective_usd:>8.4f} effective"
            f"  ({self.quota_units:>7.2f} quota, ${self.usd:.4f} cash)"
            + (f"  x{self.multiplier:g}" if self.multiplier != 1.0 else "")
        )


@dataclass
class Decision:
    """What the router decided for one call."""

    project: str
    model: str
    allowed: bool
    tier: str | None
    tokens_in: int
    tokens_out: int
    effective_usd: float = 0.0
    quota_units: float = 0.0
    usd: float = 0.0
    multiplier: float = 1.0
    reason: str = ""
    quotes: list[TierQuote] = field(default_factory=list)
    caps: list[CapCheck] = field(default_factory=list)
    schedule: ScheduleState | None = None
    usage: Usage | None = None

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def _estimate_tokens(tokens: int | None, tokens_in: int | None, tokens_out: int | None) -> tuple[int, int]:
    if tokens_in is not None or tokens_out is not None:
        return int(tokens_in or 0), int(tokens_out or 0)
    total = int(tokens or 0)
    # Default 70/30 in/out split when only a single token count is supplied;
    # output tokens are the expensive side on every host, so bias low rather
    # than pretending they are free.
    return int(total * 0.7), total - int(total * 0.7)


def quote_tiers(
    config: Config,
    project: ProjectConfig,
    model: ModelSpec,
    tokens_in: int,
    tokens_out: int,
    state: ScheduleState,
    usage: Usage,
) -> list[TierQuote]:
    """Price every tier the model offers, marking unavailability with a reason."""

    quotes: list[TierQuote] = []
    quota_value = project.quota_unit_value_usd
    for tier in project.tier_order:
        offer = model.offer(tier)
        if offer is None:
            continue
        if tier == "local":
            quotes.append(
                TierQuote(
                    tier=tier,
                    available=True,
                    effective_usd=0.0,
                    quota_units=0.0,
                    usd=0.0,
                    reason="on-box inference costs no marginal money",
                )
            )
            continue
        if tier == "subscription":
            if offer.quota_per_1k is None:
                quotes.append(
                    TierQuote(tier, False, 0.0, 0.0, 0.0, "no quota_per_1k rate configured")
                )
                continue
            units = (tokens_in + tokens_out) / THOUSAND * offer.quota_per_1k * state.quota_multiplier
            exhausted = project.weekly_quota is not None and usage.week_quota >= project.weekly_quota
            reason = (
                "weekly subscription quota is spent"
                if exhausted
                else "peak multiplier applied" if state.is_peak else "quota remaining"
            )
            quotes.append(
                TierQuote(
                    tier=tier,
                    available=not exhausted,
                    effective_usd=units * quota_value,
                    quota_units=units,
                    usd=0.0,
                    reason=reason,
                    multiplier=state.quota_multiplier,
                )
            )
            continue
        if tier == "deferred":
            if offer.usd_per_mtok is None:
                quotes.append(
                    TierQuote(tier, False, 0.0, 0.0, 0.0, "no usd_per_mtok rate configured")
                )
                continue
            raw = (tokens_in + tokens_out) / MILLION * offer.usd_per_mtok
            if not state.in_deferred_window:
                quotes.append(
                    TierQuote(tier, False, 0.0, 0.0, 0.0, "outside every deferred window")
                )
                continue
            budget_spent = (
                project.deferred_budget_usd is not None
                and usage.week_deferred_usd >= project.deferred_budget_usd
            )
            usd = raw * state.deferred_discount
            quotes.append(
                TierQuote(
                    tier=tier,
                    available=not budget_spent,
                    effective_usd=usd,
                    quota_units=0.0,
                    usd=usd,
                    reason=(
                        "deferred budget for this week is spent"
                        if budget_spent
                        else f"deferred window open, discount x{state.deferred_discount:g}"
                    ),
                    multiplier=state.deferred_discount,
                )
            )
            continue
        if tier == "payg":
            if offer.usd_per_mtok is None:
                quotes.append(TierQuote(tier, False, 0.0, 0.0, 0.0, "no usd_per_mtok rate configured"))
                continue
            usd = (tokens_in + tokens_out) / MILLION * offer.usd_per_mtok
            quotes.append(
                TierQuote(
                    tier=tier,
                    available=True,
                    effective_usd=usd,
                    quota_units=0.0,
                    usd=usd,
                    reason="metered, always available",
                )
            )
            continue
        quotes.append(TierQuote(tier, False, 0.0, 0.0, 0.0, "unsupported tier"))
    return quotes


def cap_checks(
    project: ProjectConfig,
    usage: Usage,
    *,
    quota_units: float,
    usd: float,
    tier: str,
    window: str,
) -> list[CapCheck]:
    """Cap checks for a candidate tier. ``window`` is 'hour' or 'week'.

    A dimension this call does not touch is skipped rather than blocked. A
    pay-as-you-go call burns no subscription quota, so an already-exhausted
    quota cap is not its problem — refusing it would starve the project of the
    one tier that cannot make the quota situation worse. Caps only stop work
    that *adds* to the constrained dimension.
    """

    checks: list[CapCheck] = []

    def guard(name: str, limit: float | None, current: float, contribution: float) -> None:
        if limit is None or contribution == 0:
            return
        checks.append(CapCheck(name, limit, current, current + contribution))

    if window == "hour":
        guard("hourly spend cap", project.hourly_usd, usage.hour_usd, usd)
        guard("hourly quota cap", project.hourly_quota, usage.hour_quota, quota_units)
        return checks
    guard("weekly spend cap", project.weekly_usd, usage.week_usd, usd)
    if tier == "deferred":
        guard(
            "weekly deferred budget",
            project.deferred_budget_usd,
            usage.week_deferred_usd,
            usd,
        )
    else:
        guard("weekly quota cap", project.weekly_quota, usage.week_quota, quota_units)
    return checks


def route(
    config: Config,
    *,
    project: str | None,
    model: str,
    tokens: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    when: _dt.datetime | None = None,
    ledger: Ledger | None = None,
    now: float | None = None,
) -> Decision:
    """Decide which tier should serve this call. Pure — writes nothing.

    Routing follows the project's declared ``tier_order`` rather than a raw
    dollar sort. That is deliberate: subscription quota is a *sunk* cost, so
    comparing "400 quota units" against "$1.00 of real cash" is not a like-for-
    like comparison — the quota is already paid for and expires if unused. The
    declared order is what expresses "quota first, deferred as scarce budget,
    payg as last resort, local as fallback". Effective cost is still computed
    and reported for every tier so the trade is visible, and a project that
    disagrees can simply reorder its tiers.
    """

    project_cfg = config.project(project)
    model_spec = config.model(model)
    if project_cfg.allowed_models and model not in project_cfg.allowed_models:
        allowed = ", ".join(project_cfg.allowed_models)
        return Decision(
            project=project_cfg.name,
            model=model,
            allowed=False,
            tier=None,
            tokens_in=0,
            tokens_out=0,
            reason=f"model {model!r} is not on this project's allowlist ({allowed})",
        )
    est_in, est_out = _estimate_tokens(tokens, tokens_in, tokens_out)
    state = evaluate(project_cfg, when)
    usage = ledger.usage(project_cfg.name, now=now) if ledger else Usage(
        project=project_cfg.name,
        hour_quota=0.0,
        hour_usd=0.0,
        week_quota=0.0,
        week_usd=0.0,
        week_deferred_usd=0.0,
        tokens_in=0,
        tokens_out=0,
        calls=0,
    )

    quotes = quote_tiers(config, project_cfg, model_spec, est_in, est_out, state, usage)
    order_index = {tier: idx for idx, tier in enumerate(project_cfg.tier_order)}
    ranked = sorted(
        (q for q in quotes if q.available),
        key=lambda q: order_index.get(q.tier, 99),
    )

    decision = Decision(
        project=project_cfg.name,
        model=model,
        allowed=False,
        tier=None,
        tokens_in=est_in,
        tokens_out=est_out,
        quotes=quotes,
        schedule=state,
        usage=usage,
    )

    if not ranked:
        unavailable = "; ".join(f"{q.tier} ({q.reason})" for q in quotes) or "no tiers configured"
        decision.reason = f"no tier is currently available — {unavailable}"
        return decision

    for quote in ranked:
        checks = cap_checks(
            project_cfg,
            usage,
            quota_units=quote.quota_units,
            usd=quote.usd,
            tier=quote.tier,
            window="hour",
        ) + cap_checks(
            project_cfg,
            usage,
            quota_units=quote.quota_units,
            usd=quote.usd,
            tier=quote.tier,
            window="week",
        )
        breached = [c for c in checks if c.breached]
        decision.caps = checks
        if breached:
            continue
        decision.allowed = True
        decision.tier = quote.tier
        decision.effective_usd = quote.effective_usd
        decision.quota_units = quote.quota_units
        decision.usd = quote.usd
        decision.multiplier = quote.multiplier
        decision.reason = f"cheapest tier inside every cap — {quote.reason}"
        return decision

    tightest = min(
        (c for c in decision.caps if c.breached),
        key=lambda c: c.headroom,
        default=None,
    )
    detail = tightest.describe() if tightest else "no cap details available"
    decision.reason = (
        f"refused: every available tier would break a hard cap ({detail}); "
        "wait for the window to roll over or raise the cap deliberately"
    )
    return decision


def enforce(decision: Decision, *, strict: bool = True) -> Decision:
    """Turn a refusal into an exception, for callers that must not proceed."""

    if strict and not decision.allowed:
        raise CapExceeded(decision.reason)
    return decision


def commit(
    ledger: Ledger,
    decision: Decision,
    *,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    actual_usd: float | None = None,
    now: float | None = None,
) -> int:
    """Record what actually happened, replacing the pre-flight estimate.

    Estimates are fine for the go/no-go decision, but the ledger must hold real
    numbers or the next routing decision is made against fiction.
    """

    if not decision.allowed or not decision.tier:
        raise CapExceeded(f"refusing to record a call that was not allowed: {decision.reason}")
    real_in = decision.tokens_in if tokens_in is None else tokens_in
    real_out = decision.tokens_out if tokens_out is None else tokens_out
    scale = 0.0
    if decision.total_tokens:
        scale = (real_in + real_out) / decision.total_tokens
    return ledger.record(
        project=decision.project,
        model=decision.model,
        tier=decision.tier,
        tokens_in=real_in,
        tokens_out=real_out,
        quota_units=decision.quota_units * scale,
        usd=decision.usd * scale if actual_usd is None else actual_usd,
        multiplier=decision.multiplier,
        note=decision.reason,
        ts=now,
    )
