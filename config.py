"""Configuration for the AI Trading Signal Bot."""
import os

# ---- Server ----
HOST = "0.0.0.0"   # listen on all interfaces so other devices on LAN can open the dashboard
PORT = 8000

# ---- Market data ----
DEFAULT_SYMBOL = "BTCUSDT"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
DEFAULT_INTERVAL = "15m"
INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"]
KLINE_LIMIT = 300           # candles fetched per analysis

# Binance REST endpoints, tried in order (first that works is cached).
SPOT_ENDPOINTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://data-api.binance.vision",
]
FUTURES_ENDPOINTS = [
    "https://fapi.binance.com",
]

REFRESH_SECONDS = 20        # background analysis loop interval

# ---- Binance API credentials (optional) ----
BINANCE_API_KEY = ""
BINANCE_API_SECRET = ""

# ---- Confluence engine ----
WEIGHTS = {
    "ema_trend": 12,
    "support_resistance": 12,
    "trendlines": 8,
    "patterns": 8,
    "fibonacci": 8,
    "smc": 14,
    "liquidity_sweep": 10,
    "orderflow_cvd": 14,
    "auction_market": 8,
    "fundamentals": 6,
}

SIGNAL_THRESHOLD = 20
STRONG_THRESHOLD = 45
MAX_SIGNAL_HISTORY = 200
ENGINE_SIGNAL_FEED = False

# ---- AI analyst ----
AI_INTERVAL = "1h"
AI_HTF_INTERVAL = "4h"
AI_REFRESH_SECONDS = 60         # re-analyze every 60 seconds
AI_MIN_CALL_INTERVAL = 2.1      # min gap between Groq HTTP calls (rate-limit guard)

# Server-side risk gate
AI_MIN_RISK_REWARD = 1.2
AI_MAX_ENTRY_ATR_DISTANCE = 2.5

# Model rate-limit cooldown
MODEL_RL_COOLDOWN = 60          # shorter cooldown so we can cycle faster

# ---- AI critic — DISABLED ----
AI_CRITIC_ENABLED = False

# ---- Signal memory ----
SIGNAL_MEMORY_LOOKBACK = 3

# ---- Pipeline log ----
PIPELINE_LOG_MAX = 100

# ---- Active-signal lock ----
# When a LONG/SHORT signal fires, AI analysis for that symbol is paused
# until the signal is stopped out (price crosses stop) or target is hit (price >= tp1).
ACTIVE_SIGNAL_LOCK = True

# ---- Market regime ----
REGIME_COMPRESSION_TIGHT = 0.45
REGIME_VOLATILITY_SPIKE = 1.8

# Token budgets
AI_MAX_TOKENS = 2000
AI_MAX_TOKENS_RETRY = 3000
AI_JSON_FAIL_COOLDOWN = 30

# Prompt sizing
AI_PROMPT_CANDLES = 6
AI_PROMPT_CVD_POINTS = 12
AI_PROMPT_MEMORY_ROWS = 3

# ---- Limit signals ----
LIMIT_SIGNALS_ENABLED = True   # track LIMIT order signals and alert when price hits them
