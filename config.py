"""Configuration for the AI Trading Signal Bot."""
import os

# ---- Server ----
HOST = "0.0.0.0"
PORT = 8000

# ---- Market data ----
DEFAULT_SYMBOL = "BTCUSDT"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
DEFAULT_INTERVAL = "15m"
INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"]
KLINE_LIMIT = 300

SPOT_ENDPOINTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://data-api.binance.vision",
]
FUTURES_ENDPOINTS = [
    "https://fapi.binance.com",
]

REFRESH_SECONDS = 20

# ---- Binance API credentials (optional) ----
BINANCE_API_KEY = ""
BINANCE_API_SECRET = ""

# ---- Confluence engine weights ----
# Original strategies
WEIGHTS = {
    "ema_trend":        10,
    "support_resistance": 10,
    "trendlines":        7,
    "patterns":          7,
    "fibonacci":         7,
    "smc":              14,   # Enhanced: breaker blocks, mitigation, displacement FVGs
    "liquidity_sweep":  12,   # Enhanced: inducement sweeps, liquidity voids
    "orderflow_cvd":    13,   # Enhanced: delta divergence, hidden accumulation/distribution
    "auction_market":    7,
    "fundamentals":      6,
    # New institutional strategies
    "vwap":             10,   # Session VWAP + anchored VWAP
    "kill_zones":        6,   # Institutional session timing
    "wyckoff":          11,   # Wyckoff phase classification
}

SIGNAL_THRESHOLD = 20
STRONG_THRESHOLD = 45
MAX_SIGNAL_HISTORY = 200
ENGINE_SIGNAL_FEED = False

# ---- AI analyst ----
AI_INTERVAL = "1h"
AI_HTF_INTERVAL = "4h"
AI_REFRESH_SECONDS = 60
AI_MIN_CALL_INTERVAL = 2.1

# Server-side risk gate
AI_MIN_RISK_REWARD = 1.2
AI_MAX_ENTRY_ATR_DISTANCE = 2.5

MODEL_RL_COOLDOWN = 60

# ---- AI critic — DISABLED ----
AI_CRITIC_ENABLED = False

# ---- Signal memory ----
SIGNAL_MEMORY_LOOKBACK = 3

# ---- Pipeline log ----
PIPELINE_LOG_MAX = 100

# ---- Active-signal lock ----
ACTIVE_SIGNAL_LOCK = True

# ---- Market regime ----
REGIME_COMPRESSION_TIGHT = 0.45
REGIME_VOLATILITY_SPIKE = 1.8

# Token budgets
AI_MAX_TOKENS = 2500
AI_MAX_TOKENS_RETRY = 3500
AI_JSON_FAIL_COOLDOWN = 30

# Prompt sizing
AI_PROMPT_CANDLES = 8
AI_PROMPT_CVD_POINTS = 15
AI_PROMPT_MEMORY_ROWS = 3

# ---- Limit signals ----
LIMIT_SIGNALS_ENABLED = True

# ---- Coin Scanner ----
CMC_API_KEY            = os.environ.get("CMC_API_KEY", "")   # optional
SCANNER_AUTO_SCAN      = True        # run automatically on startup + every N hours
SCANNER_INTERVAL_HOURS = 2           # hours between automatic re-scans
SCANNER_MIN_VOL_M      = 50          # minimum 24h volume in millions USD
SCANNER_BLACKLIST      = ["BTCUSDT", "ETHUSDT"]   # always-excluded symbols
SCANNER_SHOW_TOP       = 10          # rows shown in the dashboard table
SCANNER_TREND_PENALTY  = True        # discount strongly trending coins
SCANNER_TREND_HOURS    = 4           # look-back window for trend detection
SCANNER_TREND_PCT      = 3.0         # |move| threshold that triggers penalty
SCANNER_TREND_MULT     = 0.5         # multiplier applied when trend is detected
CMC_MIN_MARKET_CAP_M   = 100         # minimum market cap in millions USD
CMC_QUALITY_MIDCAP_M   = 10_000      # mid-cap threshold in millions USD
