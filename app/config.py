"""Centralized environment configuration.

All env reads go through this module so the rest of the codebase never
touches os.environ directly. Makes testing + boot-time validation easy.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw:
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class Config:
    # Ports — Railway injects $PORT for the "main" HTTP server. We run 3
    # services on 3 separate ports inside the same container.
    port: int = _int("PORT", 8080)
    port_candle: int = _int("PORT_CANDLE", 9001)
    port_telegram: int = _int("PORT_TELEGRAM", 9002)
    port_signals: int = _int("PORT_SIGNALS", 9003)

    # Telegram
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    # Node.js bridge
    node_backend_url: str = os.environ.get("NODE_BACKEND_URL", "http://localhost:8080").rstrip("/")
    internal_secret: str = os.environ.get("INTERNAL_SECRET", "dev-internal-secret")

    # Logging
    log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()

    # Signals generator
    signal_interval_sec: int = _int("SIGNAL_INTERVAL_SEC", 30)
    signal_symbols: list[str] = field(
        default_factory=lambda: _list(
            "SIGNAL_SYMBOLS",
            ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC", "EURJPY-OTC"],
        )
    )
    signal_strategies: list[str] = field(
        default_factory=lambda: _list("SIGNAL_STRATEGIES", ["strong", "medium", "pro"])
    )

    # Candle server
    candle_default_limit: int = _int("CANDLE_DEFAULT_LIMIT", 200)


config = Config()
