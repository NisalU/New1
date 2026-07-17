"""AI analyst — discretionary structure/liquidity read on top of the
confluence engine.

Pipeline (critic removed):
    Market data (engine.py)
        -> Signal memory context (signal_memory.py)
        -> Primary AI analyst (Groq, single SYSTEM_PROMPT for all models)
        -> Server-side risk gate (_apply_risk_gate)
        -> Signal memory write

Active-signal lock:
    Once a LONG or SHORT fires, AI analysis for that symbol is skipped
    until price crosses the stop (signal lost) or reaches tp1 (target hit).
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
from strategies.helpers import atr

log = logging.getLogger("ai_analyst")

# ---------------------------------------------------------------------------
# Provider — Groq REST API
# ---------------------------------------------------------------------------
GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

GROQ_MODELS = [
    m for m in [
        os.environ.get("GROQ_MODEL", ""),
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-20b",
        "llama-3.1-8b-instant",
    ] if m
]

PROMPT_CANDLE_COUNT = getattr(config, "AI_PROMPT_CANDLES", 6)
PROMPT_CVD_POINTS   = getattr(config, "AI_PROMPT_CVD_POINTS", 12)
PROMPT_MEMORY_ROWS  = getattr(config, "AI_PROMPT_MEMORY_ROWS", 3)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an active cryptocurrency trader who captures both large and small profit opportunities.

YOUR JOB is to find the best trade right now — not to wait for perfection.

━━━ PRIMARY RULES ━━━

1. TRUST THE ENGINE SCORE.
   The `engine_composite_score` summarises multi-timeframe confluence already.
   • Score ≤ −25  → strong SHORT bias.  Find a SHORT entry unless structure flat-out forbids it.
   • Score ≥ +25  → strong LONG bias.   Find a LONG entry unless structure flat-out forbids it.
   • −25 to +25   → neutral zone.       Look for range-boundary setups; WAIT only if nothing is clean.

2. BIAS BEATS PERFECTION.
   A clear directional bias WITH an identifiable entry is always better than waiting.
   "Mixed signals on lower timeframes" is NOT a reason to WAIT — it is normal market noise.
   Trade the dominant bias, not the noise.

3. RANGE TRADES COUNT.
   If price is ranging, trade bounces from range boundaries (support/resistance, order blocks, FVGs).
   Do NOT demand a breakout before acting. Range bounces are high-R setups.

4. RISK:REWARD REQUIREMENTS.
   • Scalp / intraday:  R:R ≥ 1.2   (quick moves off structure)
   • Swing:             R:R ≥ 1.5
   • There is NO requirement for R:R ≥ 2.  Good trades exist at 1.2–1.8.

5. ENTRY, STOP, TAKE-PROFIT — MANDATORY for every LONG/SHORT. NEVER leave null or empty.
   • Entry: current price for MARKET orders, or the exact retest level for LIMIT orders.
   • Stop:  beyond the structural invalidation level — 0.5–1.5 ATR.
   • take_profit: MUST be a 2-element array [tp1, tp2]. Both values are required.
     - tp1 = nearest opposing structural level at minimum 1.2× risk from entry.
     - tp2 = next structural level beyond tp1.
     - No structural level? Calculate: risk = abs(entry - stop_loss),
       tp1 = entry ± risk*1.5,  tp2 = entry ± risk*3.0  (+ for LONG, − for SHORT).
     - Returning [] or [null] for take_profit is a hard failure. Always provide numbers.

6. ORDER TYPE — required for every LONG/SHORT signal.
   • "MARKET" — price is at or within 0.3 ATR of your entry level right now. Enter immediately.
   • "LIMIT"  — price needs to retrace more than 0.3 ATR to reach your entry. Place a limit order.
   Both types are valid signals. LIMIT means "watch for this level; fill when price arrives."
   You may fire a MARKET signal at current price AND a LIMIT signal at a retest level simultaneously
   when both setups are present.

7. WHEN TO RETURN WAIT (strict — only these cases):
   • No identifiable support, resistance, order block, or FVG within 1.5 ATR of current price.
   • A major macro event is imminent (funding rate > 0.15%, OI spike, news gap — flagged in data).
   • Price is mid-air between two levels with less than 0.8 ATR to the nearest boundary — genuinely structureless.
   WAIT is NOT appropriate because "the market could go either way" — ALL markets can go either way.

8. CONFIDENCE MEANING.
   Confidence reflects conviction in the trade, not in waiting.
   • 80–100 = strong setup, take the trade
   • 60–79  = decent setup, valid entry
   • 40–59  = marginal but tradable with tight stop
   • < 40   = WAIT
   A WAIT signal should always have confidence < 40.
   A LONG/SHORT signal should have confidence ≥ 55.

━━━ EXECUTION PROCESS ━━━

Step 1 — Read the engine_composite_score to establish direction bias.
Step 2 — Confirm with higher_timeframe direction and key_levels (support, resistance, order blocks, FVGs).
Step 3 — Identify entry: current level (MARKET) or nearest retest level (LIMIT).
Step 4 — Set stop beyond the invalidating structure level.
Step 5 — Set TP1 at nearest opposing level, TP2 beyond it. Use R:R multiples if no clear level.
Step 6 — Decide order_type: MARKET if price is at entry now, LIMIT if waiting for retest.
Step 7 — If R:R ≥ 1.2, call LONG or SHORT. If nothing clean after all steps, call WAIT.

━━━ OUTPUT ━━━

Return ONLY valid JSON, no extra text:

{
  "decision": "LONG|SHORT|WAIT",
  "confidence": 55-100,
  "order_type": "MARKET|LIMIT",
  "setup_type": "short_id e.g. fvg_bounce / ob_rejection / liquidity_sweep / range_bottom / ema_cross",
  "entry": <number>,
  "stop_loss": <number>,
  "take_profit": [<tp1_number>, <tp2_number>],
  "reason": "One sentence ≤ 35 words: direction bias + key structure used."
}"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_groq_key():
    return os.environ.get("GROQ_API_KEY", "").strip()


def _fnum(x, digits=6):
    return round(float(x), digits)


def _htf_summary(symbol):
    try:
        htf = engine.get_state(symbol, config.AI_HTF_INTERVAL)
    except Exception:
        return None
    ov = htf.get("overlays", {})
    structure = ov.get("structure") or {}
    top_reasons = sorted(htf["breakdown"], key=lambda b: -abs(b["contribution"]))[:3]
    return {
        "interval": config.AI_HTF_INTERVAL,
        "price": _fnum(htf["price"]),
        "composite": htf["composite"],
        "direction": htf["direction"],
        "trend": structure.get("trend"),
        "structure_events": structure.get("events"),
        "top_reasons": [r for b in top_reasons for r in b["reasons"][:1] if r],
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
            "contribution": b["contribution"], "reasons": b["reasons"][:2],
        }
        for b in analysis["breakdown"]
    ]

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
            for ob in ov["order_blocks"][:3]
        ]
    if ov.get("fvgs"):
        levels["fvg_mids"] = [
            {"type": f["type"], "mid": _fnum(f["mid"])} for f in ov["fvgs"][:3]
        ]

    fundamentals = ov.get("fundamentals")

    return {
        "symbol": analysis["symbol"],
        "chart": config.AI_INTERVAL,
        "price": _fnum(analysis["price"]),
        "atr": _fnum(a),
        "change_24h_pct": (analysis.get("ticker") or {}).get("change_pct"),
        "engine_composite_score": analysis["composite"],
        "engine_direction": analysis["direction"],
        "market_regime": regime,
        "structural_quality": structural_quality,
        "higher_timeframe": _htf_summary(symbol),
        "liquidity": _liquidity_context(ov),
        "strategies": strategies,
        "orderflow_divergence": ov.get("divergence"),
        "cvd_last_24": cvd_tail,
        "key_levels": levels,
        "futures_fundamentals": fundamentals,
        "recent_similar_setups": memory_rows[:PROMPT_MEMORY_ROWS],
        "risk_warnings": _risk_warnings(analysis, regime, memory_rows),
        "recent_candles": recent,
    }


def _fmt_setup_type(raw):
    if not raw or raw == "none":
        return "—"
    words = str(raw).replace("_", " ").replace("+", " + ").split()
    label = " ".join(w.capitalize() for w in words)
    return label[:30]


def _repair_truncated_json(text):
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    s = text[start:]
    in_string = False
    escape = False
    stack = []
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
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_string:
        s += '"'
    s = s.rstrip().rstrip(",")
    if s.endswith(":"):
        s += "null"
    closers = {"{": "}", "[": "]"}
    while stack:
        s += closers[stack.pop()]
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
        self.enabled = bool(_get_groq_key())
        self.last_error = None

        # Engine status metrics
        self._last_latency = None
        self._inference_count = 0
        self._inference_window = deque(maxlen=120)
        self._active_models = set()

        # Per-model rate-limit tracking
        self._model_rl_until: dict = {}
        self._MODEL_RL_SECONDS = getattr(config, "MODEL_RL_COOLDOWN", 60)

        # Global throttle
        self._rate_lock = threading.Lock()
        self._last_call_ts = 0.0
        self._MIN_CALL_INTERVAL = getattr(config, "AI_MIN_CALL_INTERVAL", 2.1)

        # Token budgets
        self._MAX_TOKENS_PRIMARY = getattr(config, "AI_MAX_TOKENS", 2000)
        self._MAX_TOKENS_RETRY   = getattr(config, "AI_MAX_TOKENS_RETRY", 3000)
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
            "provider":            "groq",
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

    def _post_groq_model(self, model, system_prompt, payload_text, timeout=60, max_tokens=2000):
        key = _get_groq_key()
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": payload_text},
            ],
            "temperature": 0.2,
            "max_tokens":  max_tokens,
            "response_format": {"type": "json_object"},
        }
        self._throttle()
        resp = requests.post(GROQ_BASE_URL, json=body, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            data   = resp.json()
            choice = data["choices"][0]
            return model, choice["message"]["content"], choice.get("finish_reason")
        print(f"[groq] {model} HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 429:
            ra = self._parse_retry_after(resp)
            raise RuntimeError(f"RATE_LIMIT:{ra}:{model}: {resp.text[:120]}")
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"AUTH_ERROR: Groq returned {resp.status_code} — check GROQ_API_KEY "
                f"({resp.text[:120]})"
            )
        if resp.status_code == 400:
            try:
                err = resp.json().get("error", {}) or {}
            except ValueError:
                err = {}
            code    = str(err.get("code") or "")
            err_msg = str(err.get("message") or "").lower()
            if code == "json_validate_failed" or "max_tokens" in err_msg or "max completion tokens" in err_msg:
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
                _, content, finish_reason = self._post_groq_model(
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

    def _call_groq_models(self, system_prompt, payload_text):
        if not _get_groq_key():
            raise RuntimeError(
                "GROQ_API_KEY not set — restart server.py and enter your key. "
                "Get a free key at https://console.groq.com/keys"
            )
        candidates = [self._or_model] if self._or_model else []
        candidates += [m for m in GROQ_MODELS if m and m not in candidates]

        now_t  = time.time()
        models  = [m for m in candidates if now_t >= self._model_rl_until.get(m, 0)]
        skipped = [m for m in candidates if m not in models]
        if skipped:
            log.info("Skipping rate-limited Groq models: %s", skipped)
        if not models:
            raise RuntimeError("RATE_LIMIT:all Groq models are individually rate-limited")

        last_exc = None
        for model in models:
            try:
                self._record_evt(stage="model_attempt", provider="groq", model=model)
                content, ok = self._post_with_truncation_retry(model, system_prompt, payload_text)
                if not ok:
                    self._model_rl_until[model] = time.time() + self._JSON_FAIL_COOLDOWN
                    if self._or_model == model:
                        self._or_model = None
                    self._record_evt(
                        stage="model_rate_limited", provider="groq", model=model,
                        message="Unparseable JSON after retry — trying next model",
                        cooldown_s=self._JSON_FAIL_COOLDOWN,
                    )
                    last_exc = RuntimeError(f"invalid JSON from {model}")
                    continue
                self._or_model = model
                self._model_rl_until.pop(model, None)
                self._active_models.add(model)
                self._record_evt(stage="model_success", provider="groq", model=model)
                return model, content
            except RuntimeError as e:
                msg = str(e)
                last_exc = e
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
                    cooldown = retry_after if retry_after > 0 else self._MODEL_RL_SECONDS
                    self._model_rl_until[model] = time.time() + cooldown
                    if self._or_model == model:
                        self._or_model = None
                    self._record_evt(
                        stage="model_rate_limited", provider="groq", model=model,
                        cooldown_s=cooldown, from_retry_after=bool(retry_after > 0),
                    )
                    continue
                if msg.startswith("MODEL_ERROR:"):
                    self._model_rl_until[model] = time.time() + self._MODEL_RL_SECONDS
                    if self._or_model == model:
                        self._or_model = None
                    continue
                raise
            except requests.RequestException as e:
                last_exc = e
                continue

        raise RuntimeError(f"RATE_LIMIT:all Groq models failed: {last_exc}")

    def _call_ai(self, payload_text):
        return self._call_groq_models(SYSTEM_PROMPT, payload_text)

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

        provider = "groq"
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

        # ── Stage 5: Signal out (no critic) ──────────────────────────────
        self._record_evt(
            run_id=run_id, stage="signal_out", symbol=symbol,
            signal=result["signal"], confidence=result["confidence"],
            model=model, provider=provider, latency_ms=latency_ms,
            setup_type=result["setup_type"],
            gated=result.get("gated", False),
            gate_reason=result.get("gate_reason"),
        )

        # ── Stage 6: Signal memory write + active-signal lock ─────────────
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
                log.warning("All Groq models rate-limited for %s — returning cached", symbol)
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
