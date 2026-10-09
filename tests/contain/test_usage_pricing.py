import json

import pytest

from spendrouter.pricing import BUILTIN_PRICES, Price, PriceTable
from spendrouter.usage import StreamMeter, TokenUsage, detect_format, parse_response


def test_openai_chat_cached_tokens_are_split_out():
    usage, model = parse_response(
        "openai",
        {"model": "gpt-4o", "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 400}}},
    )
    assert model == "gpt-4o"
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (600, 400, 200)


def test_openai_responses_api_usage():
    usage, _ = parse_response(
        "openai", {"object": "response", "usage": {"input_tokens": 50, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 10}}}
    )
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (40, 10, 7)


def test_anthropic_usage_with_cache_ttl_breakdown():
    usage, _ = parse_response(
        "anthropic",
        {
            "type": "message",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 50,
                "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 30},
            },
        },
    )
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (10, 5, 100)
    assert (usage.cache_write_tokens, usage.cache_write_1h_tokens) == (50, 30)


def test_missing_usage_is_reported_as_not_found():
    usage, model = parse_response("openai", {"model": "x", "choices": []})
    assert not usage.found and model == "x"
    assert not parse_response("openai", None)[0].found


def test_detect_format():
    assert detect_format({"type": "message", "usage": {"input_tokens": 1, "output_tokens": 1}}) == "anthropic"
    assert detect_format({"usage": {"prompt_tokens": 1}}) == "openai"


def _sse(events, split_at=7):
    raw = b"".join(events)
    return [raw[i : i + split_at] for i in range(0, len(raw), split_at)]  # deliberately awkward chunking


def test_anthropic_stream_meter_merges_start_and_delta():
    events = [
        b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5",'
        b'"usage":{"input_tokens":12,"cache_read_input_tokens":3000,"output_tokens":1}}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi there"}}\n\n',
        b'event: message_delta\r\ndata: {"type":"message_delta","usage":{"output_tokens":42}}\r\n\r\n',
        b": keep-alive comment\n\n",
    ]
    meter = StreamMeter("anthropic")
    for chunk in _sse(events):
        meter.feed(chunk)
    usage = meter.close()
    assert meter.model == "claude-opus-5-5"
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (12, 3000, 42)
    assert meter.text_chars == len("Hi there")
    assert meter.error_type == ""


def test_anthropic_stream_error_event():
    meter = StreamMeter("anthropic")
    meter.feed(b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"x"}}\n\n')
    meter.close()
    assert meter.error_type == "overloaded_error"


def test_openai_stream_meter_reads_final_usage_chunk():
    base = {"object": "chat.completion.chunk", "model": "gpt-4o-mini"}
    events = [
        b"data: " + json.dumps({**base, "choices": [{"delta": {"content": "abc"}}]}).encode() + b"\n\n",
        b"data: " + json.dumps({**base, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}).encode() + b"\n\n",
        b"data: [DONE]\n\n",
    ]
    meter = StreamMeter("openai")
    for chunk in _sse(events, split_at=5):
        meter.feed(chunk)
    usage = meter.close()
    assert usage.found and (usage.input_tokens, usage.output_tokens) == (9, 3)
    assert meter.model == "gpt-4o-mini" and meter.text_chars == 3


def test_openai_responses_stream_completed_event():
    meter = StreamMeter("openai")
    meter.feed(b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hey"}\n\n')
    meter.feed(
        b'event: response.completed\ndata: {"type":"response.completed","response":{"model":"o3",'
        b'"usage":{"input_tokens":100,"output_tokens":20,"input_tokens_details":{"cached_tokens":60}},"error":null}}\n\n'
    )
    usage = meter.close()
    assert meter.model == "o3" and meter.text_chars == 3 and meter.error_type == ""
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (40, 60, 20)


def test_stream_without_usage_is_not_found():
    meter = StreamMeter("openai")
    meter.feed(b'data: {"choices":[{"delta":{"content":"abcd"}}]}\n\ndata: [DONE]\n\n')
    assert not meter.close().found
    assert meter.text_chars == 4


# -- pricing -----------------------------------------------------------------


def test_cost_is_a_weighted_sum_per_million():
    price = Price(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=2_000_000, cache_write_tokens=400_000)
    assert price.cost(usage) == pytest.approx(3.0 + 1.5 + 0.6 + 1.5)


def test_one_hour_cache_writes_bill_at_twice_input():
    price = Price(input=2.0, output=10.0)
    usage = TokenUsage(cache_write_tokens=1_000_000, cache_write_1h_tokens=1_000_000)
    assert price.cost(usage) == pytest.approx(4.0)
    assert Price(input=2.0, output=10.0).cost(TokenUsage(cache_write_tokens=1_000_000)) == pytest.approx(2.5)  # 1.25x default


@pytest.mark.parametrize(
    "model, key",
    [
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),  # dated snapshot
        ("anthropic/claude-sonnet-4.5", "claude-sonnet-4-5"),  # openrouter naming
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", "claude-opus-4-1"),  # bedrock naming
        ("claude-opus-4-5@20251101", "claude-opus-4-5"),  # vertex naming
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),  # longest key wins
        ("GPT-5-MINI", "gpt-5-mini"),
        ("gpt-5-pro", None),  # NOT priced as gpt-5: a pricier sibling must not inherit a cheap price
        ("gpt-5.6-sol", None),
        ("totally-unknown", None),
    ],
)
def test_model_matching_is_conservative(model, key):
    assert PriceTable().lookup(model)[1] == key


def test_unknown_models_get_the_high_fallback_and_are_flagged():
    table = PriceTable()
    cost, priced = table.cost("mystery-model", TokenUsage(input_tokens=1_000_000))
    assert not priced and cost == pytest.approx(15.0)
    known, priced = table.cost("gpt-4o-mini", TokenUsage(input_tokens=1_000_000))
    assert priced and known == pytest.approx(0.15)
    assert table.cost("anything", TokenUsage()) == (0.0, True)  # nothing used, nothing to price


def test_glob_keys_in_overrides():
    table = PriceTable({**BUILTIN_PRICES, "gpt-5.6-*": Price(5.0, 30.0)})
    assert table.lookup("gpt-5.6-sol")[1] == "gpt-5.6-*"


def test_builtin_claude_prices_match_published_rates():
    assert BUILTIN_PRICES["claude-opus-5-5"] == Price(4.0, 20.0, 0.20, 5.0)
    assert BUILTIN_PRICES["claude-sonnet-5-5"] == Price(2.0, 10.0, 0.20, 2.5)
    assert BUILTIN_PRICES["claude-haiku-5-5"].input == 0.10
