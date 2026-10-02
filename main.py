"""🚀 Main entry point — boots all 3 Flask services in a single process.

Architecture (single container, 3 ports):
    ┌──────────────────────────────────────────────────────────┐
    │  Python Backend (this process)                           │
    │                                                          │
    │  Thread 1: Candle Service   → port $PORT_CANDLE (9001)   │
    │  Thread 2: Telegram Service → port $PORT_TELEGRAM (9002) │
    │  Thread 3: Signals Service  → port $PORT_SIGNALS (9003)  │
    │  Thread 4: TG polling loop  (no HTTP)                   │
    │  Thread 5: Signals generator (no HTTP)                  │
    │                                                          │
    │  Main thread: $PORT (8080) — proxies to /health          │
    └──────────────────────────────────────────────────────────┘

Railway assigns $PORT automatically — we use it as the "main" entry point
that returns a unified health check. The other 3 services run on fixed
ports that the Node.js backend knows about (PYTHON_CANDLE_URL etc.).
"""

import os
import signal
import sys
import threading
import time
from werkzeug.serving import make_server

from app.config import config
from app.utils import logger
from app.services.candle_service import create_app as create_candle_app
from app.services.telegram_bot import create_app as create_telegram_app, start_polling, stop_polling
from app.services.signals_service import create_app as create_signals_app, start_generator, stop_generator
from flask import Flask, jsonify


# === The "main" app on $PORT — unified health check + service discovery ===
main_app = Flask(__name__)


@main_app.route("/health")
def health():
    """Unified health check — Railway uses this."""
    return jsonify({
        "ok": True,
        "service": "python-backend",
        "services": {
            "candle":   {"port": config.port_candle,   "url": f"http://localhost:{config.port_candle}"},
            "telegram": {"port": config.port_telegram, "url": f"http://localhost:{config.port_telegram}"},
            "signals":  {"port": config.port_signals,  "url": f"http://localhost:{config.port_signals}"},
        },
        "ts": int(time.time() * 1000),
    })


@main_app.route("/")
def root():
    return jsonify({
        "name": "QuantVexa Python Backend",
        "services": ["candle", "telegram", "signals"],
        "health": "/health",
    })


# === Helper to run a Flask app in a background thread ===
class ServerThread(threading.Thread):
    def __init__(self, app, host: str, port: int, name: str):
        super().__init__(name=name, daemon=True)
        self.app = app
        self.host = host
        self.port = port
        self.name = name
        self.srv = None

    def run(self):
        self.srv = make_server(self.host, self.port, self.app, threaded=True)
        logger.info(f"{self.name} listening", host=self.host, port=self.port)
        try:
            self.srv.serve_forever()
        except Exception as e:
            logger.error(f"{self.name} crashed", err=str(e))

    def shutdown(self):
        if self.srv:
            self.srv.shutdown()


# === Main ===
def main():
    logger.info("🚀 Python backend starting",
                port=config.port,
                candle_port=config.port_candle,
                telegram_port=config.port_telegram,
                signals_port=config.port_signals,
                node_url=config.node_backend_url)

    # Boot the 3 Flask services in background threads
    candle_thread = ServerThread(create_candle_app(), "0.0.0.0", config.port_candle, "candle-svc")
    telegram_thread = ServerThread(create_telegram_app(), "0.0.0.0", config.port_telegram, "telegram-svc")
    signals_thread = ServerThread(create_signals_app(), "0.0.0.0", config.port_signals, "signals-svc")

    candle_thread.start()
    telegram_thread.start()
    signals_thread.start()

    # Start the Telegram polling loop (no HTTP, just a thread)
    if config.telegram_bot_token:
        start_polling()
        logger.info("telegram polling enabled")
    else:
        logger.warn("TELEGRAM_BOT_TOKEN not set — polling disabled")

    # Start the signals generator thread
    start_generator()
    logger.info("signals generator enabled",
                interval=config.signal_interval_sec,
                symbols=len(config.signal_symbols))

    # Graceful shutdown handler
    def shutdown(*_):
        logger.info("shutting down...")
        stop_generator()
        stop_polling()
        candle_thread.shutdown()
        telegram_thread.shutdown()
        signals_thread.shutdown()
        time.sleep(0.5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    return shutdown


# Gunicorn entry point — Railway's Procfile uses `main:app`.
# When gunicorn imports this module, it looks for an `app` variable. We
# expose `app = main_app` (a Flask instance) so gunicorn can serve it.
# But we ALSO need to boot the 3 background services — so we call main()
# at import time. main() starts the threads + returns a shutdown function
# we don't need here (gunicorn handles signals itself).
app = main_app

# Boot the 3 services the moment this module is imported (covers both
# `python main.py` and `gunicorn main:app` paths).
_shutdown_fn = main()


if __name__ == "__main__":
    # When run directly (no gunicorn), block the main thread on the main Flask app.
    # Under gunicorn, gunicorn itself calls serve_forever() on `app`.
    logger.info(f"main app listening on :{config.port}")
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", config.port, main_app, threaded=True)
