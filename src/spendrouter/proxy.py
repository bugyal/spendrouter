"""The HTTP reverse proxy on the agent -> LLM API boundary (``spendrouter serve``).

Agents point their SDK's base URL at ``http://127.0.0.1:8787/<upstream>``
(``/openai/v1``, ``/anthropic``, or any upstream you configure). Each
request is attributed, admitted or refused by the
:class:`~spendrouter.contain.Containment` engine, forwarded, metered from the
response (JSON or SSE, relayed chunk by chunk), and written to the ledger.

Attribution comes from one of two places:

* a spendrouter credential (``sr_...``) used as the API key — attribution is
  pinned by the credential, and the daemon swaps in the real provider key,
  so the agent never holds it;
* tag headers (``X-Spendrouter-Agent`` / ``-Task`` / ``-Customer`` / ``-Run``)
  alongside the agent's own provider key, which passes through untouched.

Refusals use the provider's error shape and non-retryable statuses (401,
402, 403, 423), so SDKs raise instead of retrying into the wall.
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import socket
import sys
import threading
import time
import traceback
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO
from urllib.parse import urlsplit

from . import __version__
from .config_contain import Upstream
from .contain import UNATTRIBUTED, Attribution, Containment, Verdict, hash_body
from .credentials import PREFIX, Credential
from .toolscan import scan_request
from .usage import StreamMeter, TokenUsage, parse_response

__all__ = ["HEALTH_PATH", "ProxyServer", "is_inference", "presented_key"]

HEALTH_PATH = "/_spendrouter/health"

# Calls that never run a model. They are ledgered like any other call but
# metered at zero tokens and $0: their bodies are listings, uploads and job
# objects, and a polled batch even echoes the whole batch's usage, so reading
# or estimating a cost from them would bill the same work again on every poll.
# Each endpoint covers its sub-resources (/v1/files/<id>, /v1/batches/<id>/cancel).
NON_INFERENCE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})
NON_INFERENCE_ENDPOINTS = (
    "/v1/models",
    "/v1/files",
    "/v1/fine_tuning/jobs",
    "/v1/batches",
    "/v1/messages/count_tokens",  # Anthropic: token counting is free
    "/v1/messages/batches",  # Anthropic: batches are created and polled here, billed per result
)
TAG_PREFIX = "x-spendrouter-"
MAX_BODY = 64 * 1024 * 1024
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
_AUTH_HEADERS = ("x-api-key", "api-key", "authorization")


def is_inference(method: str, endpoint: str) -> bool:
    """Whether a call can run a model, and so has a cost to meter."""
    if method.upper() in NON_INFERENCE_METHODS:
        return False
    return not any(endpoint == e or endpoint.startswith(e + "/") for e in NON_INFERENCE_ENDPOINTS)


def presented_key(headers: Any) -> str:
    """The API key the client sent; a spendrouter credential wins if several are present."""
    keys = []
    for name in _AUTH_HEADERS:
        value = (headers.get(name) or "").strip()
        if name == "authorization" and value.lower().startswith("bearer "):
            value = value[7:].strip()
        if value:
            keys.append(value)
    return next((k for k in keys if k.startswith(PREFIX)), keys[0] if keys else "")


def _tags(headers: Any) -> dict[str, str]:
    return {f: (headers.get(TAG_PREFIX + f) or "").strip()[:200] for f in ("agent", "task", "customer", "run")}


def _retry_hint(headers: Any) -> bool:
    # x-stainless-retry-count is sent by the official OpenAI and Anthropic SDKs.
    for name in ("x-stainless-retry-count", TAG_PREFIX + "retry"):
        value = (headers.get(name) or "").strip().lower()
        if value and value not in ("0", "false", "no"):
            return True
    return False


def _error_payload(fmt: str, kind: str, message: str) -> dict[str, Any]:
    if fmt == "anthropic":
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message, "type": kind, "code": kind}}


def _decode_json(data: bytes, encoding: str | None) -> Any:
    try:
        enc = (encoding or "").lower()
        if enc == "gzip":
            data = gzip.decompress(data)
        elif enc == "deflate":
            data = zlib.decompress(data)
        return json.loads(data)
    except (ValueError, OSError, EOFError, zlib.error):
        return None


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        engine: Containment,
        host: str | None = None,
        port: int | None = None,
        *,
        log: TextIO | None = None,
        quiet: bool = False,
    ):
        cfg = engine.config.proxy
        host = cfg.host if host is None else host
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.engine = engine
        self.quiet = quiet
        self.log = log or sys.stderr
        self._log_lock = threading.Lock()
        super().__init__((host, cfg.port if port is None else port), ProxyHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        if ":" in str(host):
            host = f"[{host}]"
        return f"http://{host}:{port}"

    def log_line(self, text: str) -> None:
        if not self.quiet:
            with self._log_lock:
                print(text, file=self.log, flush=True)

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="spendrouter-proxy", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"spendrouter/{__version__}"
    timeout = 300  # idle keep-alive connections are dropped after this
    server: ProxyServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # one line per call is logged by _relay instead

    def do_GET(self) -> None:
        self._responded = False
        try:
            self._handle()
        except Exception:
            self.server.log_line("spendrouter: internal error\n" + traceback.format_exc())
            self.close_connection = True
            if not self._responded:
                try:
                    self._send_json(500, _error_payload("openai", "spendrouter_internal_error", "spendrouter: internal proxy error"))
                except OSError:
                    pass

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    # -- request -------------------------------------------------------------

    def _handle(self) -> None:
        started = time.monotonic()
        engine = self.server.engine
        try:
            body = self._read_body()
        except ValueError as exc:
            self.close_connection = True
            self._send_json(400, _error_payload("openai", "spendrouter_bad_request", f"spendrouter: {exc}"))
            return
        if self.path.split("?", 1)[0] == HEALTH_PATH:
            self._send_json(200, {"ok": True, "version": __version__})
            return
        name, _, rest = self.path.lstrip("/").partition("/")
        upstream = engine.config.upstreams.get(name)
        if upstream is None:
            known = ", ".join(sorted(engine.config.upstreams)) or "none"
            message = (
                f"spendrouter: no upstream named {name!r}. Send requests to /<upstream>/<api path>, "
                f"e.g. /openai/v1/chat/completions (configured: {known})"
            )
            self._send_json(404, _error_payload("openai", "spendrouter_unknown_upstream", message))
            return
        path = "/" + rest
        endpoint = path.split("?", 1)[0]

        # Attribution: a credential pins it; otherwise tag headers.
        tags = _tags(self.headers)
        key = presented_key(self.headers)
        credential: Credential | None = None
        if key.startswith(PREFIX):
            credential, refusal = engine.verify_credential(key)
            attr = self._credential_attribution(credential, tags) if credential else self._tag_attribution(tags)
            if refusal is not None:
                engine.record_refusal(attr, refusal, upstream=name, endpoint=endpoint)
                self._refuse(upstream, refusal, attr)
                return
        else:
            attr = self._tag_attribution(tags)
            if engine.config.proxy.require_credential:
                refusal = Verdict(
                    False,
                    "credential",
                    f"spendrouter: this proxy only accepts spendrouter credentials ({PREFIX}...); "
                    "mint one with `spendrouter creds mint --agent <name>`",
                    401,
                )
                engine.record_refusal(attr, refusal, upstream=name, endpoint=endpoint)
                self._refuse(upstream, refusal, attr)
                return

        provider_key = ""
        if credential is not None:
            provider_key = os.environ.get(upstream.api_key_env, "") if upstream.api_key_env else ""
            if not provider_key:
                where = upstream.api_key_env or f"upstreams.{name}.api_key_env"
                refusal = Verdict(
                    False,
                    "upstream_key_missing",
                    f"spendrouter: no provider key for upstream {name!r} — set {where} in the environment of `spendrouter serve`",
                    502,
                )
                engine.record_call(attr, upstream=name, endpoint=endpoint, outcome="error", http_status=502, error_class=refusal.reason)
                self._refuse(upstream, refusal, attr)
                return

        payload: Any = None
        if body[:1] in (b"{", b"["):
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
        model = payload.get("model") if isinstance(payload, dict) and isinstance(payload.get("model"), str) else ""
        tool_results = scan_request(payload, engine.patterns)
        tool_error = next((f"{t.tool}:{t.error_class}"[:200] for t in tool_results if t.is_error), "")
        body_hash = hash_body(endpoint, body) if body else ""

        verdict = engine.admit(
            attr,
            upstream=name,
            endpoint=endpoint,
            model=model,
            tool_results=tool_results,
            body_hash=body_hash,
            retry_hint=_retry_hint(self.headers),
            credential=credential,
        )
        if not verdict.allowed:
            self._refuse(upstream, verdict, attr)
            return

        if (
            engine.config.proxy.inject_stream_usage
            and upstream.format == "openai"
            and isinstance(payload, dict)
            and payload.get("stream") is True
            and endpoint.endswith("/completions")
        ):
            options = payload.get("stream_options") if isinstance(payload.get("stream_options"), dict) else {}
            if options.get("include_usage") is not True:
                payload["stream_options"] = dict(options, include_usage=True)
                body = json.dumps(payload).encode("utf-8")

        record = dict(
            upstream=name,
            endpoint=endpoint,
            body_hash=body_hash,
            retry=verdict.retry,
            tool_error=tool_error,
        )
        target = urlsplit(upstream.base_url)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(target.hostname or "", target.port, timeout=engine.config.proxy.upstream_timeout)
        try:
            try:
                conn.putrequest(self.command, target.path.rstrip("/") + path, skip_host=True, skip_accept_encoding=True)
                for header, value in self._forward_headers(upstream, target.netloc, provider_key, len(body)):
                    conn.putheader(header, value)
                conn.endheaders(body if body else None)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                failure = "upstream_timeout" if isinstance(exc, socket.timeout) else "upstream_unreachable"
                engine.record_call(
                    attr, model=model, outcome="error", http_status=502, error_class=failure,
                    latency_ms=int((time.monotonic() - started) * 1000), **record,  # type: ignore[arg-type]
                )
                message = f"spendrouter: upstream {name!r} ({upstream.base_url}) unreachable: {exc}"
                self._send_json(502, _error_payload(upstream.format, f"spendrouter_{failure}", message))
                self.server.log_line(f"spendrouter 502 {name} agent={attr.agent} {failure}: {exc}")
                return
            self._relay(resp, upstream, attr, model, body, started, record, endpoint)
        finally:
            conn.close()

    def _tag_attribution(self, tags: dict[str, str]) -> Attribution:
        return Attribution(agent=tags["agent"] or UNATTRIBUTED, task=tags["task"], customer=tags["customer"], run=tags["run"])

    def _credential_attribution(self, cred: Credential, tags: dict[str, str]) -> Attribution:
        # Fields the credential pins cannot be overridden; empty ones may be tagged.
        return Attribution(
            agent=cred.agent,
            task=cred.task or tags["task"],
            customer=cred.customer or tags["customer"],
            run=cred.run or tags["run"] or cred.id,
            credential_id=cred.id,
        )

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
            return self._read_chunked()
        length = self.headers.get("content-length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if size < 0 or size > MAX_BODY:
            raise ValueError("request body too large")
        return self.rfile.read(size)

    def _read_chunked(self) -> bytes:
        parts: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(65537)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise ValueError("malformed chunked request body") from None
            if size == 0:
                while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(parts)
            total += size
            if total > MAX_BODY:
                raise ValueError("request body too large")
            parts.append(self.rfile.read(size))
            self.rfile.readline()

    def _forward_headers(self, upstream: Upstream, netloc: str, provider_key: str, length: int) -> list[tuple[str, str]]:
        out = [("Host", netloc)]
        for header, value in self.headers.items():
            lower = header.lower()
            if lower in HOP_BY_HOP or lower in ("host", "content-length", "accept-encoding") or lower.startswith(TAG_PREFIX):
                continue
            if provider_key and lower in _AUTH_HEADERS:
                continue  # the sr_ credential stops here; the real key goes upstream
            out.append((header, value))
        if provider_key:
            if upstream.auth == "bearer":
                out.append(("Authorization", f"Bearer {provider_key}"))
            else:
                out.append((upstream.auth, provider_key))
        out.append(("Accept-Encoding", "identity"))  # so usage can be read from the body
        if length or self.command in ("POST", "PUT", "PATCH"):
            out.append(("Content-Length", str(length)))
        return out

    # -- response ------------------------------------------------------------

    def _relay(
        self,
        resp: http.client.HTTPResponse,
        upstream: Upstream,
        attr: Attribution,
        model: str,
        request_body: bytes,
        started: float,
        record: dict[str, Any],
        endpoint: str = "",
    ) -> None:
        engine = self.server.engine
        status = resp.status
        failure = ""
        meter: StreamMeter | None = None
        response_chars = 0
        if (resp.getheader("content-type") or "").lower().startswith("text/event-stream"):
            meter = StreamMeter(upstream.format)
            failure = self._relay_stream(resp, meter)
            usage, response_model = meter.close(), meter.model
            response_chars = meter.text_chars
        else:
            try:
                data = resp.read()
            except (OSError, http.client.HTTPException):
                data, failure = b"", "upstream_stream_broken"
            if failure:
                self.close_connection = True
                self._send_json(502, _error_payload(upstream.format, "spendrouter_upstream_broken", "spendrouter: upstream response was cut off"))
            else:
                self._start_response(resp, len(data))
                try:
                    self.wfile.write(data)
                except OSError:
                    self.close_connection = True
                    failure = "client_disconnected"
            usage, response_model = parse_response(upstream.format, _decode_json(data, resp.getheader("content-encoding")))
            response_chars = len(data)

        outcome, error_cls = "ok", ""
        if status >= 400:
            outcome, error_cls = "error", f"http_{status}"
        elif failure:
            outcome, error_cls = "error", failure
        elif meter is not None and meter.error_type:
            outcome, error_cls = "error", f"stream_{meter.error_type}"

        estimated = False
        if not is_inference(self.command, endpoint):
            usage = TokenUsage()  # ran no model: zero, whatever usage the body echoes
        elif not usage.found and (outcome == "ok" or (meter is not None and meter.text_chars)):
            # No usage block (a provider that omits it, a cut-off stream): estimate
            # ~4 chars/token rather than record a free call. Flagged in reports.
            usage = TokenUsage(input_tokens=len(request_body) // 4, output_tokens=response_chars // 4)
            estimated = True

        used_model = response_model or model
        latency = int((time.monotonic() - started) * 1000)
        cost = engine.record_call(
            attr,
            model=used_model,
            outcome=outcome,
            http_status=status,
            error_class=error_cls,
            usage=usage,
            estimated=estimated,
            latency_ms=latency,
            **record,  # type: ignore[arg-type]
        )
        flags = "".join(
            [
                " retry" if record.get("retry") else "",
                f" tool_error={record['tool_error']}" if record.get("tool_error") else "",
                f" {error_cls}" if error_cls else "",
                " estimated" if estimated else "",
            ]
        )
        self.server.log_line(
            f"spendrouter {status} {upstream.name} {used_model or '-'} agent={attr.agent}"
            f"{' customer=' + attr.customer if attr.customer else ''}{' task=' + attr.task if attr.task else ''}"
            f" in={usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens} out={usage.output_tokens}"
            f" ${cost:.4f} {latency}ms{flags}"
        )

    def _relay_stream(self, resp: http.client.HTTPResponse, meter: StreamMeter) -> str:
        """Relay an SSE body chunk by chunk. Returns "" or a failure class."""
        self._start_response(resp, None)
        while True:
            try:
                chunk = resp.read1(65536)
            except (OSError, http.client.HTTPException):
                self.close_connection = True  # end without the final chunk: the client sees a cut-off stream
                return "upstream_stream_broken"
            if not chunk:
                break
            meter.feed(chunk)
            try:
                self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            except OSError:
                self.close_connection = True
                return "client_disconnected"
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            self.close_connection = True
        return ""

    def _start_response(self, resp: http.client.HTTPResponse, length: int | None) -> None:
        self._responded = True
        self.send_response_only(resp.status, resp.reason)
        for header, value in resp.getheaders():
            if header.lower() in HOP_BY_HOP or header.lower() == "content-length":
                continue
            self.send_header(header, value)
        if length is None:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _send_json(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self._responded = True
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for header, value in (headers or {}).items():
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, upstream: Upstream, verdict: Verdict, attr: Attribution) -> None:
        # Contract pinned by tests: OpenAI error shape uses error.code = bare
        # refusal reason ("budget") for SDK branching; Anthropic error shape
        # uses error.type = "spendrouter_" + reason (namespaced, since Anthropic
        # types are a fixed vocabulary). Header always = bare reason.
        if upstream.format == "anthropic":
            body = _error_payload(upstream.format, f"spendrouter_{verdict.reason}", verdict.message)
        else:
            body = _error_payload(upstream.format, verdict.reason, verdict.message)
        self._send_json(verdict.status, body, {"X-Spendrouter-Refused": verdict.reason})
        self.server.log_line(f"spendrouter {verdict.status} REFUSED {verdict.reason} {upstream.name} agent={attr.agent}: {verdict.message}")
