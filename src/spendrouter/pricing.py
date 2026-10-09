"""Provider list prices (USD per 1M tokens) and the cost of one metered call.

This is the contain layer's price table: what a call that went through
``spendrouter serve`` actually cost at the provider's rates. It is separate
from the route layer's ``models:`` section on purpose — that one says how
*your plan* prices each tier (quota units, deferred and pay-as-you-go
rates), which is a routing input, not a meter reading.

The built-in table is a starting point, not a source of truth: providers
change prices and ship models faster than any table. Override or extend it
under ``pricing:`` in spendrouter.yml.

Matching is deliberately conservative. A model matches a table key exactly,
or as that key plus a dated-snapshot suffix (``gpt-4o-2024-08-06``,
``claude-sonnet-4-5-20250929``). A model that matches nothing is charged at
the *fallback* price — high on purpose — and flagged as unpriced in reports,
so an unknown model is over-counted, never silently under-counted. That is
also why ``gpt-5-pro`` does not inherit the ``gpt-5`` price.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .usage import TokenUsage

__all__ = ["BUILTIN_PRICES", "FALLBACK_PRICE", "PRICING_AS_OF", "Price", "PriceTable"]

PRICING_AS_OF = "2026-10-06"


@dataclass(frozen=True)
class Price:
    input: float  # USD per 1M uncached input tokens
    output: float  # USD per 1M output tokens
    cache_read: float | None = None  # default: 10% of input
    cache_write: float | None = None  # default: 125% of input (5-minute cache writes)

    def cost(self, usage: TokenUsage) -> float:
        cache_read = self.input * 0.1 if self.cache_read is None else self.cache_read
        cache_write = self.input * 1.25 if self.cache_write is None else self.cache_write
        long_writes = min(usage.cache_write_1h_tokens, usage.cache_write_tokens)
        total = (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * cache_read
            + (usage.cache_write_tokens - long_writes) * cache_write
            + long_writes * self.input * 2.0  # 1-hour TTL writes bill at 2x input
        )
        return total / 1_000_000


# Anthropic first-party rates per Anthropic's model table (cached 2026-10-06).
# Cache writes are 1.25x input (5-minute TTL); reads as listed per model.
# OpenAI rates are standard-tier list prices as published for each model.
BUILTIN_PRICES: dict[str, Price] = {
    # Anthropic
    "claude-fable-5-1": Price(10.0, 50.0, 0.25, 12.5),
    "claude-mythos-5-1": Price(10.0, 50.0, 0.25, 12.5),
    "claude-fable-5": Price(10.0, 50.0, 1.0, 12.5),
    "claude-mythos-5": Price(10.0, 50.0, 1.0, 12.5),
    "claude-opus-5-5": Price(4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": Price(5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": Price(5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-7": Price(5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-6": Price(5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-5": Price(5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-1": Price(15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-5-5": Price(2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": Price(2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-4-6": Price(3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4-5": Price(3.0, 15.0, 0.30, 3.75),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125),  # prompts <=100K tokens
    "claude-haiku-4-5": Price(1.0, 5.0, 0.10, 1.25),
    # OpenAI
    "gpt-5": Price(1.25, 10.0, 0.125),
    "gpt-5-mini": Price(0.25, 2.0, 0.025),
    "gpt-5-nano": Price(0.05, 0.40, 0.005),
    "gpt-4.1": Price(2.0, 8.0, 0.50),
    "gpt-4.1-mini": Price(0.40, 1.60, 0.10),
    "gpt-4.1-nano": Price(0.10, 0.40, 0.025),
    "gpt-4o": Price(2.50, 10.0, 1.25),
    "gpt-4o-mini": Price(0.15, 0.60, 0.075),
    "o1": Price(15.0, 60.0, 7.50),
    "o3": Price(2.0, 8.0, 0.50),
    "o3-mini": Price(1.10, 4.40, 0.55),
    "o4-mini": Price(1.10, 4.40, 0.275),
    "text-embedding-3-small": Price(0.02, 0.0, 0.0),
    "text-embedding-3-large": Price(0.13, 0.0, 0.0),
}

# Charged for models the table does not know. High on purpose (see module doc).
FALLBACK_PRICE = Price(15.0, 75.0)

_SNAPSHOT = re.compile(r"^-(?:\d{8}|\d{4}-\d{2}-\d{2})(?:-.*)?$|^-latest$")
_VENDOR = re.compile(r"^(?:[a-z]{2,4}\.)?(?:anthropic|openai)\.")


def _normalize(model: str) -> str:
    m = model.strip().lower().rsplit("/", 1)[-1]  # openrouter-style "anthropic/claude-..."
    m = _VENDOR.sub("", m)  # bedrock-style "us.anthropic.claude-..."
    m = m.split("@", 1)[0].split(":", 1)[0]  # vertex "@2025...", bedrock ":0"
    if m.startswith("claude-"):
        m = re.sub(r"(?<=\d)\.(?=\d)", "-", m)  # "claude-sonnet-4.5" -> "claude-sonnet-4-5"
    return m


class PriceTable:
    def __init__(self, prices: Mapping[str, Price] | None = None, fallback: Price = FALLBACK_PRICE):
        self.prices: dict[str, Price] = {k.lower(): v for k, v in (prices or BUILTIN_PRICES).items()}
        self.fallback = fallback

    def lookup(self, model: str) -> tuple[Price, str | None]:
        """Return (price, matched key); the key is None when the fallback applies."""
        raw = model.strip().lower()
        norm = _normalize(model)
        for name in (raw, norm):
            if name in self.prices:
                return self.prices[name], name
        for key, price in self.prices.items():
            if any(c in key for c in "*?[") and (fnmatch.fnmatchcase(raw, key) or fnmatch.fnmatchcase(norm, key)):
                return price, key
        best: str | None = None
        for key in self.prices:
            if norm.startswith(key) and _SNAPSHOT.match(norm[len(key) :]) and (best is None or len(key) > len(best)):
                best = key
        if best is not None:
            return self.prices[best], best
        return self.fallback, None

    def cost(self, model: str, usage: TokenUsage) -> tuple[float, bool]:
        """(USD, priced). ``priced`` is False when the fallback price was used."""
        if usage.total_tokens == 0:
            return 0.0, True
        price, key = self.lookup(model)
        return price.cost(usage), key is not None
