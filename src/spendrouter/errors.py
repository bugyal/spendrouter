"""The config error both halves of the config raise.

It lives on its own so config.py (route + the file loader) and
config_contain.py (the contain sections) can both raise it without
importing each other. ``spendrouter.config.ConfigError`` stays the public
name — config.py re-exports it.
"""

from __future__ import annotations


class ConfigError(ValueError):
    """Raised when a spendrouter config file is missing or malformed."""
