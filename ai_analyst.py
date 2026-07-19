"""AI analyst — discretionary structure/liquidity read on top of the
confluence engine.

Pipeline:
    Market data (engine.py)
        -> Signal memory context (signal_memory.py)
        -> Primary AI analyst (OpenRouter — tencent/hunyuan-a13b-instruct:free)
        -> Server-side risk gate (_apply_risk_gate)
        -> Signal memory write

Active-signal lock:
    Once a LONG or SHORT fires, AI analysis for that symbol is skipped
    until price crosses the stop (signal lost) or reaches tp1 (target hit).

Daily budget:
    OpenRouter free tier allows 50 requests/day.  The module-level counters
    _or_daily_count / _or_daily_date enforce this limit and automatically
    reset at UTC midnight.
"""
import json
import logging
import os
import threading
import time
import traceback
from collections import deque

import requests

import config
import market_regime
import signal_memory
import trade_quality
from engine import engine
from footprint import footprint as fp_agg
from strategies.helpers import atr

log = logging.getLogger("ai_analyst")

# ---------------------------------------------------------------------------
# Provider — OpenRouter (OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------
OR_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

# Free-tier model with 256k context window.
# Override via OPENROUTER_MODEL env var if needed.
# Ordered fallback list — first non-rate-limited model wins.
# Override primary via OPENROUTER_MODEL env var.
_OR_MODEL_PRIMARY = os.environ.get("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")
OR_MODELS: list[str] = [
  _OR_MODEL_PRIMARY,                              # google/gemma-4-31b-it:free (default)
  "meta-llama/llama-3.3-70b-instruct:free",      # Meta Llama 3.3 70B
  "nvidia/nemotron-3-ultra-550b-a55b:free",       # NVIDIA Nemotron 550B — best reasoning
  "nousresearch/hermes-3-llama-3.1-405b:free",   # Hermes 3 405B — strong JSON output
  "qwen/qwen3-coder:free",                        # Qwen3 Coder 480B — structured output
  "openai/gpt-oss-20b:free",                     # OpenAI OSS 20B
  "nvidia/nemotron-3-super-120b-a12b:free",       # NVIDIA Nemotron 120B
  "nvidia/nemotron-3-nano-30b-a3b:free",          # NVIDIA Nemotron Nano 30B
  "tencent/hy3:free",                             # Tencent Hy3 — 262k ctx
  "openrouter/free",                              # OpenRouter auto-router — picks any free model with capacity
]

# ---------------------------------------------------------------------------
# Daily request budget (OpenRouter free tier: 50 requests / UTC day)
# ---------------------------------------------------------------------------
_OR_DAILY_LIMIT: int = 50
_or_daily_count: int = 0
_or_daily_date:  str = ""   # "YYYY-MM-DD" UTC


def _or_check_budget() -> tuple[bool, float]:
    """Return (budget_ok, seconds_until_reset).

    Increments the counter if budget remains.  Call BEFORE making a request.
    """
    global _or_daily_count, _or_daily_date
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _or_daily_date != today:
        _or_daily_date  = today
        _or_daily_count = 0
    now = time.gmtime()
    secs_until_reset = float(86400 - (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec))
    if _or_daily_count >= _OR_DAILY_LIMIT:
        return False, secs_until_reset
    _or_daily_count += 1
    remaining = _OR_DAILY_LIMIT - _or_daily_count
    log.info("[openrouter] Daily request %d/%d  (%d remaining)",
             _or_daily_count, _OR_DAILY_LIMIT, remaining)
    if remaining <= 5:
        log.warning("[openrouter] Only %d daily requests left!", remaining)
    return True, secs_until_reset

PROMPT_CANDLE_COUNT  = getattr(config, "AI_PROMPT_CANDLES",     25)   # was 50
PROMPT_HTF_CANDLES   = getattr(config, "AI_PROMPT_HTF_CANDLES",  8)   # was 10
PROMPT_CVD_POINTS    = getattr(config, "AI_PROMPT_CVD_POINTS",  15)   # was 30
PROMPT_MEMORY_ROWS   = getattr(config, "AI_PROMPT_MEMORY_ROWS",  3)   # was 5

# ---------------------------------------------------------------------------
# System prompt  (optimised for 256k-context model — detailed chain-of-thought)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an elite institutional crypto trader. Top-down analysis, one precise trade call, JSON only — no text before or after.

STEP 1 MACRO [market_regime, engine_composite_score, structural_quality]
Engine >60=bullish,<-60=bearish. Ranging/volatile=-15 confidence. structural_quality<0.3=WAIT.

STEP 2 4H STRUCTURE [higher_timeframe]
HH+HL=bullish trend, LH+LL=bearish. Find last BOS/CHoCH. 4H OBs and FVGs = highest-priority zones. Above 50% of last swing=premium(look short), below=discount(look long). THIS IS YOUR DIRECTIONAL BIAS — never counter-trade 4H without confirmed 1H CHoCH.

STEP 3 1H INTERNAL [recent_candles]
iBOS=short-term push confirmed. CHoCH=reversal confirmed. Displacement candle(>1.5x avg body)=institutional. 1H OB before displacement = primary entry on first touch. Equal highs/lows = resting liquidity pools.

STEP 4 LIQUIDITY SWEEP [liquidity]
Confirmed sweep = wick through pool + candle CLOSES back inside. Entry after close, never on wick. Stop = beyond wick extreme + 0.1ATR. Recency: <=3 candles=highest, 4-8=moderate, >8=lower. Both sides equal=WAIT.

STEP 5 CVD & FOOTPRINT [cvd_last_n, footprint]
Rising CVD+falling price=hidden accumulation(LONG). Falling CVD+rising price=hidden distribution(SHORT). CVD confirms but never overrides 4H bias.
Footprint: POC=intracandle magnet. Bullish candle+negative delta=absorption/hidden sell. Imbalance>=3:1=institutional zone; 3+ consecutive=stacked(+10 confidence). HVN=targets at. LVN=never place SL inside. Unfinished auction=price returns.

STEP 6 WYCKOFF [wyckoff]
Spring(sweep support+close inside)=LONG. UTAD(sweep resistance+close inside)=SHORT. Ranging without Spring/UTAD=WAIT.

STEP 7 ICT POWER OF 3 [kill_zones, recent_candles]
Asia=accumulate, London=manipulate(false break), NY=real move(opposite manipulation). OTE=62-79% Fib of last impulse, valid only in discount(longs)/premium(shorts).

STEP 8 ENTRY PRIORITY [key_levels]
1)4H+1H OB overlap 2)Confirmed sweep+displacement 3)Breaker block 4)4H+1H FVG overlap 5)OTE zone 6)VWAP+1H OB 7)POC+S/R 8)Equilibrium(lowest)

STEP 9 VWAP [vwap]
Above=bullish bias. Reclaim=LONG. Loss=SHORT. +-2sigma fade only with sweep+delta divergence.

STEP 10 SESSION [kill_zones]
London 07-10 UTC / NY 12-15 UTC = best. Outside high-quality session=-15 confidence. Dead zone+no Spring/UTAD/sweep=WAIT.

STEP 11 FUNDAMENTALS [futures_fundamentals]
Funding>+0.05%=-15 LONG. Funding<-0.05%=-15 SHORT. OI increasing+aligned=+5. Long/short>3.0=-10 LONG; <0.5=-10 SHORT.

STEP 12 MEMORY [recent_similar_setups]
3+ consecutive losses=-20, require extra confluence. 2 losses=-10, widen stop 0.3ATR. 2+ consecutive wins same setup=+8.

CONFLUENCE (min 4 required):
[1]4H bias aligned [2]1H BOS/CHoCH confirmed [3]Sweep confirmed [4]CVD/delta no divergence [5]High-priority entry zone [6]VWAP aligned [7]Active session [8]Wyckoff Spring/UTAD [9]Fundamentals not opposing [10]Engine score agrees(>30 LONG,<-30 SHORT)
<4=WAIT | 4=LIMIT only max confidence 74 | 5=LIMIT/MARKET max 84 | 6+=full conviction max 100

CONFIDENCE: start 60. 95-100=Spring/UTAD+sweep+CVD+prime session+OB+R:R>=3. 85-94=6+factors. 75-84=5 factors. 65-74=4 factors.

ABSOLUTE WAIT: climax candle>3x avg range | last 5 candles range<0.4ATR | SL>2ATR | R:R<1.5 | 4H opposed+no 1H CHoCH | extreme funding(>+-0.15%)+no sweep | dead session+no Spring/UTAD/sweep.

STOP: min 0.5ATR from entry. Never inside LVN. Never beyond 2ATR.

SETUP TYPES: sweep_reversal|spring|utad|ob_bounce|breaker_rejection|fvg_fill|vwap_reclaim|wyckoff_lps|wyckoff_lpsy|delta_divergence|bos_continuation|choch_reversal|ote_entry

Output ONLY this JSON (no markdown, no extra text):
{"decision":"LONG|SHORT|WAIT","confidence":<int 65-100>,"order_type":"MARKET|LIMIT|NONE","setup_type":"<type>","entry":<number|null>,"stop_loss":<number|null>,"take_profit":[<tp1>,<tp2>],"reason":"<one sentence: trigger + 4H bias + R:R>"}
WAIT: {"decision":"WAIT","confidence":0,"order_type":"NONE","setup_type":"none","entry":null,"stop_loss":null,"take_profit":[],"reason":"<why no trade>"}"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_key():
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def _fnum(x, digits=6):
    return round(float(x), digits)


def _htf_summary(symbol):
    try:
        htf = engine.get_state(symbol, config.AI_HTF_INTERVAL)
    except Exception:
        return None
    ov = htf.get("overlays", {})
    structure = ov.get("structure") or {}
    top_reasons = sorted(htf["breakdown"], key=lambda b: -abs(b["contribution"]))[:5]

    # Last N 4H candles — gives AI direct HTF price structure to read
    htf_candles = htf.get("candles") or []
    htf_recent = [
        {
            "t": c["time"], "o": _fnum(c["open"]), "h": _fnum(c["high"]),
            "l": _fnum(c["low"]), "c": _fnum(c["close"]),
            "vol": _fnum(c["volume"], 2), "delta": _fnum(c["delta"], 2),
        }
        for c in htf_candles[-PROMPT_HTF_CANDLES:]
    ]

    # HTF key levels (OBs, FVGs, S/R)
    htf_levels = {}
    if ov.get("order_blocks"):
        htf_levels["order_blocks"] = [
            {"type": ob["type"], "top": _fnum(ob["top"]), "bottom": _fnum(ob["bottom"])}
            for ob in ov["order_blocks"][:4]
        ]
    fvg_list = ov.get("fvg") or ov.get("fvgs") or []
    if fvg_list:
        htf_levels["fvg"] = [
            {"type": f["type"], "top": _fnum(f["top"]), "bottom": _fnum(f["bottom"])}
            for f in fvg_list[:4]
        ]
    if ov.get("support"):
        htf_levels["support"] = [_fnum(lv["price"]) for lv in ov["support"][:3]]
    if ov.get("resistance"):
        htf_levels["resistance"] = [_fnum(lv["price"]) for lv in ov["resistance"][:3]]

    return {
        "interval":        config.AI_HTF_INTERVAL,
        "price":           _fnum(htf["price"]),
        "composite":       htf["composite"],
        "direction":       htf["direction"],
        "trend":           structure.get("trend"),
        "structure_events": structure.get("events"),
        "top_reasons":     [r for b in top_reasons for r in b["reasons"][:1] if r],
        "candles":         htf_recent,
        "key_levels":      htf_levels or None,
    }


def _liquidity_context(ov):
    structure = ov.get("structure") or {}
    return {
        "sweeps": ov.get("sweeps") or [],
        "liquidity_pools": ov.get("liquidity_pools") or [],
        "structure_trend": structure.get("trend"),
        "structure_events": structure.get("events") or [],
        "orderflow_divergence": ov.get("divergence"),
    }


def _risk_warnings(analysis, regime, memory_rows):
    warnings = []
    ov = analysis.get("overlays", {})
    fundamentals = ov.get("fundamentals")
    if not regime.get("tradeable", True):
        warnings.append(
            f"Note: regime classifier flagged this market as {regime['regime']} — "
            f"factor this into your discretionary read."
        )
    if fundamentals:
        fr = fundamentals.get("funding_rate", 0)
        if abs(fr) > 0.0005:
            side = "longs" if fr > 0 else "shorts"
            warnings.append(f"Funding is stretched ({fr*100:.4f}%) — {side} are crowded")
        ls = fundamentals.get("long_short_ratio", 1.0)
        if ls > 3 or ls < 0.5:
            warnings.append(f"Long/short account ratio is extreme ({ls:.2f}) — contrarian risk")
    if memory_rows:
        losses = [r for r in memory_rows if r.get("result") == "loss"]
        if len(losses) >= 2:
            warnings.append(
                f"{len(losses)} of the last {len(memory_rows)} similar setups on this symbol "
                f"lost — treat this direction with extra scrutiny"
            )
    return warnings


def _compact_market(analysis, symbol, regime, structural_quality, memory_rows):
    candles = analysis["candles"]
    ov = analysis.get("overlays", {})
    a = atr(candles) or analysis["price"] * 0.005

    recent = [
        {
            "t": c["time"], "o": _fnum(c["open"]), "h": _fnum(c["high"]),
            "l": _fnum(c["low"]), "c": _fnum(c["close"]),
            "vol": _fnum(c["volume"], 2), "delta": _fnum(c["delta"], 2),
        }
        for c in candles[-PROMPT_CANDLE_COUNT:]
    ]

    cvd = ov.get("cvd") or []
    cvd_tail = [_fnum(p["value"], 2) for p in cvd[-PROMPT_CVD_POINTS:]]

    strategies = [
        {
            "name": b["label"], "weight": b["weight"], "score": b["score"],
            "contribution": b["contribution"], "reasons": b["reasons"][:3],
        }
        for b in analysis["breakdown"]
    ]

    # ── Key structural levels ──────────────────────────────────────────────
    levels = {}
    if ov.get("support"):
        levels["support"] = [_fnum(lv["price"]) for lv in ov["support"][:4]]
    if ov.get("resistance"):
        levels["resistance"] = [_fnum(lv["price"]) for lv in ov["resistance"][:4]]
    if ov.get("volume_profile"):
        vp = ov["volume_profile"]
        levels["poc"] = _fnum(vp["poc"])
        levels["vah"] = _fnum(vp["vah"])
        levels["val"] = _fnum(vp["val"])
    if ov.get("order_blocks"):
        levels["order_blocks"] = [
            {"type": ob["type"], "top": _fnum(ob["top"]), "bottom": _fnum(ob["bottom"])}
            for ob in ov["order_blocks"][:4]
        ]
    # breaker blocks
    if ov.get("breaker_blocks"):
        levels["breaker_blocks"] = [
            {"type": bb["type"], "top": _fnum(bb["top"]), "bottom": _fnum(bb["bottom"])}
            for bb in ov["breaker_blocks"][:3]
        ]
    # FVGs — support both old "fvgs" key and new "fvg" key
    fvg_list = ov.get("fvg") or ov.get("fvgs") or []
    if fvg_list:
        levels["fvg"] = [
            {"type": f["type"], "top": _fnum(f["top"]), "bottom": _fnum(f["bottom"]),
             "mid": _fnum(f["mid"]), "displacement": f.get("displacement", False)}
            for f in fvg_list[:4]
        ]
    # Premium/discount zone
    if ov.get("premium_discount"):
        levels["premium_discount"] = ov["premium_discount"]

    # ── VWAP / AVWAP ──────────────────────────────────────────────────────
    vwap_data = ov.get("vwap")
    avwap_low = ov.get("avwap_low")
    avwap_high = ov.get("avwap_high")
    vwap_context = {}
    if vwap_data:
        vwap_context["session_vwap"] = {k: _fnum(v) for k, v in vwap_data.items()}
    if avwap_low:
        vwap_context["avwap_from_swing_low"] = _fnum(avwap_low)
    if avwap_high:
        vwap_context["avwap_from_swing_high"] = _fnum(avwap_high)

    # ── Kill zones ─────────────────────────────────────────────────────────
    kill_zone_ctx = ov.get("kill_zones")

    # ── Wyckoff ───────────────────────────────────────────────────────────
    wyckoff_ctx = ov.get("wyckoff")

    # ── Liquidity (enhanced) ───────────────────────────────────────────────
    def _liquidity_context_enhanced(o):
        structure = o.get("structure") or {}
        return {
            "sweeps":              (o.get("sweeps") or [])[:4],
            "inducements":         (o.get("inducements") or [])[:3],
            "liquidity_pools":     (o.get("liquidity_pools") or [])[:4],
            "liquidity_voids":     (o.get("voids") or [])[:3],
            "structure_trend":     structure.get("trend"),
            "structure_events":    (structure.get("events") or [])[:4],
            "orderflow_divergence": o.get("divergence"),
            "macro_orderflow":     o.get("macro_flow"),
        }

    fundamentals = ov.get("fundamentals")


    # ── Footprint chart ───────────────────────────────────────────────────
    fp_candles = fp_agg.get_summary(symbol, n=5)
    fp_partial = fp_agg.get_partial(symbol)
    footprint_ctx: dict = {"candles": fp_candles}
    if fp_partial:
        footprint_ctx["forming"] = fp_partial
    return {
        "symbol":                analysis["symbol"],
        "chart":                 config.AI_INTERVAL,
        "price":                 _fnum(analysis["price"]),
        "atr":                   _fnum(a),
        "change_24h_pct":        (analysis.get("ticker") or {}).get("change_pct"),
        "engine_composite_score": analysis["composite"],
        "engine_direction":      analysis["direction"],
        "market_regime":         regime,
        "structural_quality":    structural_quality,
        "higher_timeframe":      _htf_summary(symbol),
        "liquidity":             _liquidity_context_enhanced(ov),
        "wyckoff":               wyckoff_ctx,
        "vwap":                  vwap_context or None,
        "kill_zones":            kill_zone_ctx,
        "strategies":            strategies,
        "cvd_last_n":            cvd_tail,
        "key_levels":            levels,
        "futures_fundamentals":  fundamentals,
        "recent_similar_setups": memory_rows[:PROMPT_MEMORY_ROWS],
        "risk_warnings":         _risk_warnings(analysis, regime, memory_rows),
        "recent_candles":        recent,
        "footprint":             footprint_ctx,
    }


def _fmt_setup_type(raw):
    if not raw or raw == "none":
        return "—"
    words = str(raw).replace("_", " ").replace("+", " + ").split()
    label = " ".join(w.capitalize() for w in words)
    return label[:30]


def _repair_truncated_json(text):
    """Best-effort repair of a truncated JSON object.

    Fixes two common LLM truncation patterns:
      1. String left open (missing closing quote)
      2. Brackets/braces left open (missing closing tokens)

    The stack now tracks the *type* of each opener so mismatched brackets
    are closed correctly, e.g. {"a": [1, 2 → {"a": [1, 2]} not {"a": [1, 2}.
    """
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    s = text[start:]
    in_string = False
    escape = False
    stack: list[str] = []   # stores opening chars: '{' or '['

    _OPENERS = frozenset("{[")
    _CLOSERS = {"}": "{", "]": "["}
    _CLOSE_FOR = {"{": "}", "[": "]"}

    for ch in s:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in _OPENERS:
            stack.append(ch)
        elif ch in _CLOSERS:
            # Only pop when the closing bracket matches the most recent opener
            if stack and stack[-1] == _CLOSERS[ch]:
                stack.pop()

    if in_string:
        s += '"'
    s = s.rstrip().rstrip(",")
    if s.endswith(":"):
        s += "null"
    # Close all unclosed brackets in reverse order
    while stack:
        s += _CLOSE_FOR[stack.pop()]
    return s


def _try_parse_json(content):
    try:
        return json.loads(content), None
    except (ValueError, TypeError):
        pass
    repaired = _repair_truncated_json(content)
    if repaired is None:
        return None, None
    try:
        return json.loads(repaired), repaired
    except (ValueError, TypeError):
        return None, None


# ---------------------------------------------------------------------------
# Main analyst class
# ---------------------------------------------------------------------------

class AIAnalyst:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}
        self._or_model = None
        self.enabled = bool(_get_or_key())
        self.last_error = None

        # Engine status metrics
        self._last_latency = None
        self._inference_count = 0
        self._inference_window = deque(maxlen=120)
        self._active_models = set()

        # Per-model rate-limit tracking
        self._model_rl_until: dict = {}
        self._MODEL_RL_SECONDS = getattr(config, "MODEL_RL_COOLDOWN", 15)  # 429 retry window

        # Global throttle
        self._rate_lock = threading.Lock()
        self._last_call_ts = 0.0
        self._MIN_CALL_INTERVAL = getattr(config, "AI_MIN_CALL_INTERVAL", 2.1)

        # Output is a short JSON line — generous limit keeps retries working
        self._MAX_TOKENS_PRIMARY = getattr(config, "AI_MAX_TOKENS", 1500)   # was 4000
        self._MAX_TOKENS_RETRY   = getattr(config, "AI_MAX_TOKENS_RETRY", 2000)  # was 5000
        self._JSON_FAIL_COOLDOWN = getattr(config, "AI_JSON_FAIL_COOLDOWN", 30)

        # Pipeline event log
        self._pipeline_events: deque = deque(
            maxlen=getattr(config, "PIPELINE_LOG_MAX", 100)
        )
        self._active_run: dict = {}

        # Recent LONG/SHORT signals
        self._recent_ai_signals = []
        self._load_recent_signals_from_db()

        # ── Active-signal lock ──────────────────────────────────────────────
        # symbol -> {direction, entry, stop, tp1, tp2, updated}
        self._active_signals: dict = {}

        # ── Pending limit orders ─────────────────────────────────────────────
        # list of pending limit signal dicts waiting for price to reach entry
        self._pending_limits: list = []

        # Next scheduled analysis timestamps per symbol (for countdown)
        self._next_analysis_ts: dict = {}  # symbol -> epoch seconds

    # -----------------------------------------------------------------------
    # Active-signal lock helpers
    # -----------------------------------------------------------------------

    def _record_active_signal(self, symbol, result):
        """Store an active signal so AI skips analysis while it's running."""
        if result.get("signal") not in ("LONG", "SHORT"):
            return
        with self._lock:
            self._active_signals[symbol] = {
                "direction": result["signal"],
                "entry":     result.get("entry"),
                "stop":      result.get("stop"),
                "tp1":       result.get("tp1"),
                "tp2":       result.get("tp2"),
                "updated":   result.get("updated", int(time.time())),
            }
        log.info("[signal-lock] Active %s signal recorded for %s",
                 result["signal"], symbol)

    def _check_signal_active(self, symbol, current_price):
        """Return (is_active, reason).

        is_active = True  → signal still alive, skip AI.
        is_active = False → stopped out or target hit; signal cleared.
        reason is a human-readable string for the dashboard.
        """
        with self._lock:
            sig = self._active_signals.get(symbol)
        if not sig:
            return False, None

        direction = sig["direction"]
        stop = sig.get("stop")
        tp1  = sig.get("tp1")

        if direction == "LONG":
            if stop is not None and current_price <= stop:
                with self._lock:
                    self._active_signals.pop(symbol, None)
                log.info("[signal-lock] %s LONG stopped out at %.4f", symbol, current_price)
                return False, f"stopped out at {current_price:.4f}"
            if tp1 is not None and current_price >= tp1:
                with self._lock:
                    self._active_signals.pop(symbol, None)
                log.info("[signal-lock] %s LONG target hit at %.4f", symbol, current_price)
                return False, f"target hit at {current_price:.4f}"
        elif direction == "SHORT":
            if stop is not None and current_price >= stop:
                with self._lock:
                    self._active_signals.pop(symbol, None)
                log.info("[signal-lock] %s SHORT stopped out at %.4f", symbol, current_price)
                return False, f"stopped out at {current_price:.4f}"
            if tp1 is not None and current_price <= tp1:
                with self._lock:
                    self._active_signals.pop(symbol, None)
                log.info("[signal-lock] %s SHORT target hit at %.4f", symbol, current_price)
                return False, f"target hit at {current_price:.4f}"

        return True, f"{direction} active since {int(time.time() - sig['updated'])}s ago"

    def get_active_signal(self, symbol):
        """Return active signal dict for `symbol`, or None."""
        with self._lock:
            return dict(self._active_signals.get(symbol) or {}) or None

    # -----------------------------------------------------------------------
    # Pending limit order helpers
    # -----------------------------------------------------------------------

    def add_pending_limit(self, result):
        """Register a LIMIT signal as a pending order to watch for price hits."""
        if result.get("signal") not in ("LONG", "SHORT"):
            return
        entry = result.get("entry")
        if entry is None:
            return
        order = {
            "id":         f"{result['symbol']}:{int(time.time())}",
            "symbol":     result["symbol"],
            "direction":  result["signal"],
            "entry":      entry,
            "stop":       result.get("stop"),
            "tp1":        result.get("tp1"),
            "tp2":        result.get("tp2"),
            "confidence": result.get("confidence", 0),
            "setup_type": result.get("setup_type", "none"),
            "reasoning":  result.get("reasoning", ""),
            "created":    int(time.time()),
            "triggered":  False,
        }
        with self._lock:
            # Replace any existing pending limit for same symbol+direction to avoid stacking dupes
            self._pending_limits = [
                o for o in self._pending_limits
                if not (o["symbol"] == order["symbol"] and o["direction"] == order["direction"])
            ]
            self._pending_limits.append(order)
        log.info("[limit] Pending %s LIMIT added for %s @ %.4f",
                 order["direction"], order["symbol"], entry)

    def get_pending_limits(self, symbol=None):
        """Return list of pending limit orders, optionally filtered by symbol."""
        with self._lock:
            if symbol:
                return [dict(o) for o in self._pending_limits if o["symbol"] == symbol]
            return [dict(o) for o in self._pending_limits]

    def check_and_trigger_limits(self, symbol, price):
        """Check if current price has hit any pending limit orders.
        Returns list of triggered orders (removed from pending)."""
        triggered = []
        with self._lock:
            remaining = []
            for order in self._pending_limits:
                if order["symbol"] != symbol:
                    remaining.append(order)
                    continue
                entry     = order["entry"]
                direction = order["direction"]
                stop      = order.get("stop")
                # Trigger: LONG when price drops to/below entry; SHORT when price rises to/above entry
                hit = (direction == "LONG" and price <= entry) or \
                      (direction == "SHORT" and price >= entry)
                # Expire: price blew through the stop before the limit entry was reached
                expired = False
                if stop and not hit:
                    if direction == "LONG" and price < stop:
                        expired = True
                        log.info("[limit] %s LONG limit expired (price %.4f < stop %.4f)",
                                 symbol, price, stop)
                    elif direction == "SHORT" and price > stop:
                        expired = True
                        log.info("[limit] %s SHORT limit expired (price %.4f > stop %.4f)",
                                 symbol, price, stop)
                if hit:
                    order = dict(order)
                    order["triggered"]     = True
                    order["trigger_price"] = price
                    order["trigger_time"]  = int(time.time())
                    triggered.append(order)
                    log.info("[limit] %s %s LIMIT triggered at %.4f (target entry %.4f)",
                             symbol, direction, price, entry)
                elif not expired:
                    remaining.append(order)
            self._pending_limits = remaining
        return triggered

    def get_next_analysis_ts(self, symbol):
        with self._lock:
            return self._next_analysis_ts.get(symbol)

    def set_next_analysis_ts(self, symbol, ts):
        with self._lock:
            self._next_analysis_ts[symbol] = ts

    # -----------------------------------------------------------------------
    def _load_recent_signals_from_db(self):
        try:
            rows = []
            for sym in config.SYMBOLS:
                rows.extend(signal_memory.recent_similar(sym, limit=4))
            rows.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
            for r in rows[:20]:
                if r.get("direction") in ("LONG", "SHORT"):
                    self._recent_ai_signals.append({
                        "time":       r["timestamp"],
                        "symbol":     r.get("symbol", ""),
                        "setup_type": _fmt_setup_type(r.get("setup_type", "")),
                        "direction":  r["direction"],
                        "confidence": 0,
                    })
            self._recent_ai_signals = self._recent_ai_signals[:20]
        except Exception:
            pass

    def get_cached(self, symbol):
        with self._lock:
            return self._cache.get(symbol)

    def get_status(self):
        with self._lock:
            now = time.time()
            recent = [t for t in self._inference_window if now - t < 60]
            rate_per_min = len(recent)
            signals = list(self._recent_ai_signals)
            cur_model = self._or_model
            rl_models = {m: round(until - now) for m, until in self._model_rl_until.items()
                         if until > now}
            active_sigs = {s: dict(v) for s, v in self._active_signals.items()}
            next_ts = dict(self._next_analysis_ts)

        return {
            "online":              self.enabled,
            "version":             "v5.0",
            "provider":            "openrouter",
            "model":               cur_model,
            "daily_requests_used": _or_daily_count,
            "daily_requests_limit": _OR_DAILY_LIMIT,
            "active_models":       len(config.WEIGHTS),
            "latency_ms":          self._last_latency,
            "inference_per_min":   rate_per_min,
            "total_inferences":    self._inference_count,
            "current_model":       cur_model,
            "rate_limited_models": rl_models,
            "last_error":          self.last_error,
            "recent_signals":      signals,
            "active_signals":      active_sigs,
            "next_analysis_ts":    next_ts,
        }

    def get_recent_signals(self):
        with self._lock:
            return list(self._recent_ai_signals)

    def get_pipeline_log(self):
        with self._lock:
            return list(reversed(self._pipeline_events))

    def get_active_run(self):
        with self._lock:
            return dict(self._active_run)

    def _record_evt(self, **kwargs):
        evt = {"ts": round(time.time(), 3), **kwargs}
        with self._lock:
            self._pipeline_events.append(evt)
            self._active_run = evt
        return evt

    # -----------------------------------------------------------------------
    # HTTP calls
    # -----------------------------------------------------------------------

    def _throttle(self):
        with self._rate_lock:
            now = time.time()
            wait = self._MIN_CALL_INTERVAL - (now - self._last_call_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_call_ts = time.time()

    @staticmethod
    def _parse_retry_after(resp) -> float:
        raw = resp.headers.get("retry-after")
        if not raw:
            return 0.0
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    def _post_or_model(self, model, system_prompt, payload_text, timeout=90, max_tokens=4000):
        """POST one request to the OpenRouter chat completions endpoint."""
        key = _get_or_key()
        headers = {
            "Authorization":  f"Bearer {key}",
            "Content-Type":   "application/json",
            "HTTP-Referer":   "https://github.com/NisalU/New1",
            "X-Title":        "CryptoSignalBot",
        }
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": payload_text},
            ],
            "temperature": 0.2,
            "max_tokens":  max_tokens,
        }
        self._throttle()
        resp = requests.post(OR_BASE_URL, json=body, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            data   = resp.json()
            choice = data["choices"][0]
            return model, choice["message"]["content"], choice.get("finish_reason")
        print(f"[openrouter] {model} HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 429:
            ra = self._parse_retry_after(resp)
            raise RuntimeError(f"RATE_LIMIT:{ra}:{model}: {resp.text[:120]}")
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"AUTH_ERROR: OpenRouter returned {resp.status_code} — check OPENROUTER_API_KEY "
                f"({resp.text[:120]})"
            )
        if resp.status_code == 400:
            try:
                err = resp.json().get("error", {}) or {}
            except ValueError:
                err = {}
            err_msg = str(err.get("message") or "").lower()
            if "max_tokens" in err_msg or "max completion tokens" in err_msg:
                raise RuntimeError(f"TRUNCATED:{model}: {resp.text[:160]}")
            raise RuntimeError(f"MODEL_ERROR:{model}: {resp.status_code} {resp.text[:80]}")
        if resp.status_code == 404:
            raise RuntimeError(f"MODEL_ERROR:{model}: {resp.status_code} {resp.text[:80]}")
        raise RuntimeError(f"HTTP_ERROR:{model}: {resp.status_code} {resp.text[:160]}")

    def _post_with_truncation_retry(self, model, system_prompt, payload_text):
        token_budgets = (self._MAX_TOKENS_PRIMARY, self._MAX_TOKENS_RETRY)
        content = None
        for i, max_tokens in enumerate(token_budgets):
            try:
                _, content, finish_reason = self._post_or_model(
                    model, system_prompt, payload_text, max_tokens=max_tokens
                )
            except RuntimeError as e:
                if str(e).startswith("TRUNCATED:") and i == 0:
                    continue
                raise

            parsed, repaired = _try_parse_json(content)
            if parsed is not None:
                return (repaired if repaired is not None else content), True

            if finish_reason == "length" and i == 0:
                continue

            return content, False

        return content, False

    def _call_openrouter(self, system_prompt, payload_text):
          """Rotating-model caller — iterates OR_MODELS until one succeeds."""
          if not _get_or_key():
              raise RuntimeError(
                  "OPENROUTER_API_KEY not set — add it to your .env file. "
                  "Free key at https://openrouter.ai/keys"
              )

          # ── Daily budget check ──────────────────────────────────────────────
          ok, secs_until_reset = _or_check_budget()
          if not ok:
              raise RuntimeError(
                  f"RATE_LIMIT:{secs_until_reset}:budget: "
                  f"OpenRouter daily limit of {_OR_DAILY_LIMIT} requests reached. "
                  f"Resets in {int(secs_until_reset // 3600)}h {int((secs_until_reset % 3600) // 60)}m."
              )

          now_t = time.time()
          last_err = None

          for model in OR_MODELS:
              # Skip models still in cooldown
              if now_t < self._model_rl_until.get(model, 0):
                  wait_s = self._model_rl_until[model] - now_t
                  log.debug("[openrouter] skipping %s — cooldown %.0fs", model, wait_s)
                  continue

              try:
                  self._record_evt(stage="model_attempt", provider="openrouter", model=model)
                  response, parsed_ok = self._post_with_truncation_retry(model, system_prompt, payload_text)
                  if not parsed_ok:
                      self._model_rl_until[model] = time.time() + self._JSON_FAIL_COOLDOWN
                      self._record_evt(
                          stage="model_json_fail", provider="openrouter", model=model,
                          message="Unparseable JSON after retry",
                          cooldown_s=self._JSON_FAIL_COOLDOWN,
                      )
                      last_err = RuntimeError(f"invalid JSON from {model}")
                      continue
                  self._model_rl_until.pop(model, None)
                  self._active_models.add(model)
                  self._record_evt(stage="model_success", provider="openrouter", model=model)
                  return model, response

              except RuntimeError as e:
                  msg = str(e)
                  if msg.startswith("AUTH_ERROR"):
                      raise
                  if msg.startswith("RATE_LIMIT:"):
                      parts = msg.split(":", 2)
                      retry_after = 0.0
                      if len(parts) >= 2:
                          try:
                              retry_after = float(parts[1])
                          except ValueError:
                              pass
                      # Cap at our own limit even when server asks for more
                      cooldown = min(retry_after, self._MODEL_RL_SECONDS) if retry_after > 0 else self._MODEL_RL_SECONDS
                      self._model_rl_until[model] = time.time() + cooldown
                      self._record_evt(
                          stage="model_rate_limited", provider="openrouter", model=model,
                          cooldown_s=cooldown, from_retry_after=bool(retry_after > 0),
                      )
                      log.warning("[openrouter] %s rate-limited (%.0fs cooldown) — trying next model", model, cooldown)
                      last_err = e
                      continue
                  if msg.startswith("MODEL_ERROR:"):
                      # 404/unavailable — skip for the rest of the session (6 h)
                      self._model_rl_until[model] = time.time() + 21600
                      log.warning("[openrouter] %s unavailable (404) — skipping for 6h", model)
                      last_err = e
                      continue
                  raise

          # All models exhausted
          wait_s = min(
              (v - now_t) for v in self._model_rl_until.values() if v > now_t
          ) if self._model_rl_until else self._MODEL_RL_SECONDS
          raise RuntimeError(
              f"RATE_LIMIT:{wait_s}:all: All OpenRouter models rate-limited. "
              f"Last error: {last_err}"
          )
    
    def _call_ai(self, payload_text):
        return self._call_openrouter(SYSTEM_PROMPT, payload_text)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def _wait_result(self, symbol, analysis, reason, regime=None, extra=None):
        result = {
            "symbol":           symbol,
            "interval":         config.AI_INTERVAL,
            "updated":          int(time.time()),
            "price":            analysis["price"],
            "engine_score":     analysis["composite"],
            "model":            None,
            "signal":           "WAIT",
            "direction":        None,
            "order_type":       "MARKET",
            "setup_type":       "none",
            "confidence":       0,
            "entry":            None,
            "stop":             None,
            "tp1":              None,
            "tp2":              None,
            "risk_reward":      None,
            "orderflow_read":   "",
            "reasoning":        reason,
            "invalidation":     "",
            "gated":            False,
            "gate_reason":      None,
            "market_regime":    (regime or {}).get("regime"),
            "htf_bias":         None,
            "liquidity_context":None,
            "trade_quality":    None,
            "critic":           None,
            "latency_ms":       None,
            "signal_active":    False,
        }
        if extra:
            result.update(extra)
        with self._lock:
            self._cache[symbol] = result
        return result

    def analyze(self, symbol):
        """Run full pipeline for `symbol`. Blocking (call in a thread)."""
        run_id = f"{symbol}:{int(time.time())}"

        # ── Stage 1: Market data ──────────────────────────────────────────
        self._record_evt(run_id=run_id, stage="market_data", status="fetching", symbol=symbol)
        t_data   = time.time()
        analysis = engine.get_state(symbol, config.AI_INTERVAL)
        ov       = analysis.get("overlays", {})
        a        = atr(analysis["candles"]) or analysis["price"] * 0.005
        self._record_evt(
            run_id=run_id, stage="market_data", status="done", symbol=symbol,
            price=analysis["price"], composite=round(analysis["composite"], 1),
            duration_ms=int((time.time() - t_data) * 1000),
        )

        regime            = market_regime.classify(analysis)
        structural_quality = trade_quality.grade(analysis, plan=None, regime=None)

        self._record_evt(
            run_id=run_id, stage="market_data_regime", symbol=symbol,
            regime=regime["regime"], direction=analysis["direction"],
            composite=round(analysis["composite"], 1),
        )

        # ── Stage 2: Signal memory context ───────────────────────────────
        self._record_evt(run_id=run_id, stage="memory_context", status="loading", symbol=symbol)
        memory_rows = signal_memory.recent_similar(symbol, limit=config.SIGNAL_MEMORY_LOOKBACK)
        self._record_evt(
            run_id=run_id, stage="memory_context", status="done", symbol=symbol,
            found=len(memory_rows),
        )

        # ── Stage 3: Primary AI call ──────────────────────────────────────
        self._record_evt(
            run_id=run_id, stage="ai_call", status="start", symbol=symbol,
            interval=config.AI_INTERVAL, htf_interval=config.AI_HTF_INTERVAL,
        )
        market    = _compact_market(analysis, symbol, regime, structural_quality, memory_rows)
        user_text = (
            "Here is the live market data and context. Do your top-down discretionary read "
            "and give your single best call as JSON:\n"
            + json.dumps(market, separators=(",", ":"))
        )
        t0 = time.time()
        model, raw = self._call_ai(user_text)
        latency_ms = int((time.time() - t0) * 1000)

        provider = "openrouter"
        self._record_evt(
            run_id=run_id, stage="ai_call", status="done", symbol=symbol,
            model=model, provider=provider, latency_ms=latency_ms,
        )

        with self._lock:
            self._last_latency = latency_ms
            self._inference_count += 1
            self._inference_window.append(time.time())

        try:
            out = json.loads(raw)
        except ValueError:
            raise RuntimeError(f"AI returned non-JSON: {raw[:160]}")

        signal = str(out.get("decision", "WAIT")).upper()
        if signal not in ("LONG", "SHORT", "WAIT"):
            signal = "WAIT"

        order_type = str(out.get("order_type", "MARKET")).upper()
        if order_type not in ("MARKET", "LIMIT"):
            order_type = "MARKET"

        setup_type_raw = str(out.get("setup_type") or "").strip().lower() or "none"

        def num(v):
            try:
                return round(float(v), 8) if v is not None else None
            except (TypeError, ValueError):
                return None

        take_profit = out.get("take_profit") or []
        if not isinstance(take_profit, list):
            take_profit = [take_profit]
        tp1 = num(take_profit[0]) if len(take_profit) > 0 else None
        tp2 = num(take_profit[1]) if len(take_profit) > 1 else None

        entry = num(out.get("entry"))
        stop  = num(out.get("stop_loss"))

        # Fallback TP calculation when the AI omits or returns null take_profit
        if entry is not None and stop is not None and signal in ("LONG", "SHORT"):
            risk = abs(entry - stop)
            sign = 1 if signal == "LONG" else -1
            if tp1 is None and risk > 0:
                tp1 = round(entry + sign * risk * 1.5, 8)
                log.info("[ai] tp1 fallback for %s: %.6f", symbol, tp1)
            if tp2 is None and risk > 0:
                tp2 = round(entry + sign * risk * 3.0, 8)
                log.info("[ai] tp2 fallback for %s: %.6f", symbol, tp2)

        risk_reward = None
        if entry is not None and stop is not None and tp1 is not None and abs(entry - stop) > 0:
            risk_reward = round(abs(tp1 - entry) / abs(entry - stop), 2)

        htf = market.get("higher_timeframe") or {}

        result = {
            "symbol":           symbol,
            "interval":         config.AI_INTERVAL,
            "updated":          int(time.time()),
            "price":            analysis["price"],
            "engine_score":     analysis["composite"],
            "model":            model,
            "model_used":       model,
            "provider":         provider,
            "signal":           signal,
            "direction":        signal if signal in ("LONG", "SHORT") else None,
            "order_type":       order_type,
            "setup_type":       setup_type_raw,
            "confidence":       max(0, min(100, int(out.get("confidence") or 0))),
            "entry":            entry,
            "stop":             stop,
            "tp1":              tp1,
            "tp2":              tp2,
            "limit_entry":      entry if order_type == "LIMIT" else None,
            "risk_reward":      risk_reward,
            "orderflow_read":   "",
            "reasoning":        str(out.get("reason") or "")[:600],
            "invalidation":     "",
            "gated":            False,
            "gate_reason":      None,
            "market_regime":    regime["regime"],
            "htf_bias":         htf.get("direction"),
            "liquidity_context":market["liquidity"],
            "trade_quality":    None,
            "critic":           None,
            "latency_ms":       latency_ms,
            "signal_active":    False,
        }

        self._record_evt(
            run_id=run_id, stage="ai_parsed", symbol=symbol,
            signal=signal, confidence=result["confidence"],
            setup_type=result["setup_type"], model=model, provider=provider,
        )

        # ── Stage 4: Trade quality ────────────────────────────────────────
        self._record_evt(run_id=run_id, stage="trade_quality", status="computing", symbol=symbol)
        plan = {"entry": result["entry"], "stop": result["stop"], "tp1": result["tp1"]}
        final_quality = trade_quality.grade(analysis, plan=plan, regime=None)
        result["trade_quality"] = final_quality
        self._record_evt(
            run_id=run_id, stage="trade_quality", status="done", symbol=symbol,
            grade=final_quality["grade"] if final_quality else None,
        )

        # ── Stage 5: Risk gate — reject low-quality signals ───────────────
        # Signals that failed structural quality checks are downgraded to WAIT
        # here so they never reach the signal-memory write or active-signal lock.
        if result["signal"] in ("LONG", "SHORT") and final_quality:
            if final_quality["grade"] == "Reject":
                gate_reason = (
                    f"Trade quality Reject (score {final_quality['score']}/12): "
                    f"loc={final_quality['location_quality']}, "
                    f"struct={final_quality['structure_quality']}, "
                    f"liq={final_quality['liquidity_quality']}, "
                    f"of={final_quality['orderflow_quality']}"
                )
                log.info("[risk-gate] %s %s gated — %s", symbol, result["signal"], gate_reason)
                self._record_evt(
                    run_id=run_id, stage="risk_gate", symbol=symbol,
                    original_signal=result["signal"], gate_reason=gate_reason,
                )
                result["signal"]     = "WAIT"
                result["direction"]  = None
                result["gated"]      = True
                result["gate_reason"] = gate_reason

        # ── Stage 6: Signal out ───────────────────────────────────────────
        self._record_evt(
            run_id=run_id, stage="signal_out", symbol=symbol,
            signal=result["signal"], confidence=result["confidence"],
            model=model, provider=provider, latency_ms=latency_ms,
            setup_type=result["setup_type"],
            gated=result.get("gated", False),
            gate_reason=result.get("gate_reason"),
        )

        # ── Stage 7: Signal memory write + active-signal lock ─────────────
        if result["signal"] in ("LONG", "SHORT"):
            signal_memory.record({
                "symbol":           symbol,
                "timestamp":        result["updated"],
                "setup_type":       result["setup_type"],
                "direction":        result["signal"],
                "entry":            result["entry"],
                "stop":             result["stop"],
                "target":           result["tp1"],
                "market_condition": regime["regime"],
                "trade_quality":    result["trade_quality"]["grade"] if result["trade_quality"] else None,
                "ai_reasoning":     result["reasoning"],
                "result":           "pending",
            })
            # Record in in-memory table
            with self._lock:
                self._recent_ai_signals.insert(0, {
                    "time":       result["updated"],
                    "symbol":     symbol,
                    "setup_type": _fmt_setup_type(result["setup_type"]),
                    "direction":  result["signal"],
                    "confidence": result["confidence"],
                })
                self._recent_ai_signals = self._recent_ai_signals[:20]

            # Lock further AI analysis until signal resolves (MARKET only)
            # LIMIT signals are tracked as pending orders, not active locks
            if getattr(config, "ACTIVE_SIGNAL_LOCK", True):
                if result.get("order_type", "MARKET") == "MARKET":
                    self._record_active_signal(symbol, result)
                elif result.get("order_type") == "LIMIT":
                    self.add_pending_limit(result)

        with self._lock:
            self._cache[symbol] = result
        self.last_error = None
        return result

    def analyze_safe(self, symbol):
        """Like analyze() but never raises; returns cached/error placeholder.

        If an active signal exists for this symbol (price hasn't hit stop or
        target), skip the AI call and return the cached result annotated with
        signal_active=True.
        """
        # Check active-signal lock
        if getattr(config, "ACTIVE_SIGNAL_LOCK", True):
            try:
                analysis = engine.get_state(symbol, config.AI_INTERVAL)
                current_price = analysis["price"]
            except Exception:
                current_price = None

            if current_price is not None:
                is_active, lock_reason = self._check_signal_active(symbol, current_price)
                if is_active:
                    cached = self.get_cached(symbol)
                    if cached:
                        annotated = dict(cached)
                        annotated["signal_active"] = True
                        annotated["signal_lock_reason"] = lock_reason
                        log.info("[signal-lock] Skipping AI for %s — %s", symbol, lock_reason)
                        return annotated

        # Normal analysis
        try:
            return self.analyze(symbol)
        except Exception as e:
            msg = str(e)
            self.last_error = msg
            is_rate_limit = msg.startswith("RATE_LIMIT:")
            if is_rate_limit:
                log.warning("All OpenRouter models rate-limited for %s — returning cached", symbol)
            else:
                traceback.print_exc()
            cached = self.get_cached(symbol)
            if cached:
                return cached
            return {
                "symbol":   symbol,
                "interval": config.AI_INTERVAL,
                "updated":  int(time.time()),
                "error":    msg[:200],
            }


ai_analyst = AIAnalyst()
