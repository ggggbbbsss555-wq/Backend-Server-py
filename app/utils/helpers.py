"""Shared helpers — JSON responses, internal-secret check, time formatting."""

import time
from flask import jsonify, request

from app.config import config


def ok(data=None, status=200):
    """Standard success response: {ok: true, data: ...}"""
    return jsonify({"ok": True, "data": data}), status


def fail(code: str = "INTERNAL", message: str = "Something went wrong", status: int = 500, details=None):
    """Standard error response: {ok: false, error: {code, message, details?}}"""
    err = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return jsonify({"ok": False, "error": err}), status


def require_internal_secret():
    """Returns True if the request has the correct X-Internal-Secret header."""
    secret = request.headers.get("X-Internal-Secret", "")
    return bool(secret) and secret == config.internal_secret


def now_ms() -> int:
    """Current epoch time in milliseconds."""
    return int(time.time() * 1000)


def now_sec() -> int:
    """Current epoch time in seconds."""
    return int(time.time())


def fmt_iso(ms_or_iso: int | str | None) -> str:
    """Convert epoch-ms to ISO 8601 string."""
    if ms_or_iso is None:
        return None
    if isinstance(ms_or_iso, str):
        return ms_or_iso
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms_or_iso / 1000, tz=timezone.utc).isoformat()
