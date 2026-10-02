# QuantVexa Python Backend 🐍 (Single File)

Single Python codebase — **all logic in one `main.py` file** so a single edit covers everything.

Runs 3 Flask services + 2 background threads in one process:
- 🕯️ **Candle Service** (port 9001) — `/symbols`, `/candles/<symbol>`
- 🤖 **Telegram Service** (port 9002) — `/send`, `/notify-subscription` + polling thread
- 📈 **Signals Service** (port 9003) — `/signals`, `/bot/status`, `/bot/control` + generator thread
- 🌐 **Main App** (port `$PORT`, default 8080) — unified `/health` (Railway uses this)

## 📦 Single-file structure

```
backend-server-py/
├── main.py            ← ALL logic here (config, services, threads, Node bridge)
├── requirements.txt
├── railway.json
├── Dockerfile
├── Procfile
├── .env.example
└── README.md
```

That's it. One file. No `app/` maze — if something breaks, edit `main.py` and redeploy.

## 🚀 Quick start

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, NODE_BACKEND_URL, INTERNAL_SECRET
python main.py         # → http://localhost:8080
```

## 🌐 Railway deployment

1. Push to GitHub
2. [railway.com](https://railway.com) → **New Project** → **Deploy from GitHub repo** → select this repo
3. Set env vars (see `.env.example`):
   - `TELEGRAM_BOT_TOKEN`
   - `INTERNAL_SECRET` (same as Node.js's)
   - `NODE_BACKEND_URL` (e.g. `https://quantvexa-backend.up.railway.app`)
   - Optional: `SIGNAL_INTERVAL_SEC`, `SIGNAL_SYMBOLS`, `SIGNAL_STRATEGIES`
4. Railway auto-detects `railway.json`, runs:
   ```
   gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 main:app
   ```

## 📡 API

### 🕯️ Candle (port 9001)
- `GET /health`
- `GET /symbols` — 26 trading symbols
- `GET /candles/<symbol>?timeframe=1m&limit=200` — OHLCV candles

### 🤖 Telegram (port 9002)
- `GET /health`
- `POST /send` — `{tg_user_id, text}` → send Telegram message
- `POST /notify-subscription` — `{tg_user_id, status, plan_name}` → notify user

### 📈 Signals (port 9003)
- `GET /health`
- `GET /signals?limit=50` — recent signals
- `GET /bot/status` — strategies + platforms running
- `POST /bot/control` — `{strategy, action}` → start/stop a strategy

### 🌐 Main (port `$PORT`)
- `GET /health` — unified health check (Railway uses this)
- `GET /` — service info

## 🔌 Node.js bridge

Pushes events to Node.js `/internal/*` endpoints with `X-Internal-Secret` header:
- `POST /internal/signals/new` — new signal generated
- `POST /internal/signals/:id/resolve` — trade closed (win/lose)
- `POST /internal/support/message` — user sent a Telegram message
- `POST /internal/bot/status` — bot status update

## 📊 Strategy catalog (mirrors Node.js + admin dashboard)

| Strategy | Color | Win rate | Cost | Signals/day | Accuracy | Duration |
|----------|-------|----------|------|-------------|----------|----------|
| Strong | `#00ff88` | 55% | 1 pt | 15-25 | 70% | 1-3m |
| Medium | `#00d2ff` | 65% | 3 pt | 8-12 | 80% | 2-5m |
| Pro | `#ab82ff` | 78% | 6 pt | 3-5 | 92% | 5-15m |

## ✅ Compatibility with Node.js + admin dashboard
- Strategy catalog mirrors `STRATEGIES` in Node.js `server.js` and the admin dashboard
- Signal shape matches: `{id, symbol, type, entry, duration, startTime, result, profit, strategy, platform, createdAt}`
- Webhook contract matches Node.js's `/internal/*` endpoints
- Symbols catalog mirrors quantvexa chart page (26 pairs: OTC + majors + crypto)

## 📝 License
MIT
