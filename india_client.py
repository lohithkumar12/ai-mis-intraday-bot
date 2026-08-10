"""
india_client.py — India broker factory
======================================
Selects Dhan or Angel One based on config.INDIA_BROKER.
"""

from __future__ import annotations

import config


def get_shared_india_broker(auto_login: bool = True):
    """Return the configured India broker singleton (Dhan or Angel)."""
    broker = (config.INDIA_BROKER or "angel").strip().lower()
    if broker == "dhan":
        from dhan_broker import get_shared_dhan_broker

        return get_shared_dhan_broker(auto_login=auto_login)

    from india_broker import get_shared_india_broker as get_angel

    return get_angel(auto_login=auto_login)
