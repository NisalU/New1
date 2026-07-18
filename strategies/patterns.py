"""Candlestick & chart pattern recognition (pure Python).

Covers every major pattern class traders use:

Single-candle:
  Doji (standard / dragonfly / gravestone / long-legged)
  Marubozu (bullish / bearish)
  Spinning top / inside bar

Two-candle:
  Bullish / Bearish engulfing
  Hammer / Shooting star (pin bar)
  Inverted hammer / Hanging man
  Piercing line / Dark cloud cover
  Bullish / Bearish harami
  Tweezer bottom / Tweezer top
  Bullish / Bearish kicker

Three-candle:
  Morning star / Evening star
  Three white soldiers / Three black crows
  Three inside up / Three inside down
  Three outside up / Three outside down
  Abandoned baby (bull / bear)
  Tasuki gap (up / down)

Five-candle:
  Rising three methods / Falling three methods
  Mat hold

Chart patterns (swing-point based):
  Double top / Double bottom
  Triple top / Triple bottom
  Head & shoulders / Inverse head & shoulders
  Ascending triangle / Descending triangle / Symmetrical triangle
  Rising wedge / Falling wedge
  Bull flag / Bear flag
  Bull pennant / Bear pennant
  Rectangle breakout
  Cup & handle / Inverted cup & handle
  Broadening formation (megaphone)
  Rounding bottom / Rounding top
"""

from .helpers import swing_points, atr, linear_regression, clamp

# ---------------------------------------------------------------------------
# Shared geometry helpers
# ---------------------------------------------------------------------------

def _body(c):
    return abs(c["close"] - c["open"])


def _range(c):
    return c["high"] - c["low"] or 1e-9


def _is_bull(c):
    return c["close"] >= c["open"]


def _is_bear(c):
    return c["close"] < c["open"]


def _upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def _lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def _body_ratio(c):
    """Body as a fraction of the full candle range (0-1)."""
    return _body(c) / _range(c)


def _midpoint(c):
    return (c["open"] + c["close"]) / 2


# ---------------------------------------------------------------------------
# 1. Single-candle patterns
# ---------------------------------------------------------------------------

def _single_candle(candles, a):
    out = []
    c = candles[-1]
    rng = _range(c)
    body = _body(c)
    uw = _upper_wick(c)
    lw = _lower_wick(c)

    # ── Doji variants ────────────────────────────────────────────────────────
    if body / rng < 0.10:
        # Dragonfly doji: tiny upper wick, long lower wick → bullish reversal at lows
        if lw > rng * 0.6 and uw < rng * 0.1:
            out.append(("dragonfly_doji", 0.35,
                        "Dragonfly doji — buyers rejected the lows, bullish reversal signal"))
        # Gravestone doji: tiny lower wick, long upper wick → bearish reversal at highs
        elif uw > rng * 0.6 and lw < rng * 0.1:
            out.append(("gravestone_doji", -0.35,
                        "Gravestone doji — sellers rejected the highs, bearish reversal signal"))
        # Long-legged doji: both wicks long → strong indecision
        elif lw > rng * 0.3 and uw > rng * 0.3:
            out.append(("long_legged_doji", 0.0,
                        "Long-legged doji — indecision, wait for directional confirmation"))
        # Standard doji: neutral indecision
        else:
            out.append(("doji", 0.0,
                        "Doji — indecision candle, watch for directional follow-through"))

    # ── Marubozu ─────────────────────────────────────────────────────────────
    # Full-body candle with virtually no wicks — pure momentum
    if body / rng > 0.90:
        if _is_bull(c):
            out.append(("bull_marubozu", 0.45,
                        "Bullish marubozu — full-body bull candle, strong buying momentum"))
        else:
            out.append(("bear_marubozu", -0.45,
                        "Bearish marubozu — full-body bear candle, strong selling momentum"))

    # ── Spinning top ─────────────────────────────────────────────────────────
    # Small body with both wicks > body → indecision / potential reversal
    if 0.10 <= body / rng <= 0.35 and lw > body and uw > body:
        out.append(("spinning_top", 0.0,
                    "Spinning top — small body with equal wicks, indecision in progress"))

    # ── Inside bar ───────────────────────────────────────────────────────────
    # Current candle range is fully inside the previous candle → compression before breakout
    prev = candles[-2]
    if c["high"] < prev["high"] and c["low"] > prev["low"]:
        out.append(("inside_bar", 0.0,
                    "Inside bar — range compression, breakout setup building"))

    return out


# ---------------------------------------------------------------------------
# 2. Two-candle patterns
# ---------------------------------------------------------------------------

def _two_candle(candles, a):
    out = []
    if len(candles) < 2:
        return out
    c1, c2 = candles[-2], candles[-1]
    body1, body2 = _body(c1), _body(c2)
    rng2 = _range(c2)

    # ── Engulfing ────────────────────────────────────────────────────────────
    if body2 > body1 * 1.1:
        if _is_bull(c2) and _is_bear(c1) and \
                c2["close"] >= c1["open"] and c2["open"] <= c1["close"]:
            out.append(("bullish_engulfing", 0.55,
                        "Bullish engulfing — buyers overwhelmed sellers, trend reversal signal"))
        if _is_bear(c2) and _is_bull(c1) and \
                c2["close"] <= c1["open"] and c2["open"] >= c1["close"]:
            out.append(("bearish_engulfing", -0.55,
                        "Bearish engulfing — sellers overwhelmed buyers, trend reversal signal"))

    # ── Hammer / Hanging man ─────────────────────────────────────────────────
    # Small body in upper third of range, long lower wick (≥ 2× body), tiny upper wick
    lw2 = _lower_wick(c2)
    uw2 = _upper_wick(c2)
    body2_r = body2 / rng2
    if body2_r < 0.35 and lw2 > body2 * 2 and lw2 > uw2 * 2:
        # Hammer: appears after a downtrend → bullish reversal
        if c1["close"] < c1["open"]:  # prior bear context
            out.append(("hammer", 0.45,
                        "Hammer — long lower wick rejecting lows after downtrend, bullish reversal"))
        # Hanging man: appears after an uptrend → bearish reversal warning
        else:
            out.append(("hanging_man", -0.35,
                        "Hanging man — hammer shape after uptrend, bearish reversal warning"))

    # ── Shooting star / Inverted hammer ──────────────────────────────────────
    if body2_r < 0.35 and uw2 > body2 * 2 and uw2 > lw2 * 2:
        if _is_bull(c1):
            out.append(("shooting_star", -0.45,
                        "Shooting star — long upper wick rejecting highs after uptrend, bearish reversal"))
        else:
            out.append(("inverted_hammer", 0.30,
                        "Inverted hammer — long upper wick after downtrend, potential bullish reversal"))

    # ── Piercing line ─────────────────────────────────────────────────────────
    # Bear candle, then bull candle opens below c1 low and closes above c1 midpoint
    if _is_bear(c1) and _is_bull(c2) and body1 > a * 0.5 and body2 > a * 0.5:
        if c2["open"] < c1["low"] and c2["close"] > _midpoint(c1) and c2["close"] < c1["open"]:
            out.append(("piercing_line", 0.45,
                        "Piercing line — bull candle recovers more than half of prior bear, reversal signal"))

    # ── Dark cloud cover ─────────────────────────────────────────────────────
    if _is_bull(c1) and _is_bear(c2) and body1 > a * 0.5 and body2 > a * 0.5:
        if c2["open"] > c1["high"] and c2["close"] < _midpoint(c1) and c2["close"] > c1["open"]:
            out.append(("dark_cloud_cover", -0.45,
                        "Dark cloud cover — bear candle erases more than half of prior bull, reversal signal"))

    # ── Harami ───────────────────────────────────────────────────────────────
    # Small c2 body contained within c1 body → momentum stall
    if body1 > a * 0.5:
        c2_hi = max(c2["open"], c2["close"])
        c2_lo = min(c2["open"], c2["close"])
        c1_hi = max(c1["open"], c1["close"])
        c1_lo = min(c1["open"], c1["close"])
        if c2_hi < c1_hi and c2_lo > c1_lo and body2 < body1 * 0.5:
            if _is_bear(c1) and _is_bull(c2):
                out.append(("bullish_harami", 0.35,
                            "Bullish harami — small bull candle inside prior bear body, momentum stalling"))
            elif _is_bull(c1) and _is_bear(c2):
                out.append(("bearish_harami", -0.35,
                            "Bearish harami — small bear candle inside prior bull body, momentum stalling"))

    # ── Tweezer top / bottom ──────────────────────────────────────────────────
    if abs(c1["high"] - c2["high"]) < a * 0.15 and _is_bull(c1) and _is_bear(c2):
        out.append(("tweezer_top", -0.40,
                    f"Tweezer top at {max(c1['high'], c2['high']):.6g} — equal highs, resistance confirmed"))
    if abs(c1["low"] - c2["low"]) < a * 0.15 and _is_bear(c1) and _is_bull(c2):
        out.append(("tweezer_bottom", 0.40,
                    f"Tweezer bottom at {min(c1['low'], c2['low']):.6g} — equal lows, support confirmed"))

    # ── Kicker ────────────────────────────────────────────────────────────────
    # Gap + full-body reversal candle — one of the strongest 2-bar patterns
    if body1 > a * 0.6 and body2 > a * 0.6:
        # Bullish kicker: bear candle followed by bull candle that gaps up above c1 open
        if _is_bear(c1) and _is_bull(c2) and c2["open"] >= c1["open"]:
            out.append(("bullish_kicker", 0.65,
                        "Bullish kicker — gap-up full reversal candle, institutional buying signal"))
        # Bearish kicker: bull candle followed by bear candle that gaps down below c1 open
        if _is_bull(c1) and _is_bear(c2) and c2["open"] <= c1["open"]:
            out.append(("bearish_kicker", -0.65,
                        "Bearish kicker — gap-down full reversal candle, institutional selling signal"))

    return out


# ---------------------------------------------------------------------------
# 3. Three-candle patterns
# ---------------------------------------------------------------------------

def _three_candle(candles, a):
    out = []
    if len(candles) < 3:
        return out
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    body1, body2, body3 = _body(c1), _body(c2), _body(c3)

    # ── Morning star ─────────────────────────────────────────────────────────
    if _is_bear(c1) and body1 > a * 0.5 and \
            body2 < body1 * 0.5 and \
            _is_bull(c3) and body3 > a * 0.5 and \
            c3["close"] > _midpoint(c1):
        out.append(("morning_star", 0.65,
                    "Morning star — small middle candle breaks bear momentum, bullish reversal confirmed"))

    # ── Evening star ─────────────────────────────────────────────────────────
    if _is_bull(c1) and body1 > a * 0.5 and \
            body2 < body1 * 0.5 and \
            _is_bear(c3) and body3 > a * 0.5 and \
            c3["close"] < _midpoint(c1):
        out.append(("evening_star", -0.65,
                    "Evening star — small middle candle breaks bull momentum, bearish reversal confirmed"))

    # ── Abandoned baby ───────────────────────────────────────────────────────
    # Like morning/evening star but with gaps on both sides of the doji
    if body2 / _range(c2) < 0.10:  # middle is a doji
        if _is_bear(c1) and _is_bull(c3) and \
                c2["high"] < c1["low"] and c2["low"] > c3["high"]:
            # gaps on both sides (rare, strong)
            out.append(("abandoned_baby_bull", 0.75,
                        "Abandoned baby (bull) — gap doji between bear and bull, strong reversal"))
        if _is_bull(c1) and _is_bear(c3) and \
                c2["low"] > c1["high"] and c2["high"] < c3["low"]:
            out.append(("abandoned_baby_bear", -0.75,
                        "Abandoned baby (bear) — gap doji between bull and bear, strong reversal"))

    # ── Three white soldiers ──────────────────────────────────────────────────
    if all(_is_bull(c) for c in (c1, c2, c3)) and \
            all(_body(c) > a * 0.4 for c in (c1, c2, c3)) and \
            c2["close"] > c1["close"] and c3["close"] > c2["close"] and \
            c2["open"] > c1["open"] and c3["open"] > c2["open"]:
        out.append(("three_white_soldiers", 0.70,
                    "Three white soldiers — three large consecutive bull candles, strong uptrend resuming"))

    # ── Three black crows ─────────────────────────────────────────────────────
    if all(_is_bear(c) for c in (c1, c2, c3)) and \
            all(_body(c) > a * 0.4 for c in (c1, c2, c3)) and \
            c2["close"] < c1["close"] and c3["close"] < c2["close"] and \
            c2["open"] < c1["open"] and c3["open"] < c2["open"]:
        out.append(("three_black_crows", -0.70,
                    "Three black crows — three large consecutive bear candles, strong downtrend resuming"))

    # ── Three inside up/down ──────────────────────────────────────────────────
    # Harami followed by confirming close
    c1_hi = max(c1["open"], c1["close"])
    c1_lo = min(c1["open"], c1["close"])
    c2_hi = max(c2["open"], c2["close"])
    c2_lo = min(c2["open"], c2["close"])
    if _is_bear(c1) and body1 > a * 0.5 and \
            c2_hi < c1_hi and c2_lo > c1_lo and _is_bull(c2) and \
            _is_bull(c3) and c3["close"] > c1_hi:
        out.append(("three_inside_up", 0.55,
                    "Three inside up — harami confirmed by close above bear body, reversal signal"))
    if _is_bull(c1) and body1 > a * 0.5 and \
            c2_hi < c1_hi and c2_lo > c1_lo and _is_bear(c2) and \
            _is_bear(c3) and c3["close"] < c1_lo:
        out.append(("three_inside_down", -0.55,
                    "Three inside down — harami confirmed by close below bull body, reversal signal"))

    # ── Three outside up/down ─────────────────────────────────────────────────
    # Engulfing followed by confirming close
    if _is_bear(c1) and _is_bull(c2) and body2 > body1 * 1.1 and \
            c2["close"] > c1["open"] and c2["open"] < c1["close"] and \
            _is_bull(c3) and c3["close"] > c2["close"]:
        out.append(("three_outside_up", 0.60,
                    "Three outside up — engulfing confirmed by third bull close, strong reversal"))
    if _is_bull(c1) and _is_bear(c2) and body2 > body1 * 1.1 and \
            c2["close"] < c1["open"] and c2["open"] > c1["close"] and \
            _is_bear(c3) and c3["close"] < c2["close"]:
        out.append(("three_outside_down", -0.60,
                    "Three outside down — engulfing confirmed by third bear close, strong reversal"))

    # ── Tasuki gap ────────────────────────────────────────────────────────────
    # Gap candle followed by partial fill candle — continuation
    if _is_bull(c1) and _is_bull(c2) and c2["open"] > c1["close"] and \
            _is_bear(c3) and c3["open"] > c2["open"] and \
            c3["close"] > c1["close"] and c3["close"] < c2["open"]:
        out.append(("upside_tasuki_gap", 0.40,
                    "Upside tasuki gap — partial gap fill, bullish continuation expected"))
    if _is_bear(c1) and _is_bear(c2) and c2["open"] < c1["close"] and \
            _is_bull(c3) and c3["open"] < c2["open"] and \
            c3["close"] < c1["close"] and c3["close"] > c2["open"]:
        out.append(("downside_tasuki_gap", -0.40,
                    "Downside tasuki gap — partial gap fill, bearish continuation expected"))

    return out


# ---------------------------------------------------------------------------
# 4. Five-candle patterns
# ---------------------------------------------------------------------------

def _five_candle(candles, a):
    out = []
    if len(candles) < 5:
        return out
    c1, c2, c3, c4, c5 = candles[-5], candles[-4], candles[-3], candles[-2], candles[-1]

    # ── Rising three methods ──────────────────────────────────────────────────
    # Large bull candle, 3 small bear candles staying inside c1, then large bull breakout
    middles = (c2, c3, c4)
    if _is_bull(c1) and _body(c1) > a * 0.6 and \
            all(_is_bear(c) for c in middles) and \
            all(c["close"] > c1["open"] for c in middles) and \
            all(c["high"] < c1["high"] for c in middles) and \
            _is_bull(c5) and c5["close"] > c1["close"]:
        out.append(("rising_three_methods", 0.55,
                    "Rising three methods — three-bar pullback inside bull candle, bullish continuation"))

    # ── Falling three methods ─────────────────────────────────────────────────
    if _is_bear(c1) and _body(c1) > a * 0.6 and \
            all(_is_bull(c) for c in middles) and \
            all(c["close"] < c1["open"] for c in middles) and \
            all(c["low"] > c1["low"] for c in middles) and \
            _is_bear(c5) and c5["close"] < c1["close"]:
        out.append(("falling_three_methods", -0.55,
                    "Falling three methods — three-bar pullback inside bear candle, bearish continuation"))

    # ── Mat hold ──────────────────────────────────────────────────────────────
    # Like rising three methods but c2 gaps up
    if _is_bull(c1) and _body(c1) > a * 0.6 and \
            c2["open"] > c1["close"] and \
            all(_is_bear(c) for c in (c2, c3, c4)) and \
            all(c["low"] > c1["open"] for c in (c2, c3, c4)) and \
            _is_bull(c5) and c5["close"] > c2["open"]:
        out.append(("mat_hold_bull", 0.50,
                    "Bull mat hold — gap-up pullback fully absorbed, continuation higher expected"))

    return out


# ---------------------------------------------------------------------------
# 5. Double / Triple top & bottom
# ---------------------------------------------------------------------------

def _multi_top_bottom(candles, a):
    out = []
    highs, lows = swing_points(candles, lookback=3)
    price = candles[-1]["close"]

    # ── Double top / Double bottom ────────────────────────────────────────────
    if len(highs) >= 2:
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        if abs(p1 - p2) < a * 0.7 and i2 - i1 >= 5 and price < min(p1, p2):
            out.append(("double_top", -0.60,
                        f"Double top at {max(p1, p2):.6g} — equal highs rejected, bearish reversal"))
    if len(lows) >= 2:
        (i1, p1), (i2, p2) = lows[-2], lows[-1]
        if abs(p1 - p2) < a * 0.7 and i2 - i1 >= 5 and price > max(p1, p2):
            out.append(("double_bottom", 0.60,
                        f"Double bottom at {min(p1, p2):.6g} — equal lows defended, bullish reversal"))

    # ── Triple top / Triple bottom ────────────────────────────────────────────
    if len(highs) >= 3:
        (i1, p1), (i2, p2), (i3, p3) = highs[-3], highs[-2], highs[-1]
        spread = max(p1, p2, p3) - min(p1, p2, p3)
        if spread < a * 0.9 and i2 - i1 >= 4 and i3 - i2 >= 4 and price < min(p1, p2, p3):
            out.append(("triple_top", -0.70,
                        f"Triple top at {max(p1, p2, p3):.6g} — three failed breakout attempts, strong resistance"))
    if len(lows) >= 3:
        (i1, p1), (i2, p2), (i3, p3) = lows[-3], lows[-2], lows[-1]
        spread = max(p1, p2, p3) - min(p1, p2, p3)
        if spread < a * 0.9 and i2 - i1 >= 4 and i3 - i2 >= 4 and price > max(p1, p2, p3):
            out.append(("triple_bottom", 0.70,
                        f"Triple bottom at {min(p1, p2, p3):.6g} — three defended lows, strong support"))

    return out


# ---------------------------------------------------------------------------
# 6. Head & Shoulders
# ---------------------------------------------------------------------------

def _head_shoulders(candles, a):
    out = []
    highs, lows = swing_points(candles, lookback=3)

    # ── Head & Shoulders (bearish) ────────────────────────────────────────────
    if len(highs) >= 3:
        (i1, l), (i2, h), (i3, r) = highs[-3], highs[-2], highs[-1]
        neckline_tol = a * 1.5
        if h > l + a * 0.5 and h > r + a * 0.5 and abs(l - r) < neckline_tol:
            price = candles[-1]["close"]
            neckline = (l + r) / 2  # approximate
            if price < neckline:
                out.append(("head_shoulders_break", -0.65,
                            f"Head & shoulders — neckline at {neckline:.6g} broken, measured move down"))
            else:
                out.append(("head_shoulders_forming", -0.40,
                            f"Head & shoulders forming — watching neckline at {neckline:.6g}"))

    # ── Inverse Head & Shoulders (bullish) ────────────────────────────────────
    if len(lows) >= 3:
        (i1, l), (i2, h), (i3, r) = lows[-3], lows[-2], lows[-1]
        neckline_tol = a * 1.5
        if h < l - a * 0.5 and h < r - a * 0.5 and abs(l - r) < neckline_tol:
            price = candles[-1]["close"]
            neckline = (l + r) / 2
            if price > neckline:
                out.append(("inv_head_shoulders_break", 0.65,
                            f"Inv. head & shoulders — neckline at {neckline:.6g} broken, measured move up"))
            else:
                out.append(("inv_head_shoulders_forming", 0.40,
                            f"Inv. head & shoulders forming — watching neckline at {neckline:.6g}"))

    return out


# ---------------------------------------------------------------------------
# 7. Triangle patterns
# ---------------------------------------------------------------------------

def _triangles(candles, a):
    out = []
    if len(candles) < 20:
        return out

    highs, lows = swing_points(candles, lookback=3)
    if len(highs) < 2 or len(lows) < 2:
        return out

    price = candles[-1]["close"]
    n = len(candles)

    # Use most recent 2 swing highs and lows
    (hi1_i, hi1_p), (hi2_i, hi2_p) = highs[-2], highs[-1]
    (lo1_i, lo1_p), (lo2_i, lo2_p) = lows[-2], lows[-1]

    # Linear slopes of highs and lows
    high_slope = (hi2_p - hi1_p) / max(hi2_i - hi1_i, 1)
    low_slope  = (lo2_p - lo1_p) / max(lo2_i - lo1_i, 1)

    # Project each trendline to the current candle
    proj_high = hi2_p + high_slope * (n - 1 - hi2_i)
    proj_low  = lo2_p + low_slope  * (n - 1 - lo2_i)
    compression = proj_high - proj_low

    # Only act near apex (lines close together)
    if compression < a * 3 and compression > 0:
        # ── Ascending triangle (flat top, rising bottom) ────────────────────
        if abs(high_slope) < a * 0.005 and low_slope > a * 0.002:
            if price > proj_high:
                out.append(("ascending_triangle_break", 0.60,
                            f"Ascending triangle breakout above {proj_high:.6g} — bullish continuation"))
            else:
                out.append(("ascending_triangle", 0.25,
                            f"Ascending triangle — flat resistance at {proj_high:.6g}, coiling for breakout"))

        # ── Descending triangle (falling top, flat bottom) ──────────────────
        elif abs(low_slope) < a * 0.005 and high_slope < -a * 0.002:
            if price < proj_low:
                out.append(("descending_triangle_break", -0.60,
                            f"Descending triangle breakdown below {proj_low:.6g} — bearish continuation"))
            else:
                out.append(("descending_triangle", -0.25,
                            f"Descending triangle — flat support at {proj_low:.6g}, coiling for breakdown"))

        # ── Symmetrical triangle (converging highs and lows) ────────────────
        elif high_slope < -a * 0.001 and low_slope > a * 0.001:
            if price > proj_high:
                out.append(("symmetrical_triangle_bull", 0.45,
                            f"Symmetrical triangle bullish breakout above {proj_high:.6g}"))
            elif price < proj_low:
                out.append(("symmetrical_triangle_bear", -0.45,
                            f"Symmetrical triangle bearish breakdown below {proj_low:.6g}"))
            else:
                out.append(("symmetrical_triangle", 0.0,
                            "Symmetrical triangle — apex coiling, watch for directional breakout"))

    # ── Rising wedge (both trendlines rising, highs steeper) ─────────────────
    if high_slope > a * 0.001 and low_slope > a * 0.001 and low_slope > high_slope * 1.1:
        if price < proj_low:
            out.append(("rising_wedge_break", -0.55,
                        f"Rising wedge broken — bearish reversal, support at {proj_low:.6g} lost"))
        else:
            out.append(("rising_wedge", -0.30,
                        "Rising wedge — upward channel narrowing, bearish reversal pattern forming"))

    # ── Falling wedge (both trendlines falling, lows steeper) ────────────────
    if high_slope < -a * 0.001 and low_slope < -a * 0.001 and high_slope > low_slope * 1.1:
        if price > proj_high:
            out.append(("falling_wedge_break", 0.55,
                        f"Falling wedge broken — bullish reversal, resistance at {proj_high:.6g} cleared"))
        else:
            out.append(("falling_wedge", 0.30,
                        "Falling wedge — downward channel narrowing, bullish reversal pattern forming"))

    return out


# ---------------------------------------------------------------------------
# 8. Flag & Pennant
# ---------------------------------------------------------------------------

def _flags_pennants(candles, a):
    """Bull/bear flag and pennant — impulse followed by tight consolidation."""
    out = []
    if len(candles) < 15:
        return out

    price = candles[-1]["close"]

    # Measure the impulse: look for a strong directional move in the last 15 candles
    # then a consolidation in the final 5
    impulse_window = candles[-15:-5]
    consol_window  = candles[-5:]

    impulse_high = max(c["high"] for c in impulse_window)
    impulse_low  = min(c["low"]  for c in impulse_window)
    impulse_move = impulse_high - impulse_low

    consol_high = max(c["high"] for c in consol_window)
    consol_low  = min(c["low"]  for c in consol_window)
    consol_rng  = consol_high - consol_low

    # Consolidation must be tight (< 40% of the impulse)
    if impulse_move < a * 2 or consol_rng > impulse_move * 0.40:
        return out

    # Bullish impulse: impulse closes in upper half
    impulse_close = impulse_window[-1]["close"]
    impulse_open  = impulse_window[0]["open"]

    if impulse_close > impulse_open + impulse_move * 0.55:  # bull impulse
        # Flag: rectangular consolidation (parallel channels sloping slightly down)
        consol_xs = list(range(len(consol_window)))
        hi_slope, _ = linear_regression(consol_xs, [c["high"] for c in consol_window])
        lo_slope, _ = linear_regression(consol_xs, [c["low"]  for c in consol_window])
        slope_diff  = abs(hi_slope - lo_slope)

        if slope_diff < a * 0.005:  # parallel → flag
            if price > consol_high:
                out.append(("bull_flag_break", 0.65,
                            f"Bull flag breakout above {consol_high:.6g} — continuation of prior impulse"))
            else:
                out.append(("bull_flag", 0.35,
                            f"Bull flag forming — tight consolidation after impulse, {consol_high:.6g} is breakout level"))
        elif hi_slope < 0 and lo_slope > 0:  # converging → pennant
            if price > consol_high:
                out.append(("bull_pennant_break", 0.65,
                            f"Bull pennant breakout above {consol_high:.6g}"))
            else:
                out.append(("bull_pennant", 0.35,
                            "Bull pennant — coiling after impulse, upside continuation expected"))

    elif impulse_close < impulse_open - impulse_move * 0.55:  # bear impulse
        consol_xs = list(range(len(consol_window)))
        hi_slope, _ = linear_regression(consol_xs, [c["high"] for c in consol_window])
        lo_slope, _ = linear_regression(consol_xs, [c["low"]  for c in consol_window])
        slope_diff  = abs(hi_slope - lo_slope)

        if slope_diff < a * 0.005:
            if price < consol_low:
                out.append(("bear_flag_break", -0.65,
                            f"Bear flag breakdown below {consol_low:.6g} — continuation of prior drop"))
            else:
                out.append(("bear_flag", -0.35,
                            f"Bear flag forming — tight consolidation after drop, {consol_low:.6g} is breakdown level"))
        elif hi_slope < 0 and lo_slope > 0:
            if price < consol_low:
                out.append(("bear_pennant_break", -0.65,
                            f"Bear pennant breakdown below {consol_low:.6g}"))
            else:
                out.append(("bear_pennant", -0.35,
                            "Bear pennant — coiling after drop, downside continuation expected"))

    return out


# ---------------------------------------------------------------------------
# 9. Rectangle (range breakout)
# ---------------------------------------------------------------------------

def _rectangle(candles, a):
    """Price consolidating in a flat range then breaking."""
    out = []
    if len(candles) < 20:
        return out

    window = candles[-20:]
    hi = max(c["high"] for c in window[:-2])
    lo = min(c["low"]  for c in window[:-2])
    rng = hi - lo
    price = candles[-1]["close"]

    # Only call it a rectangle if the range is between 1 and 5 ATR
    if rng < a * 1.0 or rng > a * 5.0:
        return out

    # Most candles (≥ 75%) must have their close inside the range
    inside = sum(1 for c in window[:-2] if lo <= c["close"] <= hi)
    if inside < len(window[:-2]) * 0.75:
        return out

    if price > hi + a * 0.1:
        out.append(("rectangle_bull_break", 0.55,
                    f"Rectangle breakout above {hi:.6g} — range resolved bullish after consolidation"))
    elif price < lo - a * 0.1:
        out.append(("rectangle_bear_break", -0.55,
                    f"Rectangle breakdown below {lo:.6g} — range resolved bearish after consolidation"))

    return out


# ---------------------------------------------------------------------------
# 10. Cup & Handle
# ---------------------------------------------------------------------------

def _cup_handle(candles, a):
    """Cup-and-handle / inverse cup-and-handle detected on the last 60 candles."""
    out = []
    if len(candles) < 40:
        return out

    # Use a rolling window; split into left rim, cup bottom, right rim, handle
    w = min(60, len(candles))
    window = candles[-w:]
    n = len(window)
    third = n // 3

    left_high  = max(c["high"] for c in window[:third])
    mid_low    = min(c["low"]  for c in window[third:2*third])
    right_high = max(c["high"] for c in window[2*third:-5]) if n > 2*third + 5 else 0
    handle     = window[-5:]
    handle_low = min(c["low"]  for c in handle)
    price      = candles[-1]["close"]

    cup_depth = left_high - mid_low

    # Cup: left and right rims roughly equal height, depth > 1 ATR
    if cup_depth > a * 1.5 and right_high > 0 and \
            abs(left_high - right_high) < cup_depth * 0.35:
        handle_retrace = (right_high - handle_low) / cup_depth
        # Handle retraces 20-50% of cup depth
        if 0.10 <= handle_retrace <= 0.55:
            if price > right_high:
                out.append(("cup_handle_break", 0.65,
                            f"Cup & handle breakout above {right_high:.6g} — measured move = {cup_depth:.6g}"))
            else:
                out.append(("cup_handle", 0.35,
                            f"Cup & handle forming — watch breakout above {right_high:.6g}"))

    # ── Inverted cup & handle (bearish) ──────────────────────────────────────
    left_low   = min(c["low"]  for c in window[:third])
    mid_high   = max(c["high"] for c in window[third:2*third])
    right_low  = min(c["low"]  for c in window[2*third:-5]) if n > 2*third + 5 else 0
    handle_high = max(c["high"] for c in handle)

    inv_depth = mid_high - left_low
    if inv_depth > a * 1.5 and right_low > 0 and \
            abs(left_low - right_low) < inv_depth * 0.35:
        handle_retrace = (handle_high - right_low) / inv_depth
        if 0.10 <= handle_retrace <= 0.55:
            if price < right_low:
                out.append(("inv_cup_handle_break", -0.65,
                            f"Inv. cup & handle breakdown below {right_low:.6g}"))
            else:
                out.append(("inv_cup_handle", -0.35,
                            f"Inv. cup & handle forming — watch breakdown below {right_low:.6g}"))

    return out


# ---------------------------------------------------------------------------
# 11. Rounding bottom / Rounding top
# ---------------------------------------------------------------------------

def _rounding(candles, a):
    """Rounding bottom (saucer) and rounding top detected via parabolic curve fit."""
    out = []
    if len(candles) < 30:
        return out

    w = min(50, len(candles))
    window = candles[-w:]
    n = len(window)
    xs = list(range(n))

    # Fit a quadratic to the close prices
    # Simplified: compare left-third avg, mid-third avg, right-third avg
    third = n // 3
    left_avg  = sum(c["close"] for c in window[:third]) / third
    mid_avg   = sum(c["close"] for c in window[third:2*third]) / third
    right_avg = sum(c["close"] for c in window[2*third:]) / (n - 2*third)
    price     = candles[-1]["close"]

    curve_depth = min(left_avg, right_avg) - mid_avg
    curve_top   = mid_avg - min(left_avg, right_avg)

    # Rounding bottom: mid lower than both ends, now rising
    if curve_depth > a * 1.5 and right_avg > mid_avg and price > right_avg:
        out.append(("rounding_bottom", 0.50,
                    "Rounding bottom (saucer) — gradual curve reversal from lows, momentum building"))

    # Rounding top: mid higher than both ends, now declining
    if curve_top > a * 1.5 and right_avg < mid_avg and price < right_avg:
        out.append(("rounding_top", -0.50,
                    "Rounding top — gradual curve reversal from highs, distribution complete"))

    return out


# ---------------------------------------------------------------------------
# 12. Broadening formation (megaphone)
# ---------------------------------------------------------------------------

def _broadening(candles, a):
    """Expanding highs and lows — distribution / volatility expansion."""
    out = []
    highs, lows = swing_points(candles, lookback=3)
    if len(highs) < 3 or len(lows) < 3:
        return out

    # Check if the last 3 swing highs are each higher than the previous
    h1, h2, h3 = highs[-3][1], highs[-2][1], highs[-1][1]
    l1, l2, l3 = lows[-3][1],  lows[-2][1],  lows[-1][1]

    if h3 > h2 > h1 and l3 < l2 < l1 and (h3 - l3) > a * 3:
        price = candles[-1]["close"]
        mid   = (h3 + l3) / 2
        if price < mid:
            out.append(("broadening_bear", -0.45,
                        f"Broadening formation (megaphone) — expanding range, price below midpoint {mid:.6g}, bearish bias"))
        else:
            out.append(("broadening_bull", 0.35,
                        f"Broadening formation — expanding range, price above midpoint {mid:.6g}"))

    return out


# ---------------------------------------------------------------------------
# 13. Three drives (harmonic exhaustion)
# ---------------------------------------------------------------------------

def _three_drives(candles, a):
    """Three equal-height pushes into a level — exhaustion and reversal."""
    out = []
    highs, lows = swing_points(candles, lookback=3)

    if len(highs) >= 3:
        h1, h2, h3 = highs[-3][1], highs[-2][1], highs[-1][1]
        # Each drive roughly equal height within 1.5 ATR
        if abs(h1 - h2) < a * 1.2 and abs(h2 - h3) < a * 1.2 and \
                all(h > candles[-1]["close"] for h in (h1, h2, h3)):
            out.append(("three_drives_top", -0.50,
                        f"Three drives top at {h3:.6g} — three equal pushes exhausted, reversal signal"))

    if len(lows) >= 3:
        l1, l2, l3 = lows[-3][1], lows[-2][1], lows[-1][1]
        if abs(l1 - l2) < a * 1.2 and abs(l2 - l3) < a * 1.2 and \
                all(l < candles[-1]["close"] for l in (l1, l2, l3)):
            out.append(("three_drives_bottom", 0.50,
                        f"Three drives bottom at {l3:.6g} — three equal tests exhausted, reversal signal"))

    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze(candles):
    if len(candles) < 5:
        return {"score": 0, "reasons": ["Not enough candles for pattern analysis"], "overlays": {}}

    a = atr(candles) or candles[-1]["close"] * 0.005

    found = (
        _single_candle(candles, a)
        + _two_candle(candles, a)
        + _three_candle(candles, a)
        + _five_candle(candles, a)
        + _multi_top_bottom(candles, a)
        + _head_shoulders(candles, a)
        + _triangles(candles, a)
        + _flags_pennants(candles, a)
        + _rectangle(candles, a)
        + _cup_handle(candles, a)
        + _rounding(candles, a)
        + _broadening(candles, a)
        + _three_drives(candles, a)
    )

    # Combine scores; clamp to [-1, +1]
    score = clamp(sum(s for _, s, _ in found))

    reasons  = [msg for _, _, msg in found] or ["No notable chart patterns detected"]
    overlays = {
        "patterns": [
            {
                "name":      name,
                "direction": "bull" if s > 0 else ("bear" if s < 0 else "neutral"),
                "score":     round(s, 3),
            }
            for name, s, _ in found
        ]
    }
    return {"score": score, "reasons": reasons, "overlays": overlays}
