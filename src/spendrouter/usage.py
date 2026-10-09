"""Token usage extraction from provider responses — JSON bodies and SSE streams.

Two wire formats are understood:

``anthropic``  Messages API. ``input_tokens`` is the *uncached* remainder;
               cache reads and writes are reported separately. Streams carry
               usage in ``message_start`` and (cumulatively) ``message_delta``.
``openai``     Chat Completions, Responses and Embeddings (and the many
               OpenAI-compatible providers). Cached tokens are a *subset* of
               ``prompt_tokens`` / ``input_tokens``. Chat streams only report
               usage when ``stream_options.include_usage`` is set — the proxy
               sets it — and Responses streams report it in
               ``response.completed``.

Everything is normalised into :class:`TokenUsage`, whose ``input_tokens``
never includes cached tokens, so pricing is a straight weighted sum. (It is
not :class:`spendrouter.ledger.Usage`, the route layer's rolling totals.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = ["TokenUsage", "StreamMeter", "detect_format", "parse_response", "to_dict"]

_MAX_PENDING = 4 * 1024 * 1024  # an SSE line longer than this is not one we can use


@dataclass
class TokenUsage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0  # the part of cache_write billed at the 1-hour TTL rate
    found: bool = False  # a usage block was actually present

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


def _n(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return 0
    return int(value)


def usage_from_anthropic(u: dict[str, Any]) -> TokenUsage:
    write = _n(u.get("cache_creation_input_tokens"))
    breakdown = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    return TokenUsage(
        input_tokens=_n(u.get("input_tokens")),
        output_tokens=_n(u.get("output_tokens")),
        cache_read_tokens=_n(u.get("cache_read_input_tokens")),
        cache_write_tokens=write,
        cache_write_1h_tokens=min(write, _n(breakdown.get("ephemeral_1h_input_tokens"))),
        found=True,
    )


def usage_from_openai(u: dict[str, Any]) -> TokenUsage:
    if "prompt_tokens" in u or "completion_tokens" in u:  # chat completions, embeddings
        prompt, output = _n(u.get("prompt_tokens")), _n(u.get("completion_tokens"))
        details = u.get("prompt_tokens_details")
    else:  # responses API
        prompt, output = _n(u.get("input_tokens")), _n(u.get("output_tokens"))
        details = u.get("input_tokens_details")
    cached = _n(details.get("cached_tokens")) if isinstance(details, dict) else 0
    cached = min(cached, prompt)
    return TokenUsage(input_tokens=prompt - cached, output_tokens=output, cache_read_tokens=cached, found=True)


def _usage_block(obj: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    model = obj.get("model") if isinstance(obj.get("model"), str) else ""
    usage = obj.get("usage")
    inner = obj.get("response")
    if not isinstance(usage, dict) and isinstance(inner, dict):  # responses API stream events
        usage = inner.get("usage")
        model = model or (inner.get("model") if isinstance(inner.get("model"), str) else "")
    return (usage if isinstance(usage, dict) else None), model


def parse_response(fmt: str, obj: Any) -> tuple[TokenUsage, str]:
    """Usage and model name from a decoded (non-streaming) response body."""
    if not isinstance(obj, dict):
        return TokenUsage(), ""
    usage, model = _usage_block(obj)
    if usage is None:
        return TokenUsage(), model
    return (usage_from_anthropic(usage) if fmt == "anthropic" else usage_from_openai(usage)), model


def detect_format(obj: Any) -> str:
    """Best guess at the wire format of a response object (for the in-process API)."""
    if isinstance(obj, dict):
        usage, _ = _usage_block(obj)
        if isinstance(usage, dict):
            if "prompt_tokens" in usage or "input_tokens_details" in usage:
                return "openai"
            if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
                return "anthropic"
        if obj.get("type") == "message":
            return "anthropic"
    return "openai"


def to_dict(response: Any) -> Any:
    """Turn an SDK response object (pydantic model, etc.) into plain data."""
    if isinstance(response, (dict, list)):
        return response
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                continue
    raw = getattr(response, "__dict__", None)
    return dict(raw) if isinstance(raw, dict) else {}


class StreamMeter:
    """Incremental SSE reader: watches a relayed stream for usage, never buffers it."""

    def __init__(self, fmt: str):
        self.fmt = fmt
        self.model = ""
        self.text_chars = 0  # generated text seen, for estimating when no usage arrives
        self.error_type = ""  # set when the stream carried an error event
        self._pending = b""
        self._data: list = []
        self._event = ""
        self._anthropic: dict[str, Any] = {}
        self._usage = TokenUsage()

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    def feed(self, chunk: bytes) -> None:
        self._pending += chunk
        while True:
            nl = self._pending.find(b"\n")
            if nl < 0:
                break
            line, self._pending = self._pending[:nl], self._pending[nl + 1 :]
            self._line(line.rstrip(b"\r"))
        if len(self._pending) > _MAX_PENDING:
            self._pending = b""

    def close(self) -> TokenUsage:
        if self._pending:
            self._line(self._pending.rstrip(b"\r"))
            self._pending = b""
        self._dispatch()
        return self._usage

    def _line(self, line: bytes) -> None:
        if not line:
            self._dispatch()
            return
        if line.startswith(b":"):
            return
        name, _, value = line.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        if name == b"data":
            self._data.append(value)
        elif name == b"event":
            self._event = value.decode("utf-8", "replace").strip()

    def _dispatch(self) -> None:
        data, event = b"\n".join(self._data), self._event
        self._data, self._event = [], ""
        if not data or data.strip() == b"[DONE]":
            return
        try:
            obj = json.loads(data)
        except ValueError:
            return
        if isinstance(obj, dict):
            kind = obj.get("type") if isinstance(obj.get("type"), str) else event
            if self.fmt == "anthropic":
                self._anthropic_event(kind or event, obj)
            else:
                self._openai_event(kind or event, obj)

    def _anthropic_event(self, kind: str, obj: dict[str, Any]) -> None:
        if kind == "message_start":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if isinstance(message.get("model"), str):
                self.model = message["model"]
            self._merge_anthropic(message.get("usage"))
        elif kind == "message_delta":
            self._merge_anthropic(obj.get("usage"))
        elif kind == "content_block_delta":
            delta = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
            for key in ("text", "partial_json", "thinking"):
                if isinstance(delta.get(key), str):
                    self.text_chars += len(delta[key])
        elif kind == "error":
            error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            self.error_type = str(error.get("type") or "stream_error")

    def _merge_anthropic(self, usage: Any) -> None:
        if isinstance(usage, dict):
            self._anthropic.update({k: v for k, v in usage.items() if v is not None})
            self._usage = usage_from_anthropic(self._anthropic)

    def _openai_event(self, kind: str, obj: dict[str, Any]) -> None:
        usage, model = _usage_block(obj)
        if model:
            self.model = model
        if usage is not None:
            self._usage = usage_from_openai(usage)
        for choice in obj.get("choices") or ():
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                if isinstance(delta.get("content"), str):
                    self.text_chars += len(delta["content"])
                for call in delta.get("tool_calls") or ():
                    fn = call.get("function") if isinstance(call, dict) else None
                    if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                        self.text_chars += len(fn["arguments"])
        if kind.endswith(".delta") and isinstance(obj.get("delta"), str):
            self.text_chars += len(obj["delta"])
        if kind in ("error", "response.failed") or (kind != "response.completed" and isinstance(obj.get("error"), dict)):
            error = obj.get("error")
            if not isinstance(error, dict) and isinstance(obj.get("response"), dict):
                error = obj["response"].get("error")
            if isinstance(error, dict):
                self.error_type = str(error.get("type") or error.get("code") or "stream_error")
            else:
                self.error_type = "stream_error"
