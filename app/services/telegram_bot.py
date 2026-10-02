"""🤖 Telegram bot service — receives user messages + sends notifications.

Two responsibilities:
  1. **HTTP server** (Flask) — called by Node.js to send messages/notifications
     to users via Telegram:
       POST /send                  → send arbitrary text to a user
       POST /notify-subscription   → notify user of approval/rejection

  2. **Polling thread** — long-polls Telegram getUpdates so when a user
     sends a support message in Telegram, we forward it to Node.js via
     POST /internal/support/message. Node.js then broadcasts it to the
     Web App support chat.

In production with multiple replicas you'd switch polling → webhooks,
but for Railway single-replica polling is fine.
"""

import threading
import time

import requests
from flask import Flask, Blueprint, request

from app.config import config
from app.utils.helpers import ok, fail
from app.utils import logger
from app.services import node_bridge

bp = Blueprint("telegram", __name__)

TELEGRAM_API = f"https://api.telegram.org/bot{config.telegram_bot_token}" if config.telegram_bot_token else ""
_polling_thread = None
_polling_stop = threading.Event()
_last_update_id = 0  # offset for getUpdates


def _tg_call(method: str, payload: dict | None = None, timeout: int = 10) -> dict | None:
    """Call a Telegram Bot API method."""
    if not TELEGRAM_API:
        return None
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=payload or {}, timeout=timeout)
        if not r.ok:
            logger.warn("telegram api non-2xx", method=method, status=r.status_code, body=r.text[:200])
            return None
        return r.json()
    except Exception as e:
        logger.warn("telegram api failed", method=method, err=str(e))
        return None


def send_message(tg_user_id: str | int, text: str) -> dict | None:
    """Send a text message to a Telegram user."""
    return _tg_call("sendMessage", {"chat_id": tg_user_id, "text": text, "parse_mode": "HTML"})


# === HTTP routes (called by Node.js) ===

@bp.route("/health", methods=["GET"])
def health():
    return ok({
        "service": "telegram",
        "status": "running",
        "bot_configured": bool(config.telegram_bot_token),
        "polling": _polling_thread is not None and _polling_thread.is_alive(),
    })


@bp.route("/send", methods=["POST"])
def send():
    """POST /send — Node.js asks us to send a Telegram message.

    Body: { tg_user_id: 123, text: "..." }
    """
    data = request.get_json(silent=True) or {}
    tg_id = data.get("tg_user_id")
    text = data.get("text", "")
    if not tg_id or not text:
        return fail("BAD_REQUEST", "tg_user_id and text required", 400)

    result = send_message(tg_id, text)
    if result is None:
        return fail("TELEGRAM_FAILED", "Failed to send Telegram message", 502)
    return ok(result.get("result"))


@bp.route("/notify-subscription", methods=["POST"])
def notify_subscription():
    """POST /notify-subscription — tell a user their subscription was approved/rejected.

    Body: { tg_user_id, status: 'approved'|'rejected', plan_name }
    """
    data = request.get_json(silent=True) or {}
    tg_id = data.get("tg_user_id")
    status = data.get("status")
    plan_name = data.get("plan_name", "")

    if not tg_id or status not in ("approved", "rejected"):
        return fail("BAD_REQUEST", "tg_user_id + status (approved|rejected) required", 400)

    if status == "approved":
        text = (
            f"✅ <b>Payment Verified</b>\n\n"
            f"Your payment has been verified and you've been upgraded to the "
            f"<b>{plan_name}</b> plan. Enjoy your new features!"
        )
    else:
        text = (
            f"❌ <b>Payment Rejected</b>\n\n"
            f"Your payment for the <b>{plan_name}</b> plan was rejected. "
            f"This is your final warning — please contact support for details."
        )

    result = send_message(tg_id, text)
    if result is None:
        return fail("TELEGRAM_FAILED", "Failed to send notification", 502)
    return ok(result.get("result"))


# === Polling thread ===

def _polling_loop():
    """Long-poll Telegram getUpdates and forward support messages to Node.js."""
    global _last_update_id
    logger.info("telegram polling started")
    while not _polling_stop.is_set():
        if not TELEGRAM_API:
            # No bot token configured — sleep and retry (lets us run in dev)
            time.sleep(10)
            continue
        try:
            payload = {"timeout": 25, "allowed_updates": ["message"]}
            if _last_update_id:
                payload["offset"] = _last_update_id + 1
            r = requests.post(f"{TELEGRAM_API}/getUpdates", json=payload, timeout=30)
            if not r.ok:
                logger.warn("telegram getUpdates non-2xx", status=r.status_code, body=r.text[:200])
                time.sleep(5)
                continue
            data = r.json()
            for update in data.get("result", []):
                _last_update_id = update.get("update_id", _last_update_id)
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                user = msg.get("from") or {}
                text = msg.get("text", "")
                if not text:
                    continue
                # Forward to Node.js — it'll store the message + broadcast to admin
                user_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
                logger.info("support message received", tg_id=user.get("id"), name=user_name, text=text[:60])
                node_bridge.push_support_message(
                    tg_id=str(user.get("id")),
                    text=f"[{user_name}] {text}",  # include name so admin knows who sent it
                )
        except requests.exceptions.Timeout:
            # Normal — long-poll timed out, just loop again
            continue
        except Exception as e:
            logger.warn("polling error", err=str(e))
            time.sleep(5)
    logger.info("telegram polling stopped")


def start_polling():
    """Start the polling thread (call once at boot)."""
    global _polling_thread
    if _polling_thread and _polling_thread.is_alive():
        return
    _polling_stop.clear()
    _polling_thread = threading.Thread(target=_polling_loop, name="tg-poller", daemon=True)
    _polling_thread.start()


def stop_polling():
    """Stop the polling thread (call on shutdown)."""
    _polling_stop.set()


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app
