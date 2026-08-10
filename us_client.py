"""
us_client.py — US broker factory
==================================
Provides process-wide singleton for the US Dhan Global Stocks broker.
"""

from __future__ import annotations


def get_shared_us_broker(auto_login: bool = True):
    """Return the US broker singleton (Dhan Global Stocks)."""
    from us_broker import get_shared_us_broker as _get

    return _get(auto_login=auto_login)
