"""End-to-end: real HTTP through the proxy to a fake provider API."""

import json
import socket

import pytest

from spendrouter.pricing import BUILTIN_PRICES
from spendrouter.usage import TokenUsage

from tests.contain.fakes import ANTHROPIC_USAGE, FakeUpstream, anthropic_tool_turn, http_call

CHAT = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
MESSAGES = {"model": "claude-sonnet-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


def rows(engine, sql="SELECT * FROM calls ORDER BY id"):
    return [dict(r) for r in engine.store.query(sql)]


def test_health_and_unknown_upstream(make_proxy):
    env = make_proxy()
    status, _, body = http_call(env.url, "GET", "/_spendrouter/health")
    assert status == 200 and json.loads(body)["ok"] is True
    status, _, body = http_call(env.url, "POST", "/gemini/v1/x", {"a": 1})
    assert status == 404 and "no upstream named 'gemini'" in body.decode()


def test_credential_is_swapped_for_the_real_key_and_call_is_metered(make_proxy):
    env = make_proxy()
    token, cred = env.engine.creds.mint(agent="support-bot", customer="acme", task="T-1")
    status, headers, body = http_call(
        env.url,
        "POST",
        "/openai/v1/chat/completions",
        CHAT,
        {"Authorization": f"Bearer {token}", "X-Spendrouter-Customer": "spoofed", "X-Spendrouter-Run": "run-7"},
    )
    assert status == 200 and json.loads(body)["choices"][0]["message"]["content"] == "Hello"
    assert headers.get("x-upstream") == "fake"
    sent = env.upstream.requests[-1]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["headers"]["authorization"] == "Bearer real-openai-key"  # the agent never held this
    assert not any(h.startswith("x-spendrouter-") for h in sent["headers"])
    assert sent["headers"]["accept-encoding"] == "identity"
    (call,) = rows(env.engine)
    assert (call["agent"], call["customer"], call["task"], call["run"]) == ("support-bot", "acme", "T-1", "run-7")
    assert call["credential_id"] == cred.id and call["model"] == "gpt-4o" and call["outcome"] == "ok"
    assert (call["input_tokens"], call["cache_read_tokens"], call["output_tokens"]) == (600, 400, 200)
    expected = BUILTIN_PRICES["gpt-4o"].cost(TokenUsage(600, 200, 400))
    assert call["cost_usd"] == pytest.approx(expected)


def test_anthropic_stream_is_relayed_intact_and_metered(make_proxy):
    env = make_proxy()
    token, _ = env.engine.creds.mint(agent="writer")
    status, headers, body = http_call(
        env.url, "POST", "/anthropic/v1/messages", dict(MESSAGES, stream=True), {"x-api-key": token, "anthropic-version": "2023-06-01"}
    )
    assert status == 200 and headers["content-type"] == "text/event-stream"
    assert body == b"".join(FakeUpstream._anthropic_events("claude-sonnet-5-5"))  # byte-for-byte
    sent = env.upstream.requests[-1]["headers"]
    assert sent["x-api-key"] == "real-anthropic-key" and sent["anthropic-version"] == "2023-06-01"
    (call,) = rows(env.engine)
    assert (call["input_tokens"], call["output_tokens"]) == (ANTHROPIC_USAGE["input_tokens"], 300)
    assert (call["cache_read_tokens"], call["cache_write_tokens"]) == (2000, 500)
    usage = TokenUsage(1000, 300, 2000, 500)
    assert call["cost_usd"] == pytest.approx(BUILTIN_PRICES["claude-sonnet-5-5"].cost(usage))
    assert call["estimated"] == 0


def test_openai_stream_gets_usage_injected(make_proxy):
    env = make_proxy()
    status, _, body = http_call(
        env.url, "POST", "/openai/v1/chat/completions", dict(CHAT, stream=True), {"Authorization": "Bearer sk-agent-own", "X-Spendrouter-Agent": "bot"}
    )
    assert status == 200 and b'"usage"' in body and body.endswith(b"data: [DONE]\n\n")
    assert env.upstream.requests[-1]["body"]["stream_options"] == {"include_usage": True}
    (call,) = rows(env.engine)
    assert call["output_tokens"] == 200 and call["estimated"] == 0


def test_stream_without_usage_is_estimated_not_free(make_proxy):
    env = make_proxy({"proxy": {"inject_stream_usage": False}})
    http_call(env.url, "POST", "/openai/v1/chat/completions", dict(CHAT, stream=True), {"X-Spendrouter-Agent": "bot"})
    (call,) = rows(env.engine)
    assert call["estimated"] == 1 and call["output_tokens"] == len("Hello world") // 4 and call["cost_usd"] > 0


def test_tag_headers_attribute_and_client_key_passes_through(make_proxy):
    env = make_proxy()
    http_call(
        env.url,
        "POST",
        "/openai/v1/responses",
        {"model": "gpt-4o", "input": "hi"},
        {"Authorization": "Bearer sk-agent-own", "X-Spendrouter-Agent": "researcher", "X-Spendrouter-Customer": "globex"},
    )
    assert env.upstream.requests[-1]["headers"]["authorization"] == "Bearer sk-agent-own"
    (call,) = rows(env.engine)
    assert (call["agent"], call["customer"], call["credential_id"], call["input_tokens"]) == ("researcher", "globex", "", 800)


def test_untagged_calls_are_still_metered(make_proxy):
    env = make_proxy()
    http_call(env.url, "GET", "/openai/v1/models", headers={"Authorization": "Bearer sk-x"})
    (call,) = rows(env.engine)
    assert call["agent"] == "unattributed" and call["endpoint"] == "/v1/models" and call["cost_usd"] == 0


def test_require_credential_refuses_raw_keys(make_proxy):
    env = make_proxy({"proxy": {"require_credential": True}})
    status, headers, body = http_call(env.url, "POST", "/anthropic/v1/messages", MESSAGES, {"x-api-key": "sk-ant-raw"})
    assert status == 401 and headers["x-spendrouter-refused"] == "credential"
    payload = json.loads(body)
    assert payload["type"] == "error" and payload["error"]["type"] == "spendrouter_credential"  # anthropic error shape
    assert env.upstream.requests == []


def test_expired_and_revoked_credentials_are_refused_and_ledgered(make_proxy):
    env = make_proxy()
    token, cred = env.engine.creds.mint(agent="nightly", task="T-5")
    env.engine.creds.revoke(ids=[cred.id])
    status, _, body = http_call(env.url, "POST", "/openai/v1/chat/completions", CHAT, {"Authorization": f"Bearer {token}"})
    assert status == 401 and "revoked" in json.loads(body)["error"]["message"]
    status, _, _ = http_call(env.url, "POST", "/openai/v1/chat/completions", CHAT, {"Authorization": "Bearer sr_forged"})
    assert status == 401
    assert env.upstream.requests == []
    refused = rows(env.engine)
    assert [(r["agent"], r["outcome"], r["error_class"]) for r in refused] == [
        ("nightly", "refused", "credential"),  # a leftover process using a dead credential is visible
        ("unattributed", "refused", "credential"),
    ]


def test_hard_cap_refuses_before_the_upstream_is_called(make_proxy):
    env = make_proxy({"budgets": [{"scope": "customer", "match": "acme", "hard_usd": 0.001}]})
    headers = {"X-Spendrouter-Agent": "bot", "X-Spendrouter-Customer": "acme"}
    assert http_call(env.url, "POST", "/openai/v1/chat/completions", CHAT, headers)[0] == 200
    status, resp_headers, body = http_call(env.url, "POST", "/openai/v1/chat/completions", CHAT, headers)
    assert status == 402 and resp_headers["x-spendrouter-refused"] == "budget"
    assert json.loads(body)["error"]["code"] == "budget"  # openai error shape
    assert len(env.upstream.requests) == 1


def test_anchor_overnight_tool_loop_is_contained(make_proxy):
    """The motivating incident: a customer's agent retried a failing tool 31,000
    times overnight. With spendrouter in front, the loop ends at the 11th
    identical failure: the agent is paused, a hook fires, and every later call
    is refused without reaching the provider."""
    env = make_proxy({"hooks": [{"events": ["breaker_tripped"], "command": ["true"]}]})
    token, _ = env.engine.creds.mint(agent="support-bot", customer="acme")
    statuses = []
    for i in range(200):  # stand-in for the 31,000
        request = anthropic_tool_turn(i, f"TimeoutError: CRM did not answer within 30s (attempt {i})")
        statuses.append(http_call(env.url, "POST", "/anthropic/v1/messages", request, {"x-api-key": token})[0])
    assert statuses[:10] == [200] * 10
    assert set(statuses[10:]) == {423}
    assert len(env.upstream.requests) == 10  # 190 calls never reached the provider
    (trip,) = [json.loads(r["data"]) for r in env.engine.store.query("SELECT data FROM contain_events WHERE kind = 'breaker_tripped'")]
    assert trip["agent"] == "support-bot" and trip["tool"] == "crm_lookup" and trip["error_class"] == "TimeoutError"
    spent = env.engine.store.scalar("SELECT SUM(cost_usd) FROM calls WHERE agent = 'support-bot'")
    per_call = BUILTIN_PRICES["claude-sonnet-5-5"].cost(TokenUsage(1000, 300, 2000, 500))
    assert spent == pytest.approx(10 * per_call)
    loops = env.engine.store.scalar("SELECT COUNT(*) FROM calls WHERE tool_error = 'crm_lookup:TimeoutError'")
    assert loops == 200  # every attempt is in the ledger, refused or not
    # A human looks, fixes the CRM, resumes: the agent works again.
    assert env.engine.resume("agent", "support-bot")
    assert http_call(env.url, "POST", "/anthropic/v1/messages", MESSAGES, {"x-api-key": token})[0] == 200


def test_openai_responses_function_call_loop_trips(make_proxy):
    env = make_proxy({"breaker": {"max_repeats": 2}})
    headers = {"Authorization": "Bearer sk-x", "X-Spendrouter-Agent": "resp-bot"}
    statuses = []
    for i in range(4):
        body = {
            "model": "gpt-4o",
            "input": [
                {"role": "user", "content": "go"},
                {"type": "function_call", "call_id": f"c{i}", "name": "scrape", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"c{i}", "output": "Error: 403 Forbidden"},
            ],
        }
        statuses.append(http_call(env.url, "POST", "/openai/v1/responses", body, headers)[0])
    assert statuses == [200, 200, 423, 423]


def test_upstream_errors_are_recorded_and_can_trip(make_proxy):
    env = make_proxy({"breaker": {"api_error_repeats": 2}})
    env.upstream.fail_status = 500
    headers = {"X-Spendrouter-Agent": "flaky", "Authorization": "Bearer sk-x"}
    statuses = [http_call(env.url, "POST", "/openai/v1/chat/completions", dict(CHAT, n=i), headers)[0] for i in range(4)]
    assert statuses == [500, 500, 500, 423]  # the provider's error is passed through untouched
    errors = rows(env.engine, "SELECT outcome, http_status, error_class, cost_usd FROM calls WHERE outcome = 'error'")
    assert errors == [{"outcome": "error", "http_status": 500, "error_class": "http_500", "cost_usd": 0.0}] * 3


def test_identical_retries_are_flagged(make_proxy):
    env = make_proxy()
    headers = {"X-Spendrouter-Agent": "bot", "Authorization": "Bearer sk-x"}
    http_call(env.url, "POST", "/openai/v1/chat/completions", CHAT, headers)
    http_call(env.url, "POST", "/openai/v1/chat/completions", CHAT, headers)
    http_call(env.url, "POST", "/openai/v1/chat/completions", dict(CHAT, temperature=0), dict(headers, **{"x-stainless-retry-count": "1"}))
    assert [r["retry"] for r in rows(env.engine)] == [0, 1, 1]


def test_unreachable_upstream_is_a_502_and_ledgered(make_proxy, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    env = make_proxy({"upstreams": {"openai": {"base_url": f"http://127.0.0.1:{dead_port}"}}})
    status, _, body = http_call(env.url, "POST", "/openai/v1/chat/completions", CHAT, {"X-Spendrouter-Agent": "bot"})
    assert status == 502 and "unreachable" in json.loads(body)["error"]["message"]
    (call,) = rows(env.engine)
    assert (call["outcome"], call["error_class"]) == ("error", "upstream_unreachable")


def test_missing_provider_key_is_explained(make_proxy, monkeypatch):
    env = make_proxy()
    monkeypatch.delenv("OPENAI_API_KEY")
    token, _ = env.engine.creds.mint(agent="bot")
    status, _, body = http_call(env.url, "POST", "/openai/v1/chat/completions", CHAT, {"Authorization": f"Bearer {token}"})
    assert status == 502 and "OPENAI_API_KEY" in json.loads(body)["error"]["message"]
    assert env.upstream.requests == []


def test_keep_alive_connection_serves_several_calls(make_proxy):
    import http.client
    from urllib.parse import urlsplit

    env = make_proxy()
    parts = urlsplit(env.url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        for stream in (False, True, False):
            conn.request("POST", "/openai/v1/chat/completions", body=json.dumps(dict(CHAT, stream=stream)), headers={"X-Spendrouter-Agent": "ka"})
            resp = conn.getresponse()
            assert resp.status == 200
            resp.read()
    finally:
        conn.close()
    assert len(rows(env.engine)) == 3


def test_chunked_request_body_is_accepted(make_proxy):
    import http.client
    from urllib.parse import urlsplit

    env = make_proxy()
    parts = urlsplit(env.url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        data = json.dumps(CHAT).encode()
        conn.request("POST", "/openai/v1/chat/completions", body=iter([data[:10], data[10:]]), headers={"Transfer-Encoding": "chunked"}, encode_chunked=True)
        assert conn.getresponse().status == 200
    finally:
        conn.close()
    assert env.upstream.requests[-1]["body"] == CHAT
