"""📈 Signals service — generates trading signals + exposes bot control.

Routes:
    GET  /health
    GET  /signals?limit=50        → recent signals (newest first)
    GET  /bot/status              → strategies running + signal count
    POST /bot/control             → start/stop a strategy

The signals generator runs in a background thread. Every SIGNAL_INTERVAL_SEC
seconds, it picks a random symbol + strategy and creates a new signal,
then pushes it to Node.js via /internal/signals/new. After the signal's
duration elapses, it resolves (win/lose) and pushes the result via
/internal/signals/:id/resolve.
"""

import random
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from flask import Flask, Blueprint, request

from app.config import config
from app.utils.helpers import ok, fail, now_sec, fmt_iso
from app.utils import logger
from app.services import node_bridge

bp = Blueprint("signals", __name__)

# === Strategy catalog — mirrors quantvexa dashboard STRATEGIES ===
STRATEGIES = {
    "strong": {
        "id": "strong", "name": "Strong",
        "color": "#00ff88", "winRate": 0.55,
        "costPerSignal": 1, "signalsPerDay": "15-25",
        "accuracy": "70%", "duration": "1-3m",
        "min_sec": 60, "max_sec": 180,
    },
    "medium": {
        "id": "medium", "name": "Medium",
        "color": "#00d2ff", "winRate": 0.65,
        "costPerSignal": 3, "signalsPerDay": "8-12",
        "accuracy": "80%", "duration": "2-5m",
        "min_sec": 120, "max_sec": 300,
    },
    "pro": {
        "id": "pro", "name": "Pro",
        "color": "#ab82ff", "winRate": 0.78,
        "costPerSignal": 6, "signalsPerDay": "3-5",
        "accuracy": "92%", "duration": "5-15m",
        "min_sec": 300, "max_sec": 900,
    },
}

# === In-memory state ===
_signals_lock = threading.Lock()
_signals: list[dict] = []  # newest first
_running_strategies: set[str] = set(config.signal_strategies)  # enabled at startup
_generator_thread = None
_generator_stop = threading.Event()


def _gen_signal() -> dict:
    """Create one new signal — pick a random enabled strategy + random symbol."""
    enabled = [s for s in _running_strategies if s in STRATEGIES]
    if not enabled:
        return None
    strategy_id = random.choice(enabled)
    strat = STRATEGIES[strategy_id]
    symbol = random.choice(config.signal_symbols)
    sig_type = random.choice(["buy", "sell"])
    duration = random.randint(strat["min_sec"], strat["max_sec"])

    # Base entry price — use a plausible value per symbol
    base_prices = {
        "EURUSD-OTC": 1.0852, "GBPUSD-OTC": 1.3025, "USDJPY-OTC": 149.85,
        "AUDUSD-OTC": 0.6582, "USDCAD-OTC": 1.3585, "EURJPY-OTC": 163.45,
    }
    entry = base_prices.get(symbol, round(random.uniform(0.5, 200.0), 4))

    return {
        "id": "sig_" + uuid.uuid4().hex[:12],
        "symbol": symbol,
        "type": sig_type,
        "entry": entry,
        "duration": duration,
        "startTime": now_sec(),
        "result": None,
        "profit": None,
        "strategy": strategy_id,
        "platform": "QUOTEX",
        "createdAt": now_sec() * 1000,
    }


def _resolve_signal(sig: dict):
    """Resolve a signal — win/lose based on the strategy's winRate."""
    strat = STRATEGIES.get(sig["strategy"], STRATEGIES["strong"])
    win = random.random() < strat["winRate"]
    sig["result"] = "win" if win else "lose"
    # Profit/loss percentage — wins gain 0.3%-1.8%, losses lose 0.4%-1.6%
    bp = random.uniform(0.3, 1.8)
    sig["profit"] = round(bp if win else -(bp * 0.7), 2)
    sig["resolvedAt"] = now_sec() * 1000


def _generator_loop():
    """Background thread: generate signals + resolve them after duration."""
    logger.info("signals generator started",
                interval=config.signal_interval_sec,
                strategies=list(_running_strategies))
    next_signal_at = time.time()
    while not _generator_stop.is_set():
        now = time.time()

        # 1) Check for signals that need resolving
        with _signals_lock:
            for sig in _signals:
                if sig["result"] is not None:
                    continue
                elapsed = now - sig["startTime"]
                if elapsed >= sig["duration"]:
                    _resolve_signal(sig)
                    # Push resolution to Node.js (don't block — fire and forget)
                    try:
                        node_bridge.push_signal_resolve(sig["id"], sig["result"], sig["profit"])
                    except Exception as e:
                        logger.warn("signal resolve push failed", id=sig["id"], err=str(e))

        # 2) Generate a new signal if it's time
        if now >= next_signal_at and _running_strategies:
            sig = _gen_signal()
            if sig:
                with _signals_lock:
                    _signals.insert(0, sig)
                    # Cap buffer at 500
                    if len(_signals) > 500:
                        _signals[:] = _signals[:500]
                logger.info("signal generated",
                            id=sig["id"], symbol=sig["symbol"],
                            type=sig["type"], strategy=sig["strategy"])
                # Push to Node.js
                try:
                    node_bridge.push_signal_new(sig)
                except Exception as e:
                    logger.warn("signal new push failed", id=sig["id"], err=str(e))
            next_signal_at = now + config.signal_interval_sec

        # Sleep 1s so we don't burn CPU
        time.sleep(1)
    logger.info("signals generator stopped")


# === Routes ===

@bp.route("/health", methods=["GET"])
def health():
    return ok({
        "service": "signals",
        "status": "running",
        "generator_running": _generator_thread is not None and _generator_thread.is_alive(),
        "running_strategies": list(_running_strategies),
        "signals_buffered": len(_signals),
    })


@bp.route("/signals", methods=["GET"])
def signals():
    """GET /signals?limit=50 → recent signals (newest first)."""
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return fail("BAD_LIMIT", "limit must be an integer", 400)
    limit = max(1, min(limit, 500))
    with _signals_lock:
        # Return a copy so the response isn't mutated by the generator
        return ok([dict(s) for s in _signals[:limit]])


@bp.route("/bot/status", methods=["GET"])
def bot_status():
    """GET /bot/status → which strategies are running + signal count."""
    with _signals_lock:
        signals_count = len(_signals)
        active = sum(1 for s in _signals if s["result"] is None)
    return ok({
        "running": bool(_running_strategies),
        "strategies": [
            {**STRATEGIES[sid], "enabled": sid in _running_strategies}
            for sid in STRATEGIES
        ],
        "platforms": [
            {"id": "QUOTEX", "running": True},
            {"id": "BINOLLA", "running": True},
        ],
        "signals_total": signals_count,
        "signals_active": active,
    })


@bp.route("/bot/control", methods=["POST"])
def bot_control():
    """POST /bot/control — start/stop a strategy.

    Body: { strategy: 'strong'|'medium'|'pro', action: 'start'|'stop' }
    """
    data = request.get_json(silent=True) or {}
    strategy = data.get("strategy")
    action = data.get("action")
    if strategy not in STRATEGIES:
        return fail("BAD_STRATEGY", f"Unknown strategy: {strategy}", 400)
    if action not in ("start", "stop"):
        return fail("BAD_ACTION", "action must be 'start' or 'stop'", 400)

    with _signals_lock:
        if action == "start":
            _running_strategies.add(strategy)
        else:
            _running_strategies.discard(strategy)

    logger.info("bot control", strategy=strategy, action=action, running=list(_running_strategies))

    # Push status update to Node.js (best-effort)
    try:
        node_bridge.push_bot_status({"strategies": list(_running_strategies)})
    except Exception:
        pass

    return ok({
        "strategy": strategy,
        "action": action,
        "running_strategies": list(_running_strategies),
    })


# === Lifecycle ===

def start_generator():
    """Start the signals generator thread (call once at boot)."""
    global _generator_thread
    if _generator_thread and _generator_thread.is_alive():
        return
    _generator_stop.clear()
    _generator_thread = threading.Thread(target=_generator_loop, name="sig-gen", daemon=True)
    _generator_thread.start()


def stop_generator():
    """Stop the generator thread."""
    _generator_stop.set()


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app
