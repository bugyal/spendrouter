"""spendrouter — route model calls to the cheapest acceptable spend tier.

Treats subscription quota as the cheapest tier, deferred spend as a scarce
budget, and pay-as-you-go as the last resort. Tracks quota + spend in a local
sqlite ledger and refuses to start work that would break a hard cap.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
