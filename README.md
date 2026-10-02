# QuantVexa Python Backend 🐍

Single Python codebase that runs **3 services** in one process, designed to
deploy on [Railway](https://railway.com) and talk to the
[Node.js bridge server](https://github.com/ggggbbbsss555-wq/Backend-Server).

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────┐
│   Python Backend Server (this repo, 1 process)          │
│                                                          │
│   🕯️ Candle Service   :$PORT_CANDLE   (default 9001)    │
│       GET /symbols                                       │
│       GET /candles/<symbol>?timeframe=1m&limit=200       │
│                                                          │
│   🤖 Telegram Service :$PORT_TELEGRAM  (default 9002)   │
│       POST /send                  (send text to user)    │
│       POST /notify-subscription   (approve/reject msg)   │
│       (+ background polling thread for getUpdates)       │
│                                                          │
│   📈 Signals Service  :$PORT_SIGNALS   (default 9003)   │
│       GET  /signals?limit=50                             │
│       GET  /bot/status                                   │
│       POST /bot/control           (start/stop strategy)  │
│       (+ background signal generator thread)             │
│                                                          │
│   🌐 Main App        :$PORT            (default 8080)   │
│       GET /health  (unified — Railway uses this)         │
└──────────────────────────────────────────────────────────┘
                            ↓
   Pushes events to Node.js backend via internal webhooks:
     POST /internal/signals/new
     POST /internal/signals/:id/resolve
     POST /internal/support/message
     POST /internal/bot/status
```

## 📁 Project structure

```
backend-server-py/
├── main.py                     Entry point — boots 3 Flask apps in threads
├── requirements.txt            Flask + gunicorn + requests + python-telegram-bot
├── railway.json                Railway one-click deploy config
├── Dockerfile                  Docker support
├── Procfile                    Alternative deploy (Heroku-style)
├── .env.example                All env vars documented
└── app/
    ├── __init__.py
    ├── config.py               Centralized env config (dataclass)
    ├── utils/
    │   ├── logger.py           Tiny leveled logger (JSON in prod)
    │   └── helpers.py          ok()/fail() Flask response helpers
    └── services/
        ├── candle_service.py   🕯️  /symbols + /candles/<symbol>
        ├── telegram_bot.py     🤖  /send + /notify-subscription + polling
        ├── signals_service.py  📈  /signals + /bot/status + /bot/control
        └── node_bridge.py      HTTP client for Node.js internal webhooks
```

## 🚀 Quick start (local dev)

```bash
# 1. Install deps
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Copy env template and fill in real values
cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN, NODE_BACKEND_URL, INTERNAL_SECRET

# 3. Run (3 services start in background threads, main app on :8080)
python main.py

# 4. Health check (unified)
curl http://localhost:8080/health
# {
#   "ok": true,
#   "service": "python-backend",
#   "services": {
#     "candle":   {"port": 9001, "url": "http://localhost:9001"},
#     "telegram": {"port": 9002, "url": "http://localhost:9002"},
#     "signals":  {"port": 9003, "url": "http://localhost:9003"}
#   }
# }
```

## 🌐 Railway deployment

1. Push to GitHub (`git push origin main`)
2. Go to [railway.com](https://railway.com) → **New Project** → **Deploy from GitHub repo** → select this repo
3. Set the following environment variables in Railway:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `INTERNAL_SECRET` — long random string (same as Node.js's `INTERNAL_SECRET`)
   - `NODE_BACKEND_URL` — URL of your Node.js backend (e.g. `https://quantvexa-backend.up.railway.app`)
   - Optional: `SIGNAL_INTERVAL_SEC`, `SIGNAL_SYMBOLS`, `SIGNAL_STRATEGIES`
4. Railway auto-detects `railway.json`, runs `pip install -r requirements.txt`, and starts gunicorn
5. You get a public URL like `https://quantvexa-backend-py.up.railway.app`

> **Note on ports:** Railway injects `$PORT` (the main app's port). The 3
> services run on fixed internal ports (9001/9002/9003) inside the container.
> These internal ports are NOT exposed publicly — only the Node.js backend
> needs to reach them, and it does so over the internal network.

> **Note on multi-port:** If you need the 3 services publicly exposed,
> deploy 3 separate Railway services (one per Flask app) instead. The
> single-container approach is simpler for prototyping.

## 📡 API reference

### 🕯️ Candle service (port 9001)

- `GET /health` — service health
- `GET /symbols` — list of 26 trading symbols (OTC + majors + crypto) with current price + 24h change
- `GET /candles/<symbol>?timeframe=1m&limit=200` — OHLCV candles (deterministic per symbol)

### 🤖 Telegram service (port 9002)

- `GET /health` — service health + polling status
- `POST /send` — send a text message to a Telegram user
  - Body: `{"tg_user_id": 123, "text": "Hello!"}`
- `POST /notify-subscription` — notify a user their payment was approved/rejected
  - Body: `{"tg_user_id": 123, "status": "approved"|"rejected", "plan_name": "Pro"}`

The polling thread automatically forwards user messages from Telegram to the
Node.js backend via `POST /internal/support/message`.

### 📈 Signals service (port 9003)

- `GET /health` — service health + generator status + buffered signal count
- `GET /signals?limit=50` — recent signals (newest first, max 500)
- `GET /bot/status` — which strategies are running + per-strategy config
- `POST /bot/control` — start/stop a strategy
  - Body: `{"strategy": "strong"|"medium"|"pro", "action": "start"|"stop"}`

The signal generator runs every `SIGNAL_INTERVAL_SEC` (default 30s) and
pushes new signals to Node.js via `POST /internal/signals/new`. When a
signal's duration elapses, it resolves (win/lose based on the strategy's
winRate) and the result is pushed via `POST /internal/signals/:id/resolve`.

## 🔌 How it talks to Node.js

The Python services push events to the Node.js backend's `/internal/*`
endpoints using the `X-Internal-Secret` header:

| Event | Python source | Node.js endpoint |
|-------|---------------|------------------|
| New signal generated | signals_service generator thread | `POST /internal/signals/new` |
| Signal resolved (win/lose) | signals_service generator thread | `POST /internal/signals/:id/resolve` |
| User sent a support msg in Telegram | telegram_bot polling thread | `POST /internal/support/message` |
| Bot status update | signals_service /bot/control handler | `POST /internal/bot/status` |

Conversely, Node.js calls back to Python:

| Node.js wants | Calls Python endpoint |
|----------------|----------------------|
| Latest candles for chart | `GET /candles/<symbol>` on candle service |
| List of available symbols | `GET /symbols` on candle service |
| Send a Telegram message | `POST /send` on telegram service |
| Notify user of sub approval | `POST /notify-subscription` on telegram service |
| Recent signals history | `GET /signals` on signals service |
| Bot status | `GET /bot/status` on signals service |
| Start/stop a strategy | `POST /bot/control` on signals service |

## 🧪 Strategy catalog (mirrors quantvexa dashboard)

| Strategy | Color | Win rate | Cost | Signals/day | Accuracy | Duration |
|----------|-------|----------|------|-------------|----------|----------|
| Strong | `#00ff88` | 55% | 1 pt | 15-25 | 70% | 1-3m |
| Medium | `#00d2ff` | 65% | 3 pt | 8-12 | 80% | 2-5m |
| Pro | `#ab82ff` | 78% | 6 pt | 3-5 | 92% | 5-15m |

## 🔐 Security

- All `/internal/*` calls to Node.js carry the `X-Internal-Secret` header
- The Telegram polling loop only forwards messages (doesn't execute commands)
- Flask runs with `threaded=True` so slow calls don't block other services

## 🗺️ Roadmap

- [ ] **Real market data** — replace `_gen_candles` with a real OHLCV provider (Binance / OANDA / etc.)
- [ ] **Telegram webhooks** — switch from polling to webhooks when scaling to multiple replicas
- [ ] **Redis pub/sub** — replace HTTP webhooks to Node.js with Redis for higher throughput
- [ ] **Persistent storage** — signals are currently in-memory; add Postgres/Redis for durability
- [ ] **Tests** — pytest + Flask test client

## 📝 License

MIT
