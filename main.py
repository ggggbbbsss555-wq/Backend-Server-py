"""
QuantVexa Python Backend — Single-file server
=================================================

Runs 3 Flask services + 2 background threads in one process:
  🕯️ Candle Service   :PORT_CANDLE   (default 9001)
  🤖 Telegram Service :PORT_TELEGRAM  (default 9002)
  📈 Signals Service  :PORT_SIGNALS   (default 9003)
  🌐 Main App         :PORT           (default 8080, Railway uses this)

Background threads:
  - Telegram polling (forwards user messages to Node.js)
  - Signals generator (creates a new signal every SIGNAL_INTERVAL_SEC,
    pushes to Node.js via /internal/signals/new)

All config, services, threads, and the Node.js bridge client live in
THIS file so a single edit covers everything.

Compatibility:
  - Mirrors the admin dashboard's data shape (Basic/Pro/Elite plans,
    Binance/TRC20/BEP20 payments, Strong/Medium/Pro strategies,
    QUOTEX/BINOLLA platforms)
  - Mirrors the Node.js backend's internal-webhook contract
    (/internal/signals/new, /internal/signals/:id/resolve,
     /internal/support/message, /internal/bot/status)
"""

import os
import sys
import time
import json
import random
import uuid
import hashlib
import math
import signal as signal_module
import threading
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from werkzeug.serving import make_server, run_simple
from dotenv import load_dotenv

load_dotenv()


# ============================================================================
# CONFIG
# ============================================================================
def _int(name, default):
    try: return int(os.environ.get(name, default))
    except (TypeError, ValueError): return default


def _list(name, default):
    raw = os.environ.get(name, "")
    if not raw: return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class Config:
    port: int = _int("PORT", 8080)
    port_candle: int = _int("PORT_CANDLE", 9001)
    port_telegram: int = _int("PORT_TELEGRAM", 9002)
    port_signals: int = _int("PORT_SIGNALS", 9003)
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    node_backend_url: str = os.environ.get("NODE_BACKEND_URL", "http://localhost:8080").rstrip("/")
    internal_secret: str = os.environ.get("INTERNAL_SECRET", "dev-internal-secret")
    log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()
    signal_interval_sec: int = _int("SIGNAL_INTERVAL_SEC", 30)
    signal_symbols: list = field(default_factory=lambda: _list("SIGNAL_SYMBOLS", [
        "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC", "EURJPY-OTC"]))
    signal_strategies: list = field(default_factory=lambda: _list("SIGNAL_STRATEGIES", ["strong", "medium", "pro"]))
    candle_default_limit: int = _int("CANDLE_DEFAULT_LIMIT", 200)


config = Config()


# ============================================================================
# LOGGER
# ============================================================================
LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
COLORS = {"DEBUG": "\033[90m", "INFO": "\033[36m", "WARN": "\033[33m", "ERROR": "\033[31m"}
RESET = "\033[0m"


def _is_prod():
    return os.environ.get("NODE_ENV") == "production" or os.environ.get("RAILWAY_ENVIRONMENT") is not None


def _emit(level, msg, meta):
    if LEVELS.get(level, 0) < LEVELS.get(config.log_level, 20):
        return
    line = {"t": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg}
    if meta: line.update(meta)
    if _is_prod():
        sys.stdout.write(json.dumps(line) + "\n")
    else:
        color = COLORS.get(level, "")
        meta_str = (" " + json.dumps(meta)) if meta else ""
        sys.stdout.write(f"{color}[{level}]{RESET} {msg}{meta_str}\n")
    sys.stdout.flush()


def log_debug(msg, **meta): _emit("DEBUG", msg, meta or None)
def log_info(msg, **meta):  _emit("INFO",  msg, meta or None)
def log_warn(msg, **meta):  _emit("WARN",  msg, meta or None)
def log_error(msg, **meta): _emit("ERROR", msg, meta or None)


# ============================================================================
# HELPERS
# ============================================================================
def ok_response(data=None, status=200):
    return jsonify({"ok": True, "data": data}), status


def fail_response(code="INTERNAL", message="Something went wrong", status=500, details=None):
    err = {"code": code, "message": message}
    if details is not None: err["details"] = details
    return jsonify({"ok": False, "error": err}), status


def now_ms(): return int(time.time() * 1000)
def now_sec(): return int(time.time())


# ============================================================================
# NODE BRIDGE — HTTP client to push events to Node.js backend
# ============================================================================
NODE_HEADERS = {
    "Content-Type": "application/json",
    "X-Internal-Secret": config.internal_secret,
}


def node_push(path: str, payload: dict) -> dict | None:
    """POST to Node.js /internal/* endpoint. Returns response data or None."""
    try:
        r = requests.post(f"{config.node_backend_url}{path}", json=payload, headers=NODE_HEADERS, timeout=5)
        if r.ok:
            return r.json().get("data")
        log_warn("node push non-2xx", path=path, status=r.status_code, body=r.text[:200])
    except Exception as e:
        log_warn("node push failed", path=path, err=str(e))
    return None


def node_push_signal_new(signal: dict):
    return node_push("/internal/signals/new", signal)


def node_push_signal_resolve(signal_id: str, result: str, profit: float):
    return node_push(f"/internal/signals/{signal_id}/resolve", {"result": result, "profit": profit})


def node_push_support_message(tg_id: str, text: str):
    return node_push("/internal/support/message", {"tg_id": str(tg_id), "text": text})


def node_push_bot_status(status: dict):
    return node_push("/internal/bot/status", status)


# ============================================================================
# 🕯️ CANDLE SERVICE
# ============================================================================
SYMBOLS = [
    {"symbol": "BRLUSD-OTC", "price": 0.1985, "change": 0.42},
    {"symbol": "USDARS-OTC", "price": 985.50, "change": -0.31},
    {"symbol": "USDBDT-OTC", "price": 117.25, "change": 0.18},
    {"symbol": "USDCOP-OTC", "price": 4150.75, "change": -0.55},
    {"symbol": "USDEGP-OTC", "price": 48.85, "change": 0.12},
    {"symbol": "USDIDR-OTC", "price": 15820.50, "change": -0.28},
    {"symbol": "USDINR-OTC", "price": 83.42, "change": 0.22},
    {"symbol": "USDMXN-OTC", "price": 17.15, "change": -0.41},
    {"symbol": "USDNGN-OTC", "price": 1485.30, "change": 0.67},
    {"symbol": "USDPHP-OTC", "price": 56.78, "change": -0.19},
    {"symbol": "USDPKR-OTC", "price": 278.45, "change": 0.34},
    {"symbol": "USDZAR-OTC", "price": 18.92, "change": -0.48},
    {"symbol": "EURUSD-OTC", "price": 1.0852, "change": 0.18},
    {"symbol": "GBPUSD-OTC", "price": 1.3025, "change": -0.12},
    {"symbol": "USDJPY-OTC", "price": 149.85, "change": 0.27},
    {"symbol": "AUDUSD-OTC", "price": 0.6582, "change": -0.08},
    {"symbol": "USDCAD-OTC", "price": 1.3585, "change": 0.15},
    {"symbol": "EURJPY-OTC", "price": 163.45, "change": 0.31},
    {"symbol": "EURGBP-OTC", "price": 0.8338, "change": -0.05},
    {"symbol": "GBPJPY-OTC", "price": 195.42, "change": 0.22},
    {"symbol": "BTC/USDT", "price": 67234.50, "change": 1.42},
    {"symbol": "ETH/USDT", "price": 3456.20, "change": 0.95},
    {"symbol": "SOL/USDT", "price": 178.50, "change": -0.31},
    {"symbol": "XRP/USDT", "price": 0.5432, "change": 0.18},
    {"symbol": "AVAX/USDT", "price": 38.76, "change": -0.55},
    {"symbol": "DOGE/USDT", "price": 0.1654, "change": 0.34},
]

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _seed_from(symbol):
    h = hashlib.sha256(symbol.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def _gen_candles(symbol, timeframe, limit):
    seed = _seed_from(symbol)
    rng = random.Random(seed)
    sym_info = next((s for s in SYMBOLS if s["symbol"] == symbol), None)
    base_price = sym_info["price"] if sym_info else 100.0
    volatility = max(0.0005, math.log10(base_price + 1) * 0.001)
    tf_sec = TIMEFRAME_SECONDS.get(timeframe, 60)
    now = int(time.time())
    start_time = now - (limit * tf_sec)
    candles = []
    price = base_price
    for i in range(limit):
        ts = start_time + (i * tf_sec)
        drift = (base_price - price) * 0.02
        change = rng.gauss(drift, volatility * price)
        open_p = price
        close_p = max(0.0001, price + change)
        wick_up = abs(rng.gauss(0, volatility * price * 0.5))
        wick_dn = abs(rng.gauss(0, volatility * price * 0.5))
        high = max(open_p, close_p) + wick_up
        low = min(open_p, close_p) - wick_dn
        volume = rng.randint(100, 10_000)
        candles.append({
            "time": ts, "open": round(open_p, 5), "high": round(high, 5),
            "low": round(low, 5), "close": round(close_p, 5), "volume": volume,
        })
        price = close_p
    return candles


def create_candle_app():
    app = Flask("candle")

    @app.route("/health")
    def health():
        return ok_response({"service": "candle", "status": "running", "symbols": len(SYMBOLS)})

    @app.route("/symbols")
    def symbols():
        return ok_response(SYMBOLS)

    @app.route("/candles/<path:symbol>")
    def candles(symbol):
        timeframe = request.args.get("timeframe", "1m")
        if timeframe not in TIMEFRAME_SECONDS:
            return fail_response("BAD_TIMEFRAME", f"Unsupported timeframe: {timeframe}", 400)
        try: limit = int(request.args.get("limit", config.candle_default_limit))
        except ValueError: return fail_response("BAD_LIMIT", "limit must be an integer", 400)
        limit = max(1, min(limit, 1000))
        candles_data = _gen_candles(symbol, timeframe, limit)
        log_info("candles served", symbol=symbol, timeframe=timeframe, count=len(candles_data))
        return ok_response({"symbol": symbol, "timeframe": timeframe, "candles": candles_data})

    return app


# ============================================================================
# 🤖 TELEGRAM BOT SERVICE
# ============================================================================
TELEGRAM_API = f"https://api.telegram.org/bot{config.telegram_bot_token}" if config.telegram_bot_token else ""
_tg_polling_thread = None
_tg_polling_stop = threading.Event()
_tg_last_update_id = 0


def _tg_call(method, payload=None, timeout=10):
    if not TELEGRAM_API: return None
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=payload or {}, timeout=timeout)
        if not r.ok:
            log_warn("telegram api non-2xx", method=method, status=r.status_code, body=r.text[:200])
            return None
        return r.json()
    except Exception as e:
        log_warn("telegram api failed", method=method, err=str(e))
        return None


def tg_send_message(tg_user_id, text):
    return _tg_call("sendMessage", {"chat_id": tg_user_id, "text": text, "parse_mode": "HTML"})


def _tg_polling_loop():
    global _tg_last_update_id
    log_info("telegram polling started")
    while not _tg_polling_stop.is_set():
        if not TELEGRAM_API:
            time.sleep(10)
            continue
        try:
            payload = {"timeout": 25, "allowed_updates": ["message"]}
            if _tg_last_update_id:
                payload["offset"] = _tg_last_update_id + 1
            r = requests.post(f"{TELEGRAM_API}/getUpdates", json=payload, timeout=30)
            if not r.ok:
                log_warn("telegram getUpdates non-2xx", status=r.status_code, body=r.text[:200])
                time.sleep(5)
                continue
            for update in r.json().get("result", []):
                _tg_last_update_id = update.get("update_id", _tg_last_update_id)
                msg = update.get("message") or {}
                user = msg.get("from") or {}
                text = msg.get("text", "")
                if not text: continue
                user_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
                log_info("support message received", tg_id=user.get("id"), name=user_name, text=text[:60])
                node_push_support_message(str(user.get("id")), f"[{user_name}] {text}")
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log_warn("polling error", err=str(e))
            time.sleep(5)
    log_info("telegram polling stopped")


def start_tg_polling():
    global _tg_polling_thread
    if _tg_polling_thread and _tg_polling_thread.is_alive(): return
    _tg_polling_stop.clear()
    _tg_polling_thread = threading.Thread(target=_tg_polling_loop, name="tg-poller", daemon=True)
    _tg_polling_thread.start()


def stop_tg_polling():
    _tg_polling_stop.set()


def create_telegram_app():
    app = Flask("telegram")

    @app.route("/health")
    def health():
        return ok_response({
            "service": "telegram", "status": "running",
            "bot_configured": bool(config.telegram_bot_token),
            "polling": _tg_polling_thread is not None and _tg_polling_thread.is_alive(),
        })

    @app.route("/send", methods=["POST"])
    def send():
        data = request.get_json(silent=True) or {}
        tg_id = data.get("tg_user_id")
        text = data.get("text", "")
        if not tg_id or not text:
            return fail_response("BAD_REQUEST", "tg_user_id and text required", 400)
        result = tg_send_message(tg_id, text)
        if result is None:
            return fail_response("TELEGRAM_FAILED", "Failed to send Telegram message", 502)
        return ok_response(result.get("result"))

    @app.route("/notify-subscription", methods=["POST"])
    def notify_subscription():
        data = request.get_json(silent=True) or {}
        tg_id = data.get("tg_user_id")
        status = data.get("status")
        plan_name = data.get("plan_name", "")
        if not tg_id or status not in ("approved", "rejected"):
            return fail_response("BAD_REQUEST", "tg_user_id + status (approved|rejected) required", 400)
        if status == "approved":
            text = (f"✅ <b>Payment Verified</b>\n\nYour payment has been verified and you've been "
                    f"upgraded to the <b>{plan_name}</b> plan. Enjoy your new features!")
        else:
            text = (f"❌ <b>Payment Rejected</b>\n\nYour payment for the <b>{plan_name}</b> plan was "
                    f"rejected. This is your final warning — please contact support for details.")
        result = tg_send_message(tg_id, text)
        if result is None:
            return fail_response("TELEGRAM_FAILED", "Failed to send notification", 502)
        return ok_response(result.get("result"))

    return app


# ============================================================================
# 📈 SIGNALS SERVICE
# ============================================================================
STRATEGIES = {
    "strong": {"id": "strong", "name": "Strong", "color": "#00ff88", "winRate": 0.55,
               "costPerSignal": 1, "signalsPerDay": "15-25", "accuracy": "70%", "duration": "1-3m",
               "min_sec": 60, "max_sec": 180},
    "medium": {"id": "medium", "name": "Medium", "color": "#00d2ff", "winRate": 0.65,
               "costPerSignal": 3, "signalsPerDay": "8-12", "accuracy": "80%", "duration": "2-5m",
               "min_sec": 120, "max_sec": 300},
    "pro":    {"id": "pro", "name": "Pro", "color": "#ab82ff", "winRate": 0.78,
               "costPerSignal": 6, "signalsPerDay": "3-5", "accuracy": "92%", "duration": "5-15m",
               "min_sec": 300, "max_sec": 900},
}

_signals_lock = threading.Lock()
_signals: list[dict] = []
_running_strategies: set[str] = set(config.signal_strategies)
_sig_gen_thread = None
_sig_gen_stop = threading.Event()


def _gen_signal():
    enabled = [s for s in _running_strategies if s in STRATEGIES]
    if not enabled: return None
    strategy_id = random.choice(enabled)
    strat = STRATEGIES[strategy_id]
    symbol = random.choice(config.signal_symbols)
    sig_type = random.choice(["buy", "sell"])
    duration = random.randint(strat["min_sec"], strat["max_sec"])
    base_prices = {
        "EURUSD-OTC": 1.0852, "GBPUSD-OTC": 1.3025, "USDJPY-OTC": 149.85,
        "AUDUSD-OTC": 0.6582, "USDCAD-OTC": 1.3585, "EURJPY-OTC": 163.45,
    }
    entry = base_prices.get(symbol, round(random.uniform(0.5, 200.0), 4))
    return {
        "id": "sig_" + uuid.uuid4().hex[:12],
        "symbol": symbol, "type": sig_type, "entry": entry,
        "duration": duration, "startTime": now_sec(),
        "result": None, "profit": None,
        "strategy": strategy_id, "platform": "QUOTEX",
        "createdAt": now_sec() * 1000,
    }


def _resolve_signal(sig):
    strat = STRATEGIES.get(sig["strategy"], STRATEGIES["strong"])
    win = random.random() < strat["winRate"]
    sig["result"] = "win" if win else "lose"
    bp = random.uniform(0.3, 1.8)
    sig["profit"] = round(bp if win else -(bp * 0.7), 2)
    sig["resolvedAt"] = now_sec() * 1000


def _sig_gen_loop():
    log_info("signals generator started", interval=config.signal_interval_sec, strategies=list(_running_strategies))
    next_signal_at = time.time()
    while not _sig_gen_stop.is_set():
        now = time.time()
        # 1) Resolve expired signals
        with _signals_lock:
            for sig in _signals:
                if sig["result"] is not None: continue
                elapsed = now - sig["startTime"]
                if elapsed >= sig["duration"]:
                    _resolve_signal(sig)
                    try: node_push_signal_resolve(sig["id"], sig["result"], sig["profit"])
                    except Exception as e: log_warn("signal resolve push failed", id=sig["id"], err=str(e))
        # 2) Generate a new signal
        if now >= next_signal_at and _running_strategies:
            sig = _gen_signal()
            if sig:
                with _signals_lock:
                    _signals.insert(0, sig)
                    if len(_signals) > 500: _signals[:] = _signals[:500]
                log_info("signal generated", id=sig["id"], symbol=sig["symbol"], type=sig["type"], strategy=sig["strategy"])
                try: node_push_signal_new(sig)
                except Exception as e: log_warn("signal new push failed", id=sig["id"], err=str(e))
            next_signal_at = now + config.signal_interval_sec
        time.sleep(1)
    log_info("signals generator stopped")


def start_sig_generator():
    global _sig_gen_thread
    if _sig_gen_thread and _sig_gen_thread.is_alive(): return
    _sig_gen_stop.clear()
    _sig_gen_thread = threading.Thread(target=_sig_gen_loop, name="sig-gen", daemon=True)
    _sig_gen_thread.start()


def stop_sig_generator():
    _sig_gen_stop.set()


def create_signals_app():
    app = Flask("signals")

    @app.route("/health")
    def health():
        with _signals_lock:
            signals_count = len(_signals)
            active = sum(1 for s in _signals if s["result"] is None)
        return ok_response({
            "service": "signals", "status": "running",
            "generator_running": _sig_gen_thread is not None and _sig_gen_thread.is_alive(),
            "running_strategies": list(_running_strategies),
            "signals_buffered": signals_count, "signals_active": active,
        })

    @app.route("/signals")
    def signals():
        try: limit = int(request.args.get("limit", 50))
        except ValueError: return fail_response("BAD_LIMIT", "limit must be an integer", 400)
        limit = max(1, min(limit, 500))
        with _signals_lock:
            return ok_response([dict(s) for s in _signals[:limit]])

    @app.route("/bot/status")
    def bot_status():
        with _signals_lock:
            signals_count = len(_signals)
            active = sum(1 for s in _signals if s["result"] is None)
        return ok_response({
            "running": bool(_running_strategies),
            "strategies": [{**STRATEGIES[sid], "enabled": sid in _running_strategies} for sid in STRATEGIES],
            "platforms": [{"id": "QUOTEX", "running": True}, {"id": "BINOLLA", "running": True}],
            "signals_total": signals_count, "signals_active": active,
        })

    @app.route("/bot/control", methods=["POST"])
    def bot_control():
        data = request.get_json(silent=True) or {}
        strategy = data.get("strategy")
        action = data.get("action")
        if strategy not in STRATEGIES:
            return fail_response("BAD_STRATEGY", f"Unknown strategy: {strategy}", 400)
        if action not in ("start", "stop"):
            return fail_response("BAD_ACTION", "action must be 'start' or 'stop'", 400)
        with _signals_lock:
            if action == "start": _running_strategies.add(strategy)
            else: _running_strategies.discard(strategy)
        log_info("bot control", strategy=strategy, action=action, running=list(_running_strategies))
        try: node_push_bot_status({"strategies": list(_running_strategies)})
        except Exception: pass
        return ok_response({"strategy": strategy, "action": action, "running_strategies": list(_running_strategies)})

    return app


# ============================================================================
# 🌐 MAIN APP — unified health check on $PORT
# ============================================================================
main_app = Flask("main")


@main_app.route("/health")
def main_health():
    return jsonify({
        "ok": True, "service": "python-backend",
        "services": {
            "candle":   {"port": config.port_candle,   "url": f"http://localhost:{config.port_candle}"},
            "telegram": {"port": config.port_telegram, "url": f"http://localhost:{config.port_telegram}"},
            "signals":  {"port": config.port_signals,  "url": f"http://localhost:{config.port_signals}"},
        },
        "ts": int(time.time() * 1000),
    })


@main_app.route("/")
def main_root():
    return jsonify({"name": "QuantVexa Python Backend", "services": ["candle", "telegram", "signals"], "health": "/health"})


# ============================================================================
# SERVER THREAD HELPER
# ============================================================================
class ServerThread(threading.Thread):
    def __init__(self, app, host, port, name):
        super().__init__(name=name, daemon=True)
        self.app = app; self.host = host; self.port = port; self.name = name; self.srv = None

    def run(self):
        self.srv = make_server(self.host, self.port, self.app, threaded=True)
        log_info(f"{self.name} listening", host=self.host, port=self.port)
        try: self.srv.serve_forever()
        except Exception as e: log_error(f"{self.name} crashed", err=str(e))

    def shutdown(self):
        if self.srv: self.srv.shutdown()


# ============================================================================
# BOOT
# ============================================================================
def boot():
    log_info("🚀 Python backend starting",
             port=config.port, candle_port=config.port_candle,
             telegram_port=config.port_telegram, signals_port=config.port_signals,
             node_url=config.node_backend_url)

    candle_thread   = ServerThread(create_candle_app(),   "0.0.0.0", config.port_candle,   "candle-svc")
    telegram_thread = ServerThread(create_telegram_app(), "0.0.0.0", config.port_telegram, "telegram-svc")
    signals_thread  = ServerThread(create_signals_app(),  "0.0.0.0", config.port_signals,  "signals-svc")
    candle_thread.start(); telegram_thread.start(); signals_thread.start()

    if config.telegram_bot_token:
        start_tg_polling()
        log_info("telegram polling enabled")
    else:
        log_warn("TELEGRAM_BOT_TOKEN not set — polling disabled")

    start_sig_generator()
    log_info("signals generator enabled", interval=config.signal_interval_sec, symbols=len(config.signal_symbols))

    def shutdown(*_):
        log_info("shutting down...")
        stop_sig_generator(); stop_tg_polling()
        candle_thread.shutdown(); telegram_thread.shutdown(); signals_thread.shutdown()
        time.sleep(0.5); sys.exit(0)

    signal_module.signal(signal_module.SIGTERM, shutdown)
    signal_module.signal(signal_module.SIGINT, shutdown)
    return shutdown


# Gunicorn entry point — Railway's Procfile uses `main:app`.
# Boot the 3 services the moment this module is imported.
app = main_app
_shutdown_fn = boot()


if __name__ == "__main__":
    # When run directly (no gunicorn), block on the main Flask app.
    log_info(f"main app listening on :{config.port}")
    run_simple("0.0.0.0", config.port, main_app, threaded=True)
