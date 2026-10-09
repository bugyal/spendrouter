"""spendrouter — one agent-spend tool, in two layers.

ROUTE, before a call: subscription quota is the cheapest tier, deferred spend
a scarce budget, pay-as-you-go the last resort; work that would break a hard
cap is refused before it starts. CONTAIN, while agents run: ``spendrouter
serve`` meters every real call, trips a circuit breaker on loops, refuses
calls over a hard cap before they reach the provider, and hands agents
scoped, expiring ``sr_`` credentials. One sqlite ledger, stdlib only.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
