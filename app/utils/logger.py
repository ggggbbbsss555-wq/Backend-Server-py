"""Tiny leveled logger — no deps, prints to stdout.

Format: [LEVEL] message {json_meta}
JSON in prod, colored in dev.
"""

import json
import sys
from datetime import datetime, timezone

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
COLORS = {"DEBUG": "\033[90m", "INFO": "\033[36m", "WARN": "\033[33m", "ERROR": "\033[31m"}
RESET = "\033[0m"


def _emit(level: str, msg: str, meta: dict | None = None) -> None:
    from app.config import config
    if LEVELS.get(level, 0) < LEVELS.get(config.log_level, 20):
        return
    line = {"t": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg}
    if meta:
        line.update(meta)
    if os_environ_prod():
        sys.stdout.write(json.dumps(line) + "\n")
    else:
        color = COLORS.get(level, "")
        meta_str = (" " + json.dumps(meta)) if meta else ""
        sys.stdout.write(f"{color}[{level}]{RESET} {msg}{meta_str}\n")
    sys.stdout.flush()


def os_environ_prod() -> bool:
    import os
    return os.environ.get("NODE_ENV") == "production" or os.environ.get("RAILWAY_ENVIRONMENT") is not None


def debug(msg: str, **meta) -> None: _emit("DEBUG", msg, meta or None)
def info(msg: str, **meta) -> None:  _emit("INFO",  msg, meta or None)
def warn(msg: str, **meta) -> None:  _emit("WARN",  msg, meta or None)
def error(msg: str, **meta) -> None: _emit("ERROR", msg, meta or None)
