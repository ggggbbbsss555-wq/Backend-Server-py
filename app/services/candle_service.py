"""🕯️ Candle service — serves OHLCV data for the chart page.

Routes:
    GET /symbols                  → list of trading symbols
    GET /health                   → health check
    GET /candles/<symbol>         → OHLCV candles
        ?timeframe=1m             → candle timeframe (1m, 5m, 15m, 1h, 4h, 1d)
        ?limit=200                → number of candles (max 1000)

The candle data is generated deterministically from the symbol name so the
same symbol always returns the same chart shape (looks like real market data).
When a real market data provider is wired in, replace `_gen_candles` with
a fetch to that provider — the route shape stays the same.
"""

import hashlib
import math
import random
import time

from flask import Flask, Blueprint, request

from app.config import config
from app.utils.helpers import ok, fail
from app.utils import logger

bp = Blueprint("candle", __name__)

# === Symbols catalog ===
# Mirrors the chart page's SYMBOLS list (OTC pairs + majors + crypto).
SYMBOLS = [
    # OTC exotic pairs
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
    # Majors
    {"symbol": "EURUSD-OTC", "price": 1.0852, "change": 0.18},
    {"symbol": "GBPUSD-OTC", "price": 1.3025, "change": -0.12},
    {"symbol": "USDJPY-OTC", "price": 149.85, "change": 0.27},
    {"symbol": "AUDUSD-OTC", "price": 0.6582, "change": -0.08},
    {"symbol": "USDCAD-OTC", "price": 1.3585, "change": 0.15},
    {"symbol": "EURJPY-OTC", "price": 163.45, "change": 0.31},
    {"symbol": "EURGBP-OTC", "price": 0.8338, "change": -0.05},
    {"symbol": "GBPJPY-OTC", "price": 195.42, "change": 0.22},
    # Crypto
    {"symbol": "BTC/USDT", "price": 67234.50, "change": 1.42},
    {"symbol": "ETH/USDT", "price": 3456.20, "change": 0.95},
    {"symbol": "SOL/USDT", "price": 178.50, "change": -0.31},
    {"symbol": "XRP/USDT", "price": 0.5432, "change": 0.18},
    {"symbol": "AVAX/USDT", "price": 38.76, "change": -0.55},
    {"symbol": "DOGE/USDT", "price": 0.1654, "change": 0.34},
]

TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def _seed_from(symbol: str) -> int:
    """Deterministic seed from symbol name so charts look stable across calls."""
    h = hashlib.sha256(symbol.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def _gen_candles(symbol: str, timeframe: str, limit: int) -> list[dict]:
    """Generate `limit` candles for the given symbol + timeframe.

    Uses a deterministic seed per symbol so the chart shape is consistent.
    The last close of the previous batch becomes the first open of the next,
    giving the appearance of a continuous price series.
    """
    seed = _seed_from(symbol)
    rng = random.Random(seed)
    # Find the symbol's base price
    sym_info = next((s for s in SYMBOLS if s["symbol"] == symbol), None)
    base_price = sym_info["price"] if sym_info else 100.0
    # Volatility scales with price — higher-priced symbols move more in absolute terms
    volatility = max(0.0005, math.log10(base_price + 1) * 0.001)

    tf_sec = TIMEFRAME_SECONDS.get(timeframe, 60)
    now = int(time.time())
    start_time = now - (limit * tf_sec)

    candles = []
    price = base_price
    for i in range(limit):
        ts = start_time + (i * tf_sec)
        # Random walk with mean reversion to base_price
        drift = (base_price - price) * 0.02
        change = rng.gauss(drift, volatility * price)
        open_p = price
        close_p = max(0.0001, price + change)
        # High/low extend beyond open/close by a random factor
        wick_up = abs(rng.gauss(0, volatility * price * 0.5))
        wick_dn = abs(rng.gauss(0, volatility * price * 0.5))
        high = max(open_p, close_p) + wick_up
        low = min(open_p, close_p) - wick_dn
        volume = rng.randint(100, 10_000)
        candles.append({
            "time": ts,
            "open": round(open_p, 5),
            "high": round(high, 5),
            "low": round(low, 5),
            "close": round(close_p, 5),
            "volume": volume,
        })
        price = close_p
    return candles


# === Routes ===

@bp.route("/health", methods=["GET"])
def health():
    return ok({"service": "candle", "status": "running", "symbols": len(SYMBOLS)})


@bp.route("/symbols", methods=["GET"])
def symbols():
    """Return the symbol catalog (with current price + 24h change)."""
    return ok(SYMBOLS)


@bp.route("/candles/<path:symbol>", methods=["GET"])
def candles(symbol: str):
    """Return OHLCV candles for the given symbol.

    Query params:
        timeframe: 1m (default), 5m, 15m, 1h, 4h, 1d
        limit: number of candles (default 200, max 1000)
    """
    timeframe = request.args.get("timeframe", "1m")
    if timeframe not in TIMEFRAME_SECONDS:
        return fail("BAD_TIMEFRAME", f"Unsupported timeframe: {timeframe}", 400)
    try:
        limit = int(request.args.get("limit", config.candle_default_limit))
    except ValueError:
        return fail("BAD_LIMIT", "limit must be an integer", 400)
    limit = max(1, min(limit, 1000))

    candles = _gen_candles(symbol, timeframe, limit)
    logger.info("candles served", symbol=symbol, timeframe=timeframe, count=len(candles))
    return ok({
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
    })


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app
