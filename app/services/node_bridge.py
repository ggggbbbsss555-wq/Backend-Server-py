"""HTTP client to push events into the Node.js backend server.

Used by:
  - signals_service when a new signal is generated or resolved
  - telegram_bot when a user sends a support message in Telegram
"""

import requests

from app.config import config
from app.utils import logger

HEADERS = {
    "Content-Type": "application/json",
    "X-Internal-Secret": config.internal_secret,
}


def push_signal_new(signal: dict) -> dict | None:
    """POST /internal/signals/new — tell Node.js a new signal was generated."""
    try:
        r = requests.post(
            f"{config.node_backend_url}/internal/signals/new",
            json=signal,
            headers=HEADERS,
            timeout=5,
        )
        if r.ok:
            return r.json().get("data")
        logger.warn("node push signal_new non-2xx", status=r.status_code, body=r.text[:200])
    except Exception as e:
        logger.warn("node push signal_new failed", err=str(e))
    return None


def push_signal_resolve(signal_id: str, result: str, profit: float) -> dict | None:
    """POST /internal/signals/:id/resolve — tell Node.js a trade closed."""
    try:
        r = requests.post(
            f"{config.node_backend_url}/internal/signals/{signal_id}/resolve",
            json={"result": result, "profit": profit},
            headers=HEADERS,
            timeout=5,
        )
        if r.ok:
            return r.json().get("data")
        logger.warn("node push signal_resolve non-2xx", status=r.status_code, body=r.text[:200])
    except Exception as e:
        logger.warn("node push signal_resolve failed", err=str(e))
    return None


def push_support_message(tg_id: str | int, text: str) -> dict | None:
    """POST /internal/support/message — tell Node.js the admin replied via Telegram."""
    try:
        r = requests.post(
            f"{config.node_backend_url}/internal/support/message",
            json={"tg_id": str(tg_id), "text": text},
            headers=HEADERS,
            timeout=5,
        )
        if r.ok:
            return r.json().get("data")
        logger.warn("node push support_message non-2xx", status=r.status_code, body=r.text[:200])
    except Exception as e:
        logger.warn("node push support_message failed", err=str(e))
    return None


def push_bot_status(status: dict) -> dict | None:
    """POST /internal/bot/status — periodic status update to Node.js."""
    try:
        r = requests.post(
            f"{config.node_backend_url}/internal/bot/status",
            json=status,
            headers=HEADERS,
            timeout=5,
        )
        if r.ok:
            return r.json().get("data")
        logger.warn("node push bot_status non-2xx", status=r.status_code, body=r.text[:200])
    except Exception as e:
        logger.warn("node push bot_status failed", err=str(e))
    return None
