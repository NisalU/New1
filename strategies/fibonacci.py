"""Fibonacci retracement of the dominant recent swing."""
from .helpers import swing_points, atr, clamp

FIB_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786]


def analyze(candles):
    highs, lows = swing_points(candles, lookback=3)
    if not highs or not lows:
        return {"score": 0, "reasons": ["No swing structure for fib"], "overlays": {}}

    hi_i, hi_p = max(highs[-6:], key=lambda x: x[1])
    lo_i, lo_p = min(lows[-6:], key=lambda x: x[1])
    price = candles[-1]["close"]
    rng = hi_p - lo_p
    if rng <= 0:
        return {"score": 0, "reasons": ["Flat range"], "overlays": {}}

    up_leg = lo_i < hi_i  # swing low happened first -> impulse up, retracement down
    levels = []
    for r in FIB_RATIOS:
        lvl = hi_p - rng * r if up_leg else lo_p + rng * r
        levels.append({"ratio": r, "price": lvl})

    a = atr(candles) or (price * 0.005)
    score = 0.0
    reasons = []
    for lv in levels:
        if abs(price - lv["price"]) < a * 0.8:
            if lv["ratio"] in (0.5, 0.618):
                mag = 0.7
            elif lv["ratio"] == 0.786:
                mag = 0.5
            else:
                mag = 0.35
            if up_leg:
                score += mag
                reasons.append(f"Price at {lv['ratio']} fib retracement of up-leg (buy zone)")
            else:
                score -= mag
                reasons.append(f"Price at {lv['ratio']} fib retracement of down-leg (sell zone)")
            break

    # Deep retracement beyond 0.786 weakens the impulse thesis
    if up_leg and price < hi_p - rng * 0.9:
        score -= 0.3
        reasons.append("Retracement > 0.9, up-leg likely invalidated")
    if not up_leg and price > lo_p + rng * 0.9:
        score += 0.3
        reasons.append("Retracement > 0.9, down-leg likely invalidated")

    overlays = {
        "fibonacci": {
            "direction": "up" if up_leg else "down",
            "swing_high": hi_p,
            "swing_low": lo_p,
            "levels": levels,
        }
    }
    if not reasons:
        reasons.append("Price not at a fib level")
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
