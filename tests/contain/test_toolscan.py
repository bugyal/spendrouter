import pytest

from spendrouter.toolscan import classify, error_class, scan_request


def test_anthropic_trailing_tool_results_only():
    body = {
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "search", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "TimeoutError: 30s"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "fetch", "input": {}}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "200 OK, 3 rows"}]},
                    {"type": "text", "text": "continue"},
                ],
            },
        ]
    }
    results = scan_request(body)
    assert [(r.tool, r.call_id, r.error_class) for r in results] == [("fetch", "t2", "")]  # t1 is history


def test_anthropic_is_error_flag_is_honoured_even_without_error_words():
    body = {
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "db", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "row 7 locked"}]},
        ]
    }
    (result,) = scan_request(body)
    assert result.tool == "db" and result.error_class == "row # locked"


def test_openai_chat_tool_messages():
    body = {
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
                    {"id": "c2", "type": "function", "function": {"name": "get_time", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "Error: upstream returned status 503"},
            {"role": "tool", "tool_call_id": "c2", "content": "12:00"},
        ]
    }
    weather, time_ = scan_request(body)
    assert (weather.tool, weather.error_class) == ("get_weather", "http_503")
    assert (time_.tool, time_.error_class) == ("get_time", "")


def test_openai_responses_function_call_outputs():
    body = {
        "input": [
            {"role": "user", "content": "go"},
            {"type": "function_call", "call_id": "f1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "f1", "output": '{"error": "rate limited"}'},
        ]
    }
    (result,) = scan_request(body)
    assert result.tool == "lookup" and result.is_error


def test_non_conversation_bodies():
    assert scan_request(None) == []
    assert scan_request({"input": "plain text"}) == []
    assert scan_request({"messages": [{"role": "user", "content": "hi"}]}) == []


@pytest.mark.parametrize(
    "a, b",
    [
        ("TimeoutError: timed out after 30s", "TimeoutError: timed out after 31s"),
        ("connect ECONNREFUSED 10.0.0.1:443", "connect ECONNREFUSED 10.0.0.2:8443"),
        ("Error: user 'bob' not found (id 1234)", "Error: user 'alice' not found (id 99)"),
        ("requests.exceptions.ReadTimeout: HTTPSConnectionPool", "ReadTimeout: other text"),
    ],
)
def test_error_classes_ignore_volatile_details(a, b):
    assert error_class(a) == error_class(b)


def test_error_class_shapes():
    assert error_class("HTTPError: 502 Bad Gateway") == "HTTPError:http_502"
    assert error_class("Traceback (most recent call last):\n  ...\nKeyError: 'id'") == "KeyError"
    assert error_class("ECONNRESET") == "ECONNRESET"
    assert len(error_class("x" * 500)) <= 60


@pytest.mark.parametrize(
    "text, is_error",
    [
        ("Error: nope", True),
        ("Traceback (most recent call last):", True),
        ('{"error": "bad key"}', True),
        ("ValueError: bad input", True),
        ("request failed with status code 429", True),
        ("Here are 3 results about error handling in Rust", False),
        ("class FooError(Exception):\n    pass", False),
        ("all good", False),
    ],
)
def test_error_detection_without_a_flag(text, is_error):
    assert bool(classify(text, flagged=False)) is is_error
