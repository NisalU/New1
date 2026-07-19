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
      _OR_MODEL_PRIMARY,
      "meta-llama/llama-3.3-70b-instruct:free",
      "qwen/qwen3-235b-a22b:free",
      "mistralai/mistral-7b-instruct:free",
      "tencent/hunyuan-a13b-instruct:free",
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

PROMPT_CANDLE_COUNT  = getattr(config, "AI_PROMPT_CANDLES",     50)
PROMPT_HTF_CANDLES   = getattr(config, "AI_PROMPT_HTF_CANDLES", 10)
PROMPT_CVD_POINTS    = getattr(config, "AI_PROMPT_CVD_POINTS",  30)
PROMPT_MEMORY_ROWS   = getattr(config, "AI_PROMPT_MEMORY_ROWS",  5)

# ---------------------------------------------------------------------------
# System prompt  (optimised for 256k-context model — detailed chain-of-thought)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
You are an elite institutional crypto trader and quantitative analyst. Your task is to perform a rigorous top-down multi-timeframe analysis and deliver one precise, high-conviction trade call.

Respond with ONLY the JSON object defined below. No reasoning text before or after it.

═══════════════════════════════════════════════
ANALYTICAL FRAMEWORK (work through every step)
═══════════════════════════════════════════════

STEP 1 — MACRO CONTEXT  [market_regime, structural_quality, engine_composite_score]
• Identify regime: trending / ranging / volatile / low_volatility
• Engine composite >60 = bullish lean, <-60 = bearish lean, near 0 = choppy
• structural_quality >0.6 = high-quality structure. <0.3 = wait for structure to develop.
• In ranging or volatile regime: reduce confidence by 15 on any directional trade. Only Spring/UTAD/confirmed sweeps qualify.
• In trending regime: bias toward trend continuation after pullbacks to structure.

STEP 2 — 4H STRUCTURE  [higher_timeframe.candles + higher_timeframe.key_levels]
• Read the 4H candles. Classify trend: HH+HL = bullish, LH+LL = bearish, mixed = ranging.
• Locate the last confirmed BOS (Break of Structure) and CHoCH (Change of Character).
• Identify 4H Order Blocks: last bearish candle before a bullish impulse (bullish OB); last bullish candle before a bearish impulse (bearish OB). These are highest-priority entry zones.
• Identify 4H FVGs: three-candle imbalances where candle 1 high and candle 3 low do not overlap (bullish FVG) or candle 1 low and candle 3 high do not overlap (bearish FVG).
• Equal highs on 4H = buy-side liquidity (BSL) resting above. Equal lows = sell-side liquidity (SSL) resting below.
• 4H Premium/Discount: above 50% of last 4H swing range = premium (look for shorts). Below 50% = discount (look for longs).
• THIS IS YOUR DIRECTIONAL BIAS. Never counter-trade the 4H unless 1H CHoCH is confirmed.

STEP 3 — 1H INTERNAL STRUCTURE  [recent_candles: 50 candles]
• Identify all swing highs/lows in the 50-candle window.
• Internal BOS (iBOS): break of a minor swing — confirms short-term directional push.
• CHoCH: reversal of last internal swing direction — confirms potential reversal.
• Displacement candles: large body (>1.5× average body size) with a gap — marks institutional participation.
• 1H Order Blocks: the candle directly before displacement. Price revisiting a 1H OB on the first touch = primary entry.
• Equal highs/lows on 1H = resting liquidity pools. Price will sweep these before reversing.
• Mitigation blocks: previously broken OBs price returns to — valid but lower priority than fresh OBs.
• Internal vs external range: internal range = between last swing low and last swing high. External = beyond those extremes. Liquidity hunts target external range levels.

STEP 4 — LIQUIDITY SWEEP ANALYSIS  [liquidity field]
• Confirmed sweep: wick through a liquidity pool + candle closes BACK inside the range. Highest-conviction trigger.
• Entry: AFTER the sweep candle fully closes back inside. Never on the wick.
• Stop: beyond the wick extreme + 0.1 ATR buffer.
• Recency: sweep within last 3 candles = highest conviction. 4-8 candles = moderate. >8 candles = lower priority.
• Inducement: minor swing that draws price one direction before the real move — wait for it to complete.
• Liquidity voids / FVGs: price returns to fill them — use as targets.
• BSL vs SSL: identify which pool has MORE resting liquidity — smart money hunts that side first.
• Both sides equal → choppy/ranging → WAIT.

STEP 5 — ORDERFLOW & CVD  [cvd_last_n: 30 points]
• CVD (Cumulative Volume Delta) = net buying/selling pressure over the period.
• Rising CVD + rising price = confirmed uptrend (healthy, add conviction).
• Rising CVD + falling price = hidden accumulation → LONG bias (strong divergence signal).
• Falling CVD + rising price = hidden distribution → SHORT bias (strong divergence signal).
• Falling CVD + falling price = confirmed downtrend (healthy, add conviction).
• CVD flat or choppy = no institutional directional commitment → reduce confidence by 10.
• Last 5 deltas all positive = aggressive buying. All negative = aggressive selling. Mixed = contested.
• Volume spike on directional candle = institutional participation (adds 5 confidence).
• Volume dry-up on retracement = normal pullback (confirms trend continuation, adds 5 confidence).
• CVD confirms structure but does NOT override sweep confirmation or 4H bias.

STEP 6 — WYCKOFF PHASES  [wyckoff field]
• Accumulation → Markup: Spring is the entry (sweep support + close inside = trap bears = LONG, highest conviction).
• Distribution → Markdown: UTAD is the entry (sweep resistance + close inside = trap bulls = SHORT, highest conviction).
• LPS (Last Point of Support): retest of breakout level as new support = continuation LONG.
• LPSY (Last Point of Supply): retest of breakdown level as new resistance = continuation SHORT.
• Ranging (Accumulation/Distribution without Spring/UTAD): do NOT enter — wait for the Spring or UTAD.
• SOT (Sign of Strength): strong bullish move with high volume after accumulation phase.
• SOW (Sign of Weakness): strong bearish move with high volume after distribution phase.

STEP 7 — ICT POWER OF THREE  [recent_candles, kill_zones]
• Daily sequence: Asian session = accumulation (range builds quietly), London Open = manipulation (false break one direction to hunt liquidity), NY session = distribution (real directional move, opposite to the manipulation).
• If in London session and price just swept Asian session lows → NY will likely push UP → LONG bias.
• If in London session and price just swept Asian session highs → NY will likely push DOWN → SHORT bias.
• Optimal Trade Entry (OTE): 62–79% Fibonacci retracement of the most recent impulse swing. Strongest entries cluster at the 70.5% level.
• OTE only valid when price is in discount zone (below 50% for longs) or premium zone (above 50% for shorts).

STEP 8 — ENTRY ZONE SELECTION  [key_levels field]
Priority order (use the nearest valid zone to current price):
  1. 4H OB + 1H OB overlap — institutional confluence, highest priority
  2. Confirmed sweep low/high with displacement close — direct trigger
  3. Breaker Block — broken OB acting as magnet from the opposite side
  4. 4H FVG + 1H FVG overlap — imbalance fill zone
  5. OTE zone (62–79% Fibonacci retracement of last impulse)
  6. VWAP + 1H OB confluence
  7. POC (Point of Control) + S/R level
  8. Equilibrium (50% of swing range) — lowest priority, only in trending market

• MARKET order: price already inside or touching the zone (within 0.25 ATR).
• LIMIT order: price needs to retrace to the zone.
• Invalidate the zone if price has already closed through it without reacting.

STEP 9 — VWAP & AVWAP  [vwap field]
• Above session VWAP = bullish intraday bias. Below = bearish intraday bias.
• VWAP reclaim (candle closed below then back above) = continuation LONG setup.
• VWAP loss (candle closed above then back below) = continuation SHORT setup.
• ±1σ band: first reaction zone for mean reversion trades.
• ±2σ band: fade ONLY if sweep confirmation + delta divergence both present.
• AVWAP from swing low = dynamic support (key level for LONG entries).
• AVWAP from swing high = dynamic resistance (key level for SHORT entries).
• Price between AVWAP low and AVWAP high = equilibrium — await directional resolution.

STEP 10 — SESSION & KILL ZONES  [kill_zones field]
HIGH QUALITY — all setups valid:
  London Open      07:00–10:00 UTC — directional moves begin, best for sweep reversals
  NY Open          12:00–15:00 UTC — highest volume, all setups valid
  London/NY Overlap 12:00–16:00 UTC — strongest trending moves develop here

MODERATE QUALITY — confirmed sweeps and structure only:
  Asian Session    00:00–04:00 UTC — range builds, only Spring/UTAD/confirmed sweeps
  Pre-London       05:00–07:00 UTC — reduced quality, await London confirmation

LOW QUALITY / DEAD ZONES — only ICT Power of Three setups:
  Post-Asia gap    04:00–07:00 UTC
  Mid-day lull     10:00–12:00 UTC
  Post-NY          20:00–00:00 UTC

Outside high-quality sessions → reduce confidence by 15.
In dead zone with no Spring/UTAD/sweep confirmation → WAIT.

STEP 11 — FUNDAMENTALS GATE  [futures_fundamentals field]
• Funding > +0.05%: longs crowded → subtract 15 from LONG confidence; favor SHORT.
• Funding < -0.05%: shorts crowded → subtract 15 from SHORT confidence; favor LONG.
• Funding > +0.10% or < -0.10%: extreme crowding → subtract 25. Only counter-trade with sweep confirmation.
• OI increasing + price aligned with OI direction: trend confirmed by positioning. Add 5 confidence.
• OI increasing + price opposing OI direction: position trap building → subtract 10 (high reversal risk).
• OI decreasing + price moving: short-covering or long-unwinding — weaker move → subtract 5.
• Long/short ratio > 3.0: retail heavily long → contrarian SHORT lean → subtract 10 from LONG.
• Long/short ratio < 0.5: retail heavily short → contrarian LONG lean → subtract 10 from SHORT.

STEP 12 — SIGNAL MEMORY  [recent_similar_setups]
• 3+ consecutive losses this symbol/direction → subtract 20, require one extra confluence factor.
• 2 consecutive losses → subtract 10, widen stop by 0.3 ATR.
• 2+ consecutive wins same setup_type → add 8 (this setup is working in current conditions).
• No memory rows → neutral, no adjustment.

═══════════════════════════════════════════════
CONFLUENCE SCORING — MINIMUM 4 FACTORS TO ENTER
═══════════════════════════════════════════════
Count each confirmed factor:
  [1]  4H structural bias aligned with trade direction
  [2]  1H BOS or CHoCH confirmed in entry direction
  [3]  Liquidity sweep confirmed (wick through pool + close back inside range)
  [4]  CVD/delta confirms direction (no divergence against the trade)
  [5]  Price at high-priority entry zone (4H OB, Breaker, FVG, OTE)
  [6]  VWAP position aligned with trade direction
  [7]  Active high-quality session (London or NY open)
  [8]  Wyckoff Spring or UTAD confirmed
  [9]  Fundamentals not opposing (no extreme crowded funding, OI aligned)
  [10] Engine composite score agrees with direction (>30 for LONG, <-30 for SHORT)

< 4 confirmed → WAIT (no exceptions, even if gut says otherwise)
4 confirmed  → LIMIT order only, confidence max 74
5 confirmed  → LIMIT or MARKET, confidence up to 84
6+ confirmed → full conviction, confidence up to 100

═══════════════════════════════════════════════
STOP LOSS & TAKE PROFIT
═══════════════════════════════════════════════
STOP LOSS:
• Place beyond sweep wick extreme OR beyond OB high/low that defines the zone.
• Add 0.1–0.15 ATR buffer beyond the invalidation level.
• Minimum SL distance: 0.5 ATR (avoid noise stop-outs).
• Maximum SL distance: 2.0 ATR. If required SL > 2.0 ATR → WAIT.
• Spring/UTAD: stop must go beyond sweep wick extreme, no exceptions.

TAKE PROFIT:
• TP1: next opposing liquidity pool (nearest equal highs/lows on the other side) or next significant S/R. Minimum R:R at TP1 = 1.5.
• TP2: next 4H level, OB, or unfilled 4H FVG. Minimum R:R at TP2 = 2.5.
• Fallback if no clear level: TP1 = Entry ± (Risk × 1.5), TP2 = Entry ± (Risk × 3.0).
• ABSOLUTE MINIMUM R:R = 1.5. Below 1.5 → WAIT.
• R:R ≥ 3.0 = elite setup, add 5 to confidence.

═══════════════════════════════════════════════
CONFIDENCE CALIBRATION
═══════════════════════════════════════════════
Start at 60 and adjust per every rule above.
95–100: Spring/UTAD + 4H+1H aligned + sweep confirmed + delta confirms + prime session + OB entry + R:R ≥ 3
85–94:  6+ confluence factors, clean structure, prime session, verified sweep
75–84:  5 confluence factors, structure clear, moderate session
65–74:  4 confluence factors, partial confirmation
< 65:   WAIT

═══════════════════════════════════════════════
ABSOLUTE WAIT (overrides everything)
═══════════════════════════════════════════════
• Climax candle just printed: single candle > 3× the 20-period average range
• Tight consolidation: last 5 candles range < 0.4 ATR (range building — wait for break)
• Required SL > 2.0 ATR
• R:R < 1.5
• 4H directly opposed with no confirmed 1H CHoCH
• Extreme funding (>0.15% or <-0.15%) with no sweep counter-confirmation
• Structure destroyed: a recent large candle invalidated all reference levels
• Dead session + no Spring/UTAD/sweep confirmation

═══════════════════════════════════════════════
SETUP TYPE CLASSIFICATION
═══════════════════════════════════════════════
sweep_reversal   — liquidity sweep + close back inside, clear directional intent
spring           — Wyckoff spring: sweep support + close inside = bullish
utad             — Wyckoff UTAD: sweep resistance + close inside = bearish
ob_bounce        — price returns to order block, first-touch reaction
breaker_rejection — broken OB flipped, price revisits for second-touch rejection
fvg_fill         — imbalance fill at FVG, reaction at midpoint or far edge
vwap_reclaim     — price reclaims VWAP from below (long) or loses it (short)
wyckoff_lps      — Last Point of Support in markup phase
wyckoff_lpsy     — Last Point of Supply in markdown phase
delta_divergence — CVD/price divergence signalling hidden accumulation or distribution
bos_continuation — clean BOS with pullback to OB/FVG, continuation
choch_reversal   — CHoCH confirmed, trade the new direction on first pullback
ote_entry        — Optimal Trade Entry at 62-79% Fibonacci retracement of impulse

═══════════════════════════════════════════════
    OUTPUT FORMAT
    ═══════════════════════════════════════════════
    Output ONLY this JSON. No markdown, no text before or after it.

    {"decision":"LONG|SHORT|WAIT","confidence":<int 65-100>,"order_type":"MARKET|LIMIT|NONE","setup_type":"<type>","entry":<number|null>,"stop_loss":<number|null>,"take_profit":[<tp1>,<tp2>],"reason":"<one sentence: trigger + 4H bias + R:R>"}

    WAIT:
    {"decision":"WAIT","confidence":0,"order_type":"NONE","setup_type":"none","entry":null,"stop_loss":null,"take_profit":[],"reason":"<one sentence: why no trade>"}
"""


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
        levels["support"] = [_fnum(lv["price"]) for lv in ov["support"][:8]]
    if ov.get("resistance"):
        levels["resistance"] = [_fnum(lv["price"]) for lv in ov["resistance"][:8]]
    if ov.get("volume_profile"):
        vp = ov["volume_profile"]
        levels["poc"] = _fnum(vp["poc"])
        levels["vah"] = _fnum(vp["vah"])
        levels["val"] = _fnum(vp["val"])
    if ov.get("order_blocks"):
        levels["order_blocks"] = [
            {"type": ob["type"], "top": _fnum(ob["top"]), "bottom": _fnum(ob["bottom"])}
            for ob in ov["order_blocks"][:6]
        ]
    # breaker blocks
    if ov.get("breaker_blocks"):
        levels["breaker_blocks"] = [
            {"type": bb["type"], "top": _fnum(bb["top"]), "bottom": _fnum(bb["bottom"])}
            for bb in ov["breaker_blocks"][:6]
        ]
    # FVGs — support both old "fvgs" key and new "fvg" key
    fvg_list = ov.get("fvg") or ov.get("fvgs") or []
    if fvg_list:
        levels["fvg"] = [
            {"type": f["type"], "top": _fnum(f["top"]), "bottom": _fnum(f["bottom"]),
             "mid": _fnum(f["mid"]), "displacement": f.get("displacement", False)}
            for f in fvg_list[:8]
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
            "sweeps":              (o.get("sweeps") or [])[:8],
            "inducements":         (o.get("inducements") or [])[:6],
            "liquidity_pools":     (o.get("liquidity_pools") or [])[:8],
            "liquidity_voids":     (o.get("voids") or [])[:6],
            "structure_trend":     structure.get("trend"),
            "structure_events":    structure.get("events") or [],
            "orderflow_divergence": o.get("divergence"),
            "macro_orderflow":     o.get("macro_flow"),
        }

    fundamentals = ov.get("fundamentals")

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
        self._MODEL_RL_SECONDS = getattr(config, "MODEL_RL_COOLDOWN", 60)

        # Global throttle
        self._rate_lock = threading.Lock()
        self._last_call_ts = 0.0
        self._MIN_CALL_INTERVAL = getattr(config, "AI_MIN_CALL_INTERVAL", 2.1)

        # Token budgets — increased for 256k-context model (richer chain-of-thought)
        self._MAX_TOKENS_PRIMARY = getattr(config, "AI_MAX_TOKENS", 4000)
        self._MAX_TOKENS_RETRY   = getattr(config, "AI_MAX_TOKENS_RETRY", 5000)
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
            "model":               OR_MODEL,
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
        """Single-model caller for OpenRouter with daily budget enforcement."""
        if not _get_or_key():
            raise RuntimeError(
                "OPENROUTER_API_KEY not set — add it to your .env file. "
                "Free key at https://openrouter.ai/keys"
            )

        model = OR_MODEL

        # ── Daily budget check ──────────────────────────────────────────────
        ok, secs_until_reset = _or_check_budget()
        if not ok:
            raise RuntimeError(
                f"RATE_LIMIT:{secs_until_reset}:{model}: "
                f"OpenRouter daily limit of {_OR_DAILY_LIMIT} requests reached. "
                f"Resets in {int(secs_until_reset // 3600)}h {int((secs_until_reset % 3600) // 60)}m."
            )

        # ── Per-model cooldown (e.g. after 429) ─────────────────────────────
        now_t = time.time()
        if now_t < self._model_rl_until.get(model, 0):
            wait_s = self._model_rl_until[model] - now_t
            raise RuntimeError(
                f"RATE_LIMIT:{wait_s}:{model}: model is in cooldown "
                f"({int(wait_s // 60)}m {int(wait_s % 60)}s remaining)"
            )

        try:
            self._record_evt(stage="model_attempt", provider="openrouter", model=model)
            content, ok = self._post_with_truncation_retry(model, system_prompt, payload_text)
            if not ok:
                self._model_rl_until[model] = time.time() + self._JSON_FAIL_COOLDOWN
                self._record_evt(
                    stage="model_json_fail", provider="openrouter", model=model,
                    message="Unparseable JSON after retry",
                    cooldown_s=self._JSON_FAIL_COOLDOWN,
                )
                raise RuntimeError(f"invalid JSON from {model}")
            self._model_rl_until.pop(model, None)
            self._active_models.add(model)
            self._record_evt(stage="model_success", provider="openrouter", model=model)
            return model, content
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
                cooldown = retry_after if retry_after > 0 else self._MODEL_RL_SECONDS
                self._model_rl_until[model] = time.time() + cooldown
                self._record_evt(
                    stage="model_rate_limited", provider="openrouter", model=model,
                    cooldown_s=cooldown, from_retry_after=bool(retry_after > 0),
                )
                raise
            if msg.startswith("MODEL_ERROR:"):
                self._model_rl_until[model] = time.time() + self._MODEL_RL_SECONDS
                raise
            raise

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
