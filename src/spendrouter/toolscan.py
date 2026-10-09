"""Find the tool results an agent is feeding back to the model, and classify errors.

A runaway agent rarely looks like one failing API call. It looks like a
healthy model call, again and again, each carrying the *same tool failure*
back to the model. The proxy sees that in the request body: the tool results
appended since the model's last turn. This module extracts them from the
three common request shapes:

* Anthropic Messages      ``tool_result`` blocks (``is_error`` is honoured)
* OpenAI Chat Completions ``role: "tool"`` (and legacy ``"function"``) messages
* OpenAI Responses        ``function_call_output`` items

Only results *after the last assistant turn* count — older ones are history
the agent re-sends every call, and counting them would multiply every
failure by the conversation length.

Error classes are normalised so that "timed out after 30s" and "timed out
after 31s" are the same failure: exception names, errno codes and HTTP
statuses are kept, numbers and quoted values are masked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["DEFAULT_ERROR_PATTERNS", "ToolResult", "compile_patterns", "error_class", "scan_request"]

DEFAULT_ERROR_PATTERNS = (
    r"^\s*(?:error|exception|traceback|fatal|failed|failure)\b",
    r"\b[A-Za-z_][\w.]*(?:Error|Exception)\s*:",
    r"^\s*\{\s*\"(?:error|err)\"\s*:",
    r"\bstatus(?:[ _]?code)?\s*[:=]?\s*[45]\d\d\b",
    r"\bE(?:CONNREFUSED|CONNRESET|TIMEDOUT|NOTFOUND|HOSTUNREACH|NETUNREACH|AI_AGAIN)\b",
)

_SAMPLE = 2000  # only the head of a tool result is inspected
_ERRNO = re.compile(
    r"\b(E(?:CONNREFUSED|CONNRESET|CONNABORTED|TIMEDOUT|NOTFOUND|HOSTUNREACH|NETUNREACH|PIPE"
    r"|ACCES|NOENT|AI_AGAIN|PERM|MFILE|NOSPC))\b"
)
_EXCEPTION = re.compile(
    r"\b((?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Timeout|Refused|Denied|Failure|Fault))\b"
)
_HTTP = re.compile(
    r"(?:\bstatus(?:[ _]?code)?\s*[:=]?\s*|\bHTTP(?:/\d(?:\.\d)?)?\s*[:=]?\s*|\bcode\s*[:=]\s*)([45]\d\d)\b"
    r"|\b([45]\d\d)\s+(?:Bad Request|Unauthorized|Payment Required|Forbidden|Not Found|Conflict"
    r"|Too Many Requests|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out)",
    re.IGNORECASE,
)
_LABEL = re.compile(r"^(?:error|exception|fatal|failed|failure)\s*[:\-]\s*", re.IGNORECASE)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_QUOTED = re.compile(r"(['\"`]).*?\1")
_HEX = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_NUM = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ToolResult:
    tool: str
    call_id: str
    error_class: str  # "" when the result is not an error

    @property
    def is_error(self) -> bool:
        return bool(self.error_class)


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns]


_DEFAULT_COMPILED = compile_patterns(DEFAULT_ERROR_PATTERNS)


def error_class(text: str) -> str:
    """A short, stable label for an error message."""
    sample = text[:_SAMPLE]
    parts = []
    named = _ERRNO.search(sample) or _EXCEPTION.search(sample)
    if named:
        parts.append(named.group(1).rsplit(".", 1)[-1])
    http = _HTTP.search(sample)
    if http:
        parts.append("http_" + (http.group(1) or http.group(2)))
    if parts:
        return ":".join(parts)
    line = next((ln.strip() for ln in sample.splitlines() if ln.strip()), "")
    line = _LABEL.sub("", line)
    line = _UUID.sub("<id>", line)
    line = _QUOTED.sub("<s>", line)
    line = _HEX.sub("#", line)
    line = _NUM.sub("#", line)
    line = re.sub(r"\s+", " ", line).strip().lower()
    return line[:60] or "error"


def classify(text: str, flagged: bool, patterns: Sequence[re.Pattern[str]] = _DEFAULT_COMPILED) -> str:
    """Error class of a tool result, or "" if it does not look like an error."""
    sample = text[:_SAMPLE]
    if not flagged and not any(p.search(sample) for p in patterns):
        return ""
    return error_class(sample)


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif "content" in block:
                    parts.append(_text(block["content"]))
        return "\n".join(p for p in parts if p)
    try:
        return json.dumps(content, default=str)
    except (TypeError, ValueError):
        return str(content)


def scan_request(body: Any, patterns: Sequence[re.Pattern[str]] = _DEFAULT_COMPILED) -> list[ToolResult]:
    """Tool results added to the conversation since the model's last turn."""
    if not isinstance(body, dict):
        return []
    if isinstance(body.get("messages"), list):
        return _scan_messages([m for m in body["messages"] if isinstance(m, dict)], patterns)
    if isinstance(body.get("input"), list):
        return _scan_responses_input([m for m in body["input"] if isinstance(m, dict)], patterns)
    return []


def _scan_messages(messages: list[dict[str, Any]], patterns: Sequence[re.Pattern[str]]) -> list[ToolResult]:
    names: dict[str, str] = {}
    last_assistant = -1
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        last_assistant = i
        for call in msg.get("tool_calls") or ():  # openai chat
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                names[str(call.get("id"))] = str(call["function"].get("name") or "unknown")
        if isinstance(msg.get("content"), list):  # anthropic
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    names[str(block.get("id"))] = str(block.get("name") or "unknown")
    found = []
    for msg in messages[last_assistant + 1 :]:
        role = msg.get("role")
        if role == "tool":
            ref = str(msg.get("tool_call_id") or "")
            name = names.get(ref) or str(msg.get("name") or "unknown")
            found.append(ToolResult(name, ref, classify(_text(msg.get("content")), False, patterns)))
        elif role == "function":
            found.append(ToolResult(str(msg.get("name") or "unknown"), "", classify(_text(msg.get("content")), False, patterns)))
        elif role == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    ref = str(block.get("tool_use_id") or "")
                    flagged = block.get("is_error") is True
                    found.append(ToolResult(names.get(ref, "unknown"), ref, classify(_text(block.get("content")), flagged, patterns)))
    return found


def _scan_responses_input(items: list[dict[str, Any]], patterns: Sequence[re.Pattern[str]]) -> list[ToolResult]:
    names: dict[str, str] = {}
    last_assistant = -1
    for i, item in enumerate(items):
        kind = item.get("type")
        if kind in ("function_call", "custom_tool_call"):
            names[str(item.get("call_id"))] = str(item.get("name") or "unknown")
            last_assistant = i
        elif item.get("role") == "assistant" and kind in (None, "message"):
            last_assistant = i
    found = []
    for item in items[last_assistant + 1 :]:
        if item.get("type") in ("function_call_output", "custom_tool_call_output"):
            ref = str(item.get("call_id") or "")
            found.append(ToolResult(names.get(ref, "unknown"), ref, classify(_text(item.get("output")), False, patterns)))
    return found
