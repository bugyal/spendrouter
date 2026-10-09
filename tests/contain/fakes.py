"""Test doubles for the contain layer: a fake LLM provider API and a controllable clock."""

from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

# 2026-10-09 12:00:00 UTC — a Friday, mid-day, mid-month
NOON = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc).timestamp()


class FakeClock:
    def __init__(self, start: float = NOON):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = 1.0) -> None:
        self.now += seconds


OPENAI_USAGE = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 400}}
ANTHROPIC_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 300,
    "cache_read_input_tokens": 2000,
    "cache_creation_input_tokens": 500,
}


class FakeUpstream:
    """Speaks just enough OpenAI Chat/Responses and Anthropic Messages, JSON and SSE.

    ``fail_status`` makes every request fail with that status. Each request
    is recorded with lower-cased headers and the decoded JSON body.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail_status: int | None = None
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self) -> type:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                with fake._lock:
                    fake.requests.append(
                        {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                    )
                if fake.fail_status:
                    return self._json(fake.fail_status, {"error": {"type": "server_error", "message": "boom"}})
                if self.path == "/v1/models":
                    return self._json(200, {"object": "list", "data": [{"id": "gpt-4o"}]})
                model = (body or {}).get("model", "")
                stream = bool((body or {}).get("stream"))
                if self.path.endswith("/v1/chat/completions"):
                    if stream:
                        include = ((body or {}).get("stream_options") or {}).get("include_usage")
                        return self._sse(fake._openai_chunks(model, include))
                    return self._json(
                        200,
                        {
                            "id": "chatcmpl-1",
                            "object": "chat.completion",
                            "model": model,
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}],
                            "usage": OPENAI_USAGE,
                        },
                    )
                if self.path.endswith("/v1/responses"):
                    return self._json(
                        200,
                        {
                            "id": "resp_1",
                            "object": "response",
                            "model": model,
                            "output": [],
                            "usage": {"input_tokens": 800, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 0}},
                        },
                    )
                if self.path.endswith("/v1/messages"):
                    if stream:
                        return self._sse(fake._anthropic_events(model))
                    return self._json(
                        200,
                        {
                            "id": "msg_1",
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [{"type": "text", "text": "Hello"}],
                            "stop_reason": "end_turn",
                            "usage": ANTHROPIC_USAGE,
                        },
                    )
                return self._json(404, {"error": {"message": "no such path"}})

            do_POST = do_GET

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Upstream", "fake")
                self.end_headers()
                self.wfile.write(data)

            def _sse(self, events: list[bytes]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for event in events:
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(event), event))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

        return Handler

    @staticmethod
    def _openai_chunks(model: str, include_usage: bool) -> list[bytes]:
        def chunk(obj: dict[str, Any]) -> bytes:
            return b"data: " + json.dumps(obj).encode() + b"\n\n"

        base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": model}
        out = [
            chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}),
            chunk({**base, "choices": [{"index": 0, "delta": {"content": "Hello"}}]}),
            chunk({**base, "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": "stop"}]}),
        ]
        if include_usage:
            out.append(chunk({**base, "choices": [], "usage": OPENAI_USAGE}))
        out.append(b"data: [DONE]\n\n")
        return out

    @staticmethod
    def _anthropic_events(model: str) -> list[bytes]:
        def event(name: str, obj: dict[str, Any]) -> bytes:
            return f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode()

        start_usage = dict(ANTHROPIC_USAGE, output_tokens=1)
        return [
            event("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "model": model, "usage": start_usage}}),
            event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
            event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 300}}),
            event("message_stop", {"type": "message_stop"}),
        ]


def http_call(
    base_url: str, method: str, path: str, payload: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        body = json.dumps(payload).encode() if payload is not None else None
        hdrs = {"Content-Type": "application/json"} if payload is not None else {}
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def anthropic_tool_turn(i: int, error: str) -> dict[str, Any]:
    """A Messages request whose newest turn feeds back a failed tool call."""
    return {
        "model": "claude-sonnet-5-5",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "Look up the order status for ACME-42"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"toolu_{i}", "name": "crm_lookup", "input": {"order": "ACME-42"}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}", "is_error": True, "content": error}],
            },
        ],
    }
