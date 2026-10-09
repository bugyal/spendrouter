"""The containment engine: ledger, budgets, circuit breaker, pauses, credentials."""

import json
import sys

import pytest

from spendrouter.contain import Attribution, SpendBlocked
from spendrouter.toolscan import ToolResult
from spendrouter.usage import TokenUsage

from tests.contain.fakes import FakeClock

HALF_DOLLAR = TokenUsage(input_tokens=500_000, found=True)  # test-model: $1 per 1M input tokens
ACME_BOT = Attribution(agent="support-bot", customer="acme", task="T-1")


def events(engine, kind=None):
    rows = engine.store.query("SELECT kind, data FROM contain_events ORDER BY id")
    return [json.loads(r["data"]) for r in rows if kind is None or r["kind"] == kind]


def spend(engine, attr=ACME_BOT, usage=HALF_DOLLAR, **kw):
    return engine.record_call(attr, upstream="openai", model="test-model", usage=usage, **kw)


# -- ledger ------------------------------------------------------------------


def test_record_call_prices_and_attributes(make_containment):
    engine = make_containment()
    cost = spend(engine)
    assert cost == pytest.approx(0.5)
    row = engine.store.one("SELECT * FROM calls")
    assert (row["agent"], row["customer"], row["task"], row["outcome"], row["priced"]) == ("support-bot", "acme", "T-1", "ok", 1)
    assert row["cost_usd"] == pytest.approx(0.5)


def test_failed_and_refused_calls_are_ledgered_too(make_containment):
    engine = make_containment({"breaker": {"api_error_repeats": 0}})
    engine.record_call(ACME_BOT, upstream="openai", outcome="error", http_status=500, error_class="http_500")
    engine.pause("agent", "support-bot")
    assert not engine.admit(ACME_BOT, upstream="openai").allowed
    outcomes = [r["outcome"] for r in engine.store.query("SELECT outcome FROM calls ORDER BY id")]
    assert outcomes == ["error", "refused"]


def test_identical_resend_is_marked_as_retry(make_containment):
    clock = FakeClock()
    engine = make_containment(clock=clock)
    assert not engine.admit(ACME_BOT, body_hash="abc").retry
    spend(engine, body_hash="abc")
    clock.advance(5)
    assert engine.admit(ACME_BOT, body_hash="abc").retry
    assert engine.admit(ACME_BOT, body_hash="abc", retry_hint=True).retry
    clock.advance(301)  # outside retry_window_seconds
    assert not engine.admit(ACME_BOT, body_hash="abc").retry


# -- budgets -----------------------------------------------------------------


def test_soft_cap_alerts_once_per_window(make_containment):
    clock = FakeClock()
    engine = make_containment({"budgets": [{"scope": "customer", "match": "*", "soft_usd": 1, "hard_usd": 5}]}, clock=clock)
    for _ in range(4):
        assert engine.admit(ACME_BOT).allowed
        spend(engine)
    assert len(events(engine, "soft_cap_exceeded")) == 1
    (alert,) = events(engine, "soft_cap_exceeded")
    assert alert["value"] == "acme" and alert["limit_usd"] == 1
    clock.advance(86400)  # a new day re-arms the alert
    spend(engine)
    spend(engine)
    assert len(events(engine, "soft_cap_exceeded")) == 2


def test_hard_cap_refuses_and_rolls_over_next_day(make_containment):
    clock = FakeClock()
    engine = make_containment({"budgets": [{"scope": "customer", "match": "*", "hard_usd": 1}]}, clock=clock)
    spend(engine)
    assert engine.admit(ACME_BOT).allowed
    spend(engine)
    verdict = engine.admit(ACME_BOT)
    assert (verdict.allowed, verdict.reason, verdict.status) == (False, "budget", 402)
    assert "hard cap" in verdict.message
    assert engine.admit(Attribution(agent="support-bot", customer="globex")).allowed  # per customer
    assert len(events(engine, "hard_cap_exceeded")) == 1
    clock.advance(12 * 3600 + 1)  # past midnight UTC
    assert engine.admit(ACME_BOT).allowed


def test_hard_cap_with_pause_action_needs_a_human(make_containment):
    clock = FakeClock()
    engine = make_containment(
        {"budgets": [{"scope": "customer", "match": "acme", "period": "month", "hard_usd": 1, "action": "pause"}]}, clock=clock
    )
    spend(engine)
    spend(engine)  # crossing the cap pauses the customer straight away
    verdict = engine.admit(Attribution(agent="other-bot", customer="acme"))
    assert (verdict.allowed, verdict.reason, verdict.status) == (False, "paused", 423)
    assert "resume --customer acme" in verdict.message
    clock.advance(40 * 86400)  # a new month does not lift a pause
    assert not engine.admit(ACME_BOT).allowed
    assert engine.resume("customer", "acme")
    assert engine.admit(ACME_BOT).allowed
    assert [e["kind"] for e in events(engine) if e["kind"] in ("hard_cap_exceeded", "paused", "resumed")] == [
        "hard_cap_exceeded",
        "paused",
        "resumed",
    ]


def test_global_cap_counts_everyone(make_containment):
    engine = make_containment({"budgets": [{"scope": "global", "hard_usd": 1}]})
    spend(engine, Attribution(agent="a"))
    spend(engine, Attribution(agent="b"))
    assert engine.admit(Attribution(agent="c")).reason == "budget"


def test_rules_for_one_value_ignore_others(make_containment):
    engine = make_containment({"budgets": [{"scope": "agent", "match": "big-spender", "hard_usd": 0.5}]})
    spend(engine)
    assert engine.admit(ACME_BOT).allowed
    spend(engine, Attribution(agent="big-spender"))
    assert engine.admit(Attribution(agent="big-spender")).reason == "budget"


# -- circuit breaker -----------------------------------------------------------


def tool_error(i, cls="TimeoutError", tool="crm_lookup"):
    return [ToolResult(tool, f"call_{i}", cls)]


def test_tool_loop_trips_after_max_repeats(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"max_repeats": 3}}, clock=clock)
    allowed = []
    for i in range(6):
        clock.advance(1)
        allowed.append(engine.admit(ACME_BOT, tool_results=tool_error(i)).allowed)
    assert allowed == [True, True, True, False, False, False]
    first_refusal = engine.store.one("SELECT error_class, tool_error FROM calls WHERE outcome = 'refused' ORDER BY id")
    assert (first_refusal["error_class"], first_refusal["tool_error"]) == ("breaker", "crm_lookup:TimeoutError")
    (trip,) = events(engine, "breaker_tripped")
    assert trip["signal"] == "tool_loop" and trip["tool"] == "crm_lookup" and trip["count"] == 4
    assert engine.admit(ACME_BOT).reason == "paused"  # the agent stays paused, whatever it sends
    assert engine.admit(Attribution(agent="other-bot")).allowed  # other agents are untouched


def test_success_resets_the_streak(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"max_repeats": 3}}, clock=clock)
    for i in range(3):
        clock.advance(1)
        assert engine.admit(ACME_BOT, tool_results=tool_error(i)).allowed
    clock.advance(1)
    assert engine.admit(ACME_BOT, tool_results=[ToolResult("crm_lookup", "ok_1", "")]).allowed
    for i in range(3, 6):
        clock.advance(1)
        assert engine.admit(ACME_BOT, tool_results=tool_error(i)).allowed


def test_error_classes_and_tools_count_separately(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"max_repeats": 2}}, clock=clock)
    for i in range(2):
        clock.advance(1)
        assert engine.admit(ACME_BOT, tool_results=tool_error(i, "TimeoutError")).allowed
        assert engine.admit(ACME_BOT, tool_results=tool_error(i + 100, "http_503")).allowed
        assert engine.admit(ACME_BOT, tool_results=tool_error(i + 200, tool="search")).allowed


def test_resent_tool_results_are_not_double_counted(make_containment):
    engine = make_containment({"breaker": {"max_repeats": 2, "max_identical_requests": 0}})
    for _ in range(10):  # an SDK retrying the same request carries the same tool_use ids
        assert engine.admit(ACME_BOT, tool_results=tool_error(1)).allowed


def test_failures_outside_the_window_are_forgotten(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"max_repeats": 2, "window_seconds": 600}}, clock=clock)
    for i in range(2):
        assert engine.admit(ACME_BOT, tool_results=tool_error(i)).allowed
    clock.advance(601)
    assert engine.admit(ACME_BOT, tool_results=tool_error(9)).allowed


def test_resume_restarts_breaker_counts(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"max_repeats": 1}}, clock=clock)
    for i in range(2):
        clock.advance(1)
        engine.admit(ACME_BOT, tool_results=tool_error(i))
    assert engine.admit(ACME_BOT).reason == "paused"
    clock.advance(1)
    assert engine.resume("agent", "support-bot")
    clock.advance(1)
    assert engine.admit(ACME_BOT, tool_results=tool_error(5)).allowed  # not re-tripped by old failures


def test_cooldown_auto_resumes(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"max_repeats": 1, "cooldown_seconds": 60}}, clock=clock)
    for i in range(2):
        engine.admit(ACME_BOT, tool_results=tool_error(i))
    assert "Auto-resumes" in engine.admit(ACME_BOT).message
    clock.advance(61)
    assert engine.admit(ACME_BOT).allowed
    (resumed,) = events(engine, "resumed")
    assert resumed["by"] == "cooldown"


def test_identical_request_loop_trips(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"max_identical_requests": 3}}, clock=clock)
    for _ in range(3):
        clock.advance(1)
        verdict = engine.admit(ACME_BOT, body_hash="same")
        assert verdict.allowed
        spend(engine, body_hash="same", retry=verdict.retry)
    verdict = engine.admit(ACME_BOT, body_hash="same")
    assert verdict.reason == "breaker"
    assert events(engine, "breaker_tripped")[0]["signal"] == "identical_requests"


def test_upstream_error_streak_trips(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"api_error_repeats": 2}}, clock=clock)
    for _ in range(3):
        clock.advance(1)
        engine.record_call(ACME_BOT, upstream="openai", outcome="error", http_status=500, error_class="http_500")
    verdict = engine.admit(ACME_BOT, upstream="openai")
    assert verdict.reason == "paused" and "http_500" in verdict.message
    assert events(engine, "breaker_tripped")[0]["signal"] == "api_errors"


def test_upstream_errors_interleaved_with_success_do_not_trip(make_containment):
    clock = FakeClock()
    engine = make_containment({"breaker": {"api_error_repeats": 2}}, clock=clock)
    for _ in range(5):
        clock.advance(1)
        engine.record_call(ACME_BOT, upstream="openai", outcome="error", http_status=429, error_class="http_429")
        clock.advance(1)
        spend(engine)
    assert engine.admit(ACME_BOT, upstream="openai").allowed


def test_breaker_can_be_disabled(make_containment):
    engine = make_containment({"breaker": {"enabled": False, "max_repeats": 1}})
    for i in range(5):
        assert engine.admit(ACME_BOT, tool_results=tool_error(i)).allowed


# -- pauses ------------------------------------------------------------------


def test_manual_pause_scopes(make_containment):
    engine = make_containment()
    engine.pause("task", "T-1", reason="investigating")
    assert engine.admit(ACME_BOT).reason == "paused"
    assert engine.admit(Attribution(agent="support-bot", task="T-2")).allowed
    engine.pause("global", "ignored")
    assert "spendrouter resume --all" in engine.admit(Attribution(agent="x")).message
    assert engine.resume("global", "*") and engine.resume("task", "T-1")
    assert not engine.resume("task", "T-1")
    assert engine.admit(ACME_BOT).allowed


# -- credentials -------------------------------------------------------------


def test_credentials_are_hashed_scoped_and_revocable(make_containment):
    engine = make_containment()
    token, cred = engine.creds.mint(agent="support-bot", customer="acme", upstreams=["anthropic"], ttl_seconds=3600)
    assert token.startswith("sr_") and len(token) > 40
    dump = "\n".join(str(tuple(r)) for r in engine.store.query("SELECT * FROM credentials"))
    assert token not in dump  # only the hash is stored
    found, refusal = engine.verify_credential(token)
    assert refusal is None and found.id == cred.id and found.upstreams == ("anthropic",)
    attr = Attribution(agent="support-bot", customer="acme", credential_id=cred.id)
    assert engine.admit(attr, upstream="openai", credential=found).reason == "credential_scope"
    assert engine.admit(attr, upstream="anthropic", credential=found).allowed
    assert engine.creds.revoke(ids=[cred.id]) == [cred.id]
    assert "revoked" in engine.verify_credential(token)[1].message
    assert engine.verify_credential("sr_made_up")[1].reason == "credential"


def test_credentials_expire(make_containment):
    clock = FakeClock()
    engine = make_containment(clock=clock)
    token, _ = engine.creds.mint(agent="a", ttl_seconds=60)
    assert engine.verify_credential(token)[1] is None
    clock.advance(61)
    refusal = engine.verify_credential(token)[1]
    assert refusal.status == 401 and "expired" in refusal.message
    assert engine.creds.list() == [] and len(engine.creds.list(include_inactive=True)) == 1
    assert engine.creds.gc(older_than_seconds=3600) == 0
    assert engine.creds.gc() == 1


def test_credential_spend_cap(make_containment):
    engine = make_containment()
    token, cred = engine.creds.mint(agent="support-bot", max_usd=1.0)
    attr = Attribution(agent="support-bot", credential_id=cred.id)
    spend(engine, attr)
    assert engine.admit(attr, credential=cred).allowed
    spend(engine, attr)
    verdict = engine.admit(attr, credential=cred)
    assert (verdict.reason, verdict.status) == ("credential_budget", 402)
    assert engine.creds.stats(cred.id) == (3, pytest.approx(1.0))  # two calls + the refusal


def test_revoke_by_task(make_containment):
    engine = make_containment()
    engine.creds.mint(agent="a", task="T-9")
    engine.creds.mint(agent="b", task="T-9")
    engine.creds.mint(agent="c", task="T-1")
    assert len(engine.creds.revoke(task="T-9")) == 2
    assert [c.agent for c in engine.creds.list()] == ["c"]


# -- in-process API ------------------------------------------------------------


def test_call_context_records_sdk_response(make_containment):
    engine = make_containment()
    response = {
        "type": "message",
        "model": "claude-sonnet-5-5",
        "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 0},
    }
    with engine.call(agent="support-bot", customer="acme") as call:
        call.record_response(response)
    assert call.cost_usd == pytest.approx(2.0 + 1.0)
    row = engine.store.one("SELECT model, customer, upstream FROM calls")
    assert tuple(row) == ("claude-sonnet-5-5", "acme", "sdk")


def test_call_context_refuses_and_records_failures(make_containment):
    engine = make_containment()

    class RateLimitError(Exception):
        status_code = 429

    with pytest.raises(RateLimitError):
        with engine.call(agent="bot"):
            raise RateLimitError("slow down")
    row = engine.store.one("SELECT outcome, http_status, error_class FROM calls")
    assert tuple(row) == ("error", 429, "http_429")
    engine.pause("agent", "bot")
    with pytest.raises(SpendBlocked) as info:
        with engine.call(agent="bot"):
            pytest.fail("a refused call must not run")
    assert info.value.verdict.reason == "paused"


def test_call_context_scans_request_for_tool_loops(make_containment):
    engine = make_containment({"breaker": {"max_repeats": 2}})
    from tests.contain.fakes import anthropic_tool_turn

    for i in range(2):
        with engine.call(agent="bot", request=anthropic_tool_turn(i, "TimeoutError: 30s")) as call:
            call.set_usage(input_tokens=10, output_tokens=1)
    with pytest.raises(SpendBlocked) as info:
        with engine.call(agent="bot", request=anthropic_tool_turn(3, "TimeoutError: 31s")):
            pass
    assert info.value.verdict.reason == "breaker"


def test_tool_result_api_trips_breaker(make_containment):
    engine = make_containment({"breaker": {"max_repeats": 2}})
    engine.tool_result(agent="bot", tool="search", error=TimeoutError("read timed out"))
    engine.tool_result(agent="bot", tool="search", error="TimeoutError: read timed out (2)")
    with pytest.raises(SpendBlocked) as info:
        engine.tool_result(agent="bot", tool="search", error=TimeoutError("read timed out"))
    assert "search" in str(info.value) and info.value.verdict.status == 423
    with pytest.raises(SpendBlocked):
        engine.tool_result(agent="bot", tool="other")  # paused agents are refused everything


def test_hooks_receive_events(make_containment, tmp_path):
    out = tmp_path / "hook.jsonl"
    script = f"import sys; open({str(out)!r}, 'a').write(sys.stdin.read() + '\\n')"
    engine = make_containment({"hooks": [{"events": ["paused"], "command": [sys.executable, "-c", script]}]})
    engine.pause("agent", "bot", reason="testing hooks")
    engine.events.wait()
    (event,) = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert event["kind"] == "paused" and event["reason"] == "testing hooks"
    log = engine.config.events_path.read_text().splitlines()
    assert json.loads(log[-1])["kind"] == "paused"


# -- one ledger file -----------------------------------------------------------


def test_contain_tables_share_the_route_ledger_file(make_containment, tmp_path):
    """Both layers write one sqlite file; neither layer's tables get in the other's way."""
    from spendrouter.ledger import Ledger

    path = str(tmp_path / "shared" / "ledger.sqlite3")
    with Ledger(path) as route_ledger:
        route_ledger.record(project="work", model="big", tier="payg", usd=1.25)
    engine = make_containment({"db_path": path})
    spend(engine)  # a metered call...
    engine.pause("agent", "support-bot")  # ...and a contain event
    assert engine.store.scalar("SELECT COUNT(*) FROM calls") == 1
    with Ledger(path) as route_ledger:
        assert route_ledger.usage("work").week_usd == pytest.approx(1.25)
        assert [row["model"] for row in route_ledger.recent()] == ["big"]
