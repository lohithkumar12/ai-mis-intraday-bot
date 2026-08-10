"""
alerts.py — Optional trade / risk notifications
===============================================
Supports Telegram Bot API and a generic webhook URL.
Disabled when credentials are empty.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID) or bool(
        config.ALERT_WEBHOOK_URL
    )


def _send_telegram(text: str) -> None:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:3500]},
            timeout=8,
        )
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")


def _send_webhook(text: str, event: str) -> None:
    if not config.ALERT_WEBHOOK_URL:
        return
    try:
        requests.post(
            config.ALERT_WEBHOOK_URL,
            json={"text": text, "event": event},
            timeout=8,
        )
    except Exception as e:
        logger.warning(f"Webhook alert failed: {e}")


def notify(text: str, event: str = "info") -> None:
    if not _enabled():
        return

    def _run():
        _send_telegram(f"[{event}] {text}")
        _send_webhook(text, event)

    threading.Thread(target=_run, daemon=True).start()


def trade_alert(market: str, action: str, symbol: str, detail: str = "") -> None:
    notify(f"{market} {action} {symbol} {detail}".strip(), event=action.lower())


def kill_switch_alert(market: str, active: bool) -> None:
    state = "ON" if active else "OFF"
    notify(f"{market} kill switch {state}", event="kill_switch")


def health_alert(message: str) -> None:
    notify(message, event="health")
