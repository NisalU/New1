# AI Trading Signal Bot (Termux + Web Dashboard)

A pure-Python crypto trading signal bot that analyzes Binance market data with
10 strategies and serves a live charting dashboard on your phone's local IP.

Built on **aiohttp + WebSocket** for realtime updates: live trade ticks move
the last candle and the price with smooth animation, klines stream in as they
form, and analysis snapshots / signals push instantly to every connected
browser. No numpy / pandas — only `aiohttp`, `websockets` and
`requests`, so it installs cleanly on Termux.

## Strategies (confluence scored)

| Strategy | What it detects |
| --- | --- |
| EMA 7/25/99 | Trend stack alignment, fresh 7/25 crosses |
| Support / Resistance | Clustered swing levels, bounces, breakouts |
| Trendlines | Regression-fit trendlines, bounces and breaks |
| Chart Patterns | Engulfing, pin bars, double top/bottom, head & shoulders |
| Fibonacci | Retracement of dominant swing (0.5 / 0.618 golden zone) |
| Smart Money Concepts | BOS / CHoCH, order blocks, fair value gaps |
| Liquidity Sweeps | Stop hunts through equal highs/lows that reverse |
| Orderflow / CVD | Delta pressure, CVD divergence, absorption |
| Auction Market | Volume profile POC, value area acceptance/rejection |
| Fundamentals | Funding rate, open interest, long/short ratio |

Each strategy votes -1..+1 and is weighted (see `config.py`). A LONG/SHORT
signal fires when the composite score crosses the threshold (default 45/100),
together with an ATR-based trade plan (entry / stop / TP1 / TP2).

## Install on Termux

```bash
pkg update && pkg install python git
git clone <your-repo-url> signal-bot && cd signal-bot
pip install -r requirements.txt
python server.py
```

The console prints your local network URL, e.g.:

```
  Local:   http://127.0.0.1:8000
  Network: http://192.168.1.23:8000
```

Open the Network URL from any browser on the same Wi-Fi (or the Local URL on
the phone itself). To keep it running with the screen off, run
`termux-wake-lock` first.

## Configuration

Edit `config.py`:

- `SYMBOLS`, `INTERVALS` — markets offered in the dashboard dropdowns
- `WEIGHTS` — importance of each strategy in the composite score
- `SIGNAL_THRESHOLD` — how much confluence is needed to fire a signal
- `REFRESH_SECONDS` — background analysis interval
- `PORT` — web server port

Signal history persists to `signals.json`.

> Note: if `api.binance.com` is geo-restricted where you are, the bot
> automatically falls back to `data-api.binance.vision` for market data.
> Futures fundamentals (funding/OI) are skipped gracefully when unavailable.

## AI market intelligence pipeline

`ai_analyst.py` is not a single model call — it's a pipeline that only lets a
trade idea through if every stage agrees it's worth publishing:

```
Market data (engine.py, all 10 strategies)
    -> Signal memory context (signal_memory.py)      -- past similar setups
    -> Primary AI analyst (Groq → OpenRouter fallback) -- forms the thesis
    -> Server-side risk gate                          -- re-checks the math
    -> AI critic (second opinion)                    -- tries to kill it
    -> Signal memory write
```

### AI provider: Groq + OpenRouter fallback

The bot uses **two AI providers** with automatic failover:

| Priority | Provider | When used |
| --- | --- | --- |
| 1 | **Groq** (`GROQ_API_KEY`) | Default — fast, high-quality models |
| 2 | **OpenRouter free** (`OPENROUTER_API_KEY`) | Automatic fallback on Groq HTTP 429 rate-limit |

When Groq returns a rate-limit error, the bot instantly switches to OpenRouter
free models and stays there for 5 minutes (`GROQ_RATE_LIMIT_COOLDOWN` in
`config.py`) before retrying Groq. No manual intervention needed.

OpenRouter free models are smaller and less capable than Groq's 70B models, so
the bot uses **`SYSTEM_PROMPT_ENHANCED`** for them — a stricter, more directive
prompt that:
- Puts the JSON schema **first** (prevents garbled output)
- Makes the **WAIT bias even stronger** with an explicit 7-condition checklist
- Bans hallucinating price levels not present in the input data
- Adds a concise trade checklist the model runs before calling LONG/SHORT
- Uses shorter, simpler sentences to reduce misinterpretation

In practice this means free models are held to the same standard as Groq — they
just need more hand-holding to get there.

### Setup

Create a `.env` file next to `server.py`:

```env
# Required for AI analysis (at least one of these)
GROQ_API_KEY=gsk_...          # https://console.groq.com/keys (free tier available)
OPENROUTER_API_KEY=sk-or-...  # https://openrouter.ai/keys   (free models available)

# Optional overrides
GROQ_MODEL=llama-3.3-70b-versatile          # override default Groq model
OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct:free  # pin a specific free model

# Optional Binance credentials (for private endpoints / higher rate limits)
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

Without any AI key the dashboard falls back to raw engine signals.

### Pipeline stages

**1. Signal memory** (`signal_memory.py`) — a local SQLite log
(`signal_history.db`, gitignored) of every published call: symbol,
timestamp, setup type, entry/stop/target, market condition, quality grade
and the AI's own reasoning. The last few setups on the same symbol are fed
back into the AI's context window and turned into an explicit risk warning
if recent similar trades lost.

**2. Primary AI analyst** — receives the full 1h confluence read, an
explicit liquidity/structure summary (sweeps, resting liquidity pools,
BOS/CHoCH events, CVD divergence), a 4h higher-timeframe summary, the regime
classification, the structural quality grade, recent similar setups and any
risk warnings. Its default answer is **WAIT**; it only calls LONG/SHORT with
a thesis, a clean location, a concrete confirmation, a logical invalidation,
and a reward/risk of at least 1.8.

**3. Server-side risk gate** — never trusts the model's self-reported
numbers. It re-derives risk/reward and entry-to-price distance from the
actual entry/stop/tp1. Any arithmetic failure (missing levels, R:R below
minimum, entry more than 2.5 ATR from price) sets `gated: true` and forces
`signal: "WAIT"`.

**4. AI critic** (`config.AI_CRITIC_ENABLED`, default on) — a second,
independent AI call that is instructed to be skeptical by default and try
to kill the trade: is the entry late, is there liquidity/structure against
it before target, is the reward/risk realistic, could this be a trap, does
the higher timeframe disagree. If it doesn't approve, the call is forced to
WAIT with the critic's critique attached (`result.critic`).

### Tunables (`config.py`)

- `AI_INTERVAL` / `AI_HTF_INTERVAL` — primary chart + higher-timeframe context
- `AI_REFRESH_SECONDS` — poll cadence per active symbol
- `GROQ_RATE_LIMIT_COOLDOWN` — seconds to stay on OpenRouter after a Groq 429 (default 300)
- `REGIME_COMPRESSION_TIGHT` / `REGIME_VOLATILITY_SPIKE` — regime filter thresholds
- `AI_MIN_RISK_REWARD` / `AI_MAX_ENTRY_ATR_DISTANCE` — risk gate thresholds
- `AI_CRITIC_ENABLED` — toggle the second-pass critic review
- `SIGNAL_MEMORY_LOOKBACK` — how many past setups are shown to the AI

### Output

`GET /api/ai?symbol=BTCUSDT` returns the latest cached call; the dashboard
also receives live `{"type":"ai", ...}` pushes over the websocket. Result
shape:

```json
{
  "signal": "LONG | SHORT | WAIT",
  "direction": "LONG | SHORT | null",
  "setup_type": "e.g. liquidity sweep + reclaim",
  "confidence": 0,
  "entry": null, "stop": null, "tp1": null, "tp2": null,
  "risk_reward": null,
  "market_regime": "trending_bullish | range | ...",
  "htf_bias": "LONG | SHORT | NEUTRAL",
  "liquidity_context": { "sweeps": [], "liquidity_pools": [], "...": "..." },
  "orderflow_read": "...",
  "reasoning": "...",
  "invalidation": "...",
  "trade_quality": { "grade": "A+ | A | B | Reject", "...": "..." },
  "gated": false,
  "gate_reason": null,
  "critic": { "approve": true, "concerns": [], "critique": "..." }
}
```

## Disclaimer

Educational tool — not financial advice. Signals are algorithmic confluence
scores and discretionary AI reads, not guarantees.
