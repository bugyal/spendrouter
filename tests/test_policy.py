"""The core promise: spend the sunk tier first, then fall back in order, and
refuse the call outright when nothing fits inside the hard caps.
"""

from __future__ import annotations

import time

import pytest

from spendrouter.config import ConfigError
from spendrouter.policy import CapExceeded, commit, enforce, route

from tests.conftest import IN_WINDOW_OFF_PEAK, OUTSIDE_WINDOW_OFF_PEAK, PEAK_OUTSIDE_WINDOW


def _exhaust_subscription(ledger, project="work", model="big"):
    """Burn the weekly quota so the subscription tier is genuinely unavailable."""

    ledger.record(
        project=project, model=model, tier="subscription", quota_units=1000.0, ts=time.time()
    )


# -- tier selection ------------------------------------------------------


def test_subscription_is_the_default_tier(config, ledger):
    """100k tokens at 4 quota/1k = 400 units, which fits hourly (500) and weekly (1000)."""

    decision = route(config, project="work", model="big", tokens=100_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger)
    assert decision.allowed
    assert decision.tier == "subscription"
    assert decision.quota_units == pytest.approx(400.0)
    assert decision.usd == pytest.approx(0.0)
    assert decision.effective_usd == pytest.approx(8.0)  # 400 units * $0.02


def test_deferred_takes_over_when_the_quota_is_gone(config, ledger):
    _exhaust_subscription(ledger)
    decision = route(config, project="work", model="big", tokens=100_000, when=IN_WINDOW_OFF_PEAK, ledger=ledger)
    assert decision.allowed
    assert decision.tier == "deferred"
    assert decision.usd == pytest.approx(0.05)  # 0.1 Mtok * $1.00 * 0.5 discount
    assert decision.multiplier == pytest.approx(0.5)
    assert decision.quota_units == pytest.approx(0.0)


def test_payg_takes_over_when_the_window_is_closed(config, ledger):
    _exhaust_subscription(ledger)
    decision = route(
        config, project="work", model="big", tokens=100_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger
    )
    assert decision.allowed
    assert decision.tier == "payg"
    assert decision.usd == pytest.approx(1.0)  # 0.1 Mtok * $10.00


def test_local_tier_is_free(config, ledger):
    decision = route(config, project="work", model="localonly", tokens=5_000_000, ledger=ledger)
    assert decision.allowed
    assert decision.tier == "local"
    assert decision.usd == 0.0
    assert decision.effective_usd == 0.0


def test_local_is_the_last_resort_not_the_first_choice(config, ledger):
    """A project listing local last must not silently prefer it over paid quota."""

    decision = route(config, project="work", model="small", tokens=1000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger)
    assert decision.tier == "subscription"


def test_deferred_is_unavailable_outside_its_window(config, ledger):
    """The quote must say *why*, so a user can see the window is the blocker."""

    decision = route(config, project="work", model="big", tokens=1000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger)
    deferred = next(q for q in decision.quotes if q.tier == "deferred")
    assert not deferred.available
    assert "outside every deferred window" in deferred.reason


# -- peak hours ----------------------------------------------------------


def test_peak_hours_multiply_quota_burn(config, ledger):
    off_peak = route(config, project="work", model="small", tokens=10_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger)
    peak = route(config, project="work", model="small", tokens=10_000, when=PEAK_OUTSIDE_WINDOW, ledger=ledger)
    assert off_peak.tier == "subscription"
    assert off_peak.quota_units == pytest.approx(10.0)
    assert peak.quota_units == pytest.approx(40.0)  # x4 peak multiplier
    assert peak.multiplier == pytest.approx(4.0)


def test_peak_burn_can_push_a_call_off_the_subscription_tier(config, ledger):
    """The same 200k-token call is 200 quota units off-peak but 800 at peak;
    with a 500-unit hourly quota the peak version must fail over instead of
    blowing the cap."""

    off_peak = route(config, project="work", model="small", tokens=200_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger)
    peak = route(config, project="work", model="small", tokens=200_000, when=PEAK_OUTSIDE_WINDOW, ledger=ledger)
    assert off_peak.tier == "subscription"
    assert off_peak.quota_units == pytest.approx(200.0)
    assert peak.tier == "payg"  # 800 units would breach the 500-unit hourly cap
    assert peak.quota_units == pytest.approx(0.0)
    assert peak.usd > 0.0


# -- caps and refusals ---------------------------------------------------


def test_model_outside_the_allowlist_is_refused(config, ledger):
    decision = route(config, project="nocaps", model="small", tokens=1000, ledger=ledger)
    assert not decision.allowed
    assert "allowlist" in decision.reason


def test_unknown_project_and_model_raise(config):
    with pytest.raises(ConfigError):
        config.project("nope")
    with pytest.raises(ConfigError):
        config.model("nope")


def test_refuses_when_every_tier_would_break_a_cap(config, ledger):
    # paygonly has no local fallback, so a $12 call against a $10 weekly cap
    # has nowhere to go and must be refused rather than quietly allowed.
    decision = route(
        config, project="work", model="paygonly", tokens=6_000_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger
    )
    assert not decision.allowed
    assert "hard cap" in decision.reason
    assert any(cap.breached for cap in decision.caps)


def test_a_call_that_fits_is_allowed(config, ledger):
    decision = route(
        config, project="work", model="paygonly", tokens=1_000_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger
    )
    assert decision.allowed  # $2.00 against a $5 hourly cap
    assert decision.tier == "payg"


def test_hard_cap_stops_a_runaway_job_before_it_bills(config, ledger):
    """The marquee behaviour: N calls, then a refusal — never an overrun."""

    allowed = 0
    for _ in range(50):
        decision = route(
            config, project="work", model="paygonly", tokens=250_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger
        )
        if not decision.allowed:
            assert "hard cap" in decision.reason
            break
        commit(ledger, decision, now=time.time())
        allowed += 1
    # $0.50 per call against a $5.00 hourly cap -> 10 calls, then refused.
    assert allowed == 10
    usage = ledger.usage("work")
    assert usage.hour_usd == pytest.approx(5.0)
    assert usage.hour_usd <= 5.0  # never overshot


def test_deferred_budget_is_a_separate_cap_from_cash(config, ledger):
    """Spending the deferred budget must stop the run, not overflow it."""

    _exhaust_subscription(ledger)
    tiers_used: list[str] = []
    refusal: str | None = None
    for _ in range(120):
        decision = route(
            config, project="work", model="big", tokens=100_000, when=IN_WINDOW_OFF_PEAK, ledger=ledger
        )
        if not decision.allowed:
            refusal = decision.reason
            break
        commit(ledger, decision, now=time.time())
        tiers_used.append(decision.tier or "?")
    # $0.05 per deferred call fills the $5 deferred budget in exactly 100 calls.
    # Those calls also accrue real cash, so the $5 hourly spend cap trips at the
    # same moment and the 101st call is refused rather than overflowing.
    assert tiers_used.count("deferred") == 100
    assert refusal is not None
    assert "hourly spend cap" in refusal
    usage = ledger.usage("work")
    assert usage.week_deferred_usd == pytest.approx(5.0)
    assert usage.week_deferred_usd <= 5.0 + 1e-9
    assert usage.hour_usd <= 5.0 + 1e-9


def test_enforce_raises_for_strict_callers(config, ledger):
    decision = route(
        config, project="work", model="paygonly", tokens=6_000_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger
    )
    with pytest.raises(CapExceeded):
        enforce(decision)


def test_commit_refuses_unallowed_calls(config, ledger):
    decision = route(config, project="nocaps", model="small", tokens=1000, ledger=ledger)
    with pytest.raises(CapExceeded):
        commit(ledger, decision)


# -- accounting ----------------------------------------------------------


def test_commit_scales_the_estimate_to_actual_tokens(config, ledger):
    decision = route(config, project="work", model="big", tokens=100_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger)
    assert decision.tier == "subscription"
    commit(ledger, decision, tokens_in=50_000, tokens_out=50_000)
    assert ledger.usage("work").week_quota == pytest.approx(400.0)


def test_commit_halves_the_burn_when_only_half_the_tokens_land(config, ledger):
    """Real token counts must correct the estimate in *both* directions."""

    decision = route(config, project="work", model="big", tokens=100_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger)
    commit(ledger, decision, tokens_in=35_000, tokens_out=15_000)
    assert ledger.usage("work").week_quota == pytest.approx(200.0)


def test_commit_accepts_a_real_dollar_figure(config, ledger):
    _exhaust_subscription(ledger)
    decision = route(config, project="work", model="big", tokens=100_000, when=OUTSIDE_WINDOW_OFF_PEAK, ledger=ledger)
    assert decision.tier == "payg"
    commit(ledger, decision, actual_usd=0.87)
    assert ledger.usage("work").week_usd == pytest.approx(0.87)


def test_estimate_splits_tokens_when_only_a_total_is_given(config, ledger):
    decision = route(config, project="work", model="big", tokens=1000, ledger=ledger)
    assert decision.tokens_in + decision.tokens_out == 1000
    assert decision.tokens_in == 700  # output tokens are the expensive side


def test_explicit_in_out_tokens_bypass_the_split(config, ledger):
    decision = route(config, project="work", model="big", tokens_in=10, tokens_out=90, ledger=ledger)
    assert (decision.tokens_in, decision.tokens_out) == (10, 90)


def test_routing_without_a_ledger_assumes_a_clean_slate(config):
    decision = route(config, project="work", model="big", tokens=1000, when=OUTSIDE_WINDOW_OFF_PEAK)
    assert decision.allowed
    assert decision.tier == "subscription"
    assert decision.usage is not None
    assert decision.usage.week_usd == 0.0
