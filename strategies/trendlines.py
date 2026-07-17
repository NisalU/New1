"""Trendline detection via regression on swing highs/lows."""
from .helpers import swing_points, linear_regression, atr, clamp


def _fit_line(points, candles):
    """Fit a line through swing points; return dict with endpoints + slope, or None."""
    if len(points) < 3:
        return None
    pts = points[-5:]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    slope, intercept = linear_regression(xs, ys)
    # Reject bad fits: average residual must be small vs ATR
    a = atr(candles) or 1e-9
    resid = sum(abs(ys[i] - (slope * xs[i] + intercept)) for i in range(len(xs))) / len(xs)
    if resid > a * 1.2:
        return None
    i0, i1 = xs[0], len(candles) - 1
    return {
        "slope": slope,
        "start": {"time": candles[i0]["time"], "price": slope * i0 + intercept},
        "end": {"time": candles[i1]["time"], "price": slope * i1 + intercept},
        "value_now": slope * i1 + intercept,
    }


def analyze(candles):
    highs, lows = swing_points(candles, lookback=3)
    upper = _fit_line(highs, candles)   # descending resistance / channel top
    lower = _fit_line(lows, candles)    # ascending support / channel bottom

    price = candles[-1]["close"]
    a = atr(candles) or (price * 0.005)
    score = 0.0
    reasons = []

    if lower:
        if lower["slope"] > 0:
            score += 0.3
            reasons.append("Ascending trendline support (uptrend structure)")
        dist = (price - lower["value_now"]) / a
        if 0 <= dist < 1.0:
            score += 0.4
            reasons.append("Price bouncing on trendline support")
        elif dist < 0:
            score -= 0.5
            reasons.append("Price broke below trendline support")
    if upper:
        if upper["slope"] < 0:
            score -= 0.3
            reasons.append("Descending trendline resistance (downtrend structure)")
        dist = (upper["value_now"] - price) / a
        if 0 <= dist < 1.0:
            score -= 0.4
            reasons.append("Price rejecting at trendline resistance")
        elif dist < 0:
            score += 0.5
            reasons.append("Price broke above trendline resistance")

    overlays = {"trendlines": []}
    if upper:
        overlays["trendlines"].append({"type": "resistance", **{k: upper[k] for k in ("start", "end")}})
    if lower:
        overlays["trendlines"].append({"type": "support", **{k: lower[k] for k in ("start", "end")}})

    if not reasons:
        reasons.append("No clean trendline structure")
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
