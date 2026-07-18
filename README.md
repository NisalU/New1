# AI Trading Signal Bot

A pure-Python crypto trading signal bot that analyzes Binance market data with
13 strategies and serves a live charting dashboard accessible from any device
on the same network.

Built on **aiohttp + WebSocket** for realtime updates: live trade ticks move
the last candle and the price with smooth animation, klines stream in as they
form, and analysis snapshots / signals push instantly to every connected
browser. No numpy / pandas — only `aiohttp`, `websockets`, `requests`, and
`python-dotenv`, so it installs cleanly on Linux, macOS, or Termux.

## Strategies (confluence scored)

| Strategy | What it detects |
| --- | --- |
| EMA 7/25/99 | Trend stack alignment, fresh 7/25 crosses |
| Support / Resistance | Clustered swing levels, bounces, breakouts |
| Trendlines | Regression-fit trendlines, bounces and breaks |
| Chart Patterns | Engulfing, pin bars, double top/bottom, head & shoulders |
| Fibonacci | Retracement of dominant swing (0.5 / 0.618 golden zone) |
| Smart Money Concepts | BOS / CHoCH, order blocks, fair value gaps, breaker blocks |
| Liquidity Sweeps | Stop hunts through equal highs/lows that reverse |
| Orderflow / CVD | Delta pressure, CVD divergence, absorption |
| Auction Market | Volume profile POC, value area acceptance/rejection |
| Fundamentals | Funding rate, open interest, long/short ratio |
| VWAP / AVWAP | Session VWAP, anchored VWAP, σ bands |
| Kill Zones | Institutional session timing (London, NY, Asia) |
| Wyckoff Phase | Accumulation/distribution phase, Spring, UTAD |

Each strategy votes -1..+1 and is weighted (see `config.py`). A LONG/SHORT
signal fires when the composite score crosses the threshold (default 20/100),
together with an ATR-based trade plan (entry / stop / TP1 / TP2).

## Install

```bash
git clone https://github.com/NisalU/New1.git signal-bot
cd signal-bot
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
# edit .env with your preferred editor
```

Keys the bot uses:

| Variable | Required | Purpose |
| --- | --- | --- |
| `GROQ_API_KEY` | Yes (for AI) | AI analyst — free key at https://console.groq.com/keys |
| `BINANCE_API_KEY` | No | Higher rate limits + private endpoints |
| `BINANCE_API_SECRET` | No | Required alongside `BINANCE_API_KEY` for signing |
| `CMC_API_KEY` | No | Enriches coin scanner with market-cap data |
| `GROQ_MODEL` | No | Pin a specific Groq model (default: `llama-3.3-70b-versatile`) |

Keys are read from the environment (or `.env` file) at startup.
If running interactively without a `.env`, the server will prompt for them.

## Run

```bash
python server.py
```

The console prints your local network URL, e.g.:

```
  Local:   http://127.0.0.1:8000
  Network: http://192.168.1.23:8000
```

Open the **Network** URL from any browser on the same Wi-Fi.

> **Termux note:** run `termux-wake-lock` first to keep it alive with the screen off.

> **Geo-restriction note:** if `api.binance.com` is blocked in your region, the bot
> automatically falls back to `data-api.binance.vision`. Futures stats are skipped
> gracefully when unavailable.

## AI Market Intelligence Pipeline

`ai_analyst.py` runs a multi-stage pipeline that only lets a trade idea through
if every stage agrees it's worth publishing:

```
Stage 1 — Market data       (engine.py — all 13 strategies)
Stage 2 — Memory context    (signal_memory.py — past similar setups)
Stage 3 — AI analyst        (Groq LLM — forms thesis, produces JSON signal)
Stage 4 — Trade quality     (trade_quality.py — grades setup A+/A/B/Reject)
Stage 5 — Risk gate         (rejects signals graded "Reject" before publish)
Stage 6 — Signal out        (pipeline log + dashboard push)
Stage 7 — Memory write      (signal_memory.py — records result for future context)
```

### Groq model fallback

The bot cycles through these models in order, falling back on rate-limit or error:

1. `$GROQ_MODEL` (if set)
2. `llama-3.3-70b-versatile`
3. `llama-3.1-70b-versatile`
4. `llama-3.1-8b-instant`
5. `mixtral-8x7b-32768`

### Active-signal lock

Once a LONG or SHORT fires, AI analysis is paused for that symbol until price
hits the stop loss or TP1. This prevents the AI from second-guessing an open
trade and avoids duplicate signals.

## Configuration options (`config.py`)

| Setting | Default | Description |
| --- | --- | --- |
| `SYMBOLS` | BTCUSDT … DOGEUSDT | Coins in the dashboard dropdown |
| `INTERVALS` | 1m … 1d | Timeframes in the dropdown |
| `WEIGHTS` | see file | Per-strategy weight in the composite score |
| `SIGNAL_THRESHOLD` | 20 | Minimum score (0–100) to fire a LONG/SHORT |
| `STRONG_THRESHOLD` | 45 | Score above which a signal is labelled STRONG |
| `REFRESH_SECONDS` | 20 | Background engine refresh interval |
| `AI_REFRESH_SECONDS` | 60 | AI analyst refresh interval |
| `PORT` | 8000 | Web server port |

Signal history persists to `signals.json`; AI signal memory persists to
`signal_history.db` (SQLite).

## REST API

| Endpoint | Method | Description |
| --- | --- | --- |
| `/api/config` | GET | Server configuration + defaults |
| `/api/state` | GET | Latest engine analysis (`?symbol=&interval=`) |
| `/api/signals` | GET | Recent confluence signals (last 50) |
| `/api/ai` | GET | Latest AI signal (`?symbol=`) |
| `/api/ai-signals` | GET | Recent AI signal table |
| `/api/engine-status` | GET | AI engine health + latency |
| `/api/pipeline-events` | GET | Pipeline stage log |
| `/api/signal-status` | GET | Active-signal lock state |
| `/api/pending-limits` | GET | Pending LIMIT orders (`?symbol=`) |
| `/api/symbol` | GET/POST | Get/set active symbol |
| `/api/scanner` | GET | Coin scanner results |
| `/api/scanner/trigger` | POST | Trigger manual rescan |
| `/ws` | WS | Live WebSocket feed |

## Disclaimer

Educational tool — not financial advice. Signals are algorithmic confluence
scores, not guarantees. Trade at your own risk.
