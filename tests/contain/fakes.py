"""Test doubles for the contain layer: a controllable clock and request builders."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# 2026-10-09 12:00:00 UTC — a Friday, mid-day, mid-month
NOON = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc).timestamp()


class FakeClock:
    def __init__(self, start: float = NOON):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = 1.0) -> None:
        self.now += seconds


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
