"""Candlestick & chart pattern recognition (pure Python)."""
from .helpers import swing_points, atr, clamp


def _body(c):
    return abs(c["close"] - c["open"])


def _candle_patterns(candles):
    """Detect patterns on the last closed candles."""
    out = []
    c1, c2 = candles[-2], candles[-1]
    rng2 = c2["high"] - c2["low"] or 1e-9

    # Engulfing
    if _body(c2) > _body(c1) * 1.1:
        if c2["close"] > c2["open"] and c1["close"] < c1["open"] and \
           c2["close"] >= c1["open"] and c2["open"] <= c1["close"]:
            out.append(("bullish_engulfing", 0.5, "Bullish engulfing candle"))
        if c2["close"] < c2["open"] and c1["close"] > c1["open"] and \
           c2["close"] <= c1["open"] and c2["open"] >= c1["close"]:
            out.append(("bearish_engulfing", -0.5, "Bearish engulfing candle"))

    # Pin bar / hammer / shooting star
    body = _body(c2)
    lower_wick = min(c2["open"], c2["close"]) - c2["low"]
    upper_wick = c2["high"] - max(c2["open"], c2["close"])
    if body / rng2 < 0.35:
        if lower_wick > body * 2 and lower_wick > upper_wick * 2:
            out.append(("hammer", 0.4, "Hammer / bullish pin bar (rejection of lows)"))
        if upper_wick > body * 2 and upper_wick > lower_wick * 2:
            out.append(("shooting_star", -0.4, "Shooting star / bearish pin bar (rejection of highs)"))
    return out


def _double_top_bottom(candles):
    """Double top/bottom from the last swing points."""
    out = []
    highs, lows = swing_points(candles, lookback=3)
    a = atr(candles) or 1e-9
    price = candles[-1]["close"]

    if len(highs) >= 2:
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        if abs(p1 - p2) < a * 0.7 and i2 - i1 >= 5 and price < min(p1, p2):
            out.append(("double_top", -0.6, f"Double top at {max(p1, p2):.6g}"))
    if len(lows) >= 2:
        (i1, p1), (i2, p2) = lows[-2], lows[-1]
        if abs(p1 - p2) < a * 0.7 and i2 - i1 >= 5 and price > max(p1, p2):
            out.append(("double_bottom", 0.6, f"Double bottom at {min(p1, p2):.6g}"))
    return out


def _head_shoulders(candles):
    out = []
    highs, lows = swing_points(candles, lookback=3)
    a = atr(candles) or 1e-9
    if len(highs) >= 3:
        (_, l), (_, h), (_, r) = highs[-3], highs[-2], highs[-1]
        if h > l + a * 0.5 and h > r + a * 0.5 and abs(l - r) < a * 1.2:
            out.append(("head_shoulders", -0.5, "Head & shoulders forming"))
    if len(lows) >= 3:
        (_, l), (_, h), (_, r) = lows[-3], lows[-2], lows[-1]
        if h < l - a * 0.5 and h < r - a * 0.5 and abs(l - r) < a * 1.2:
            out.append(("inv_head_shoulders", 0.5, "Inverse head & shoulders forming"))
    return out


def analyze(candles):
    found = _candle_patterns(candles) + _double_top_bottom(candles) + _head_shoulders(candles)
    score = clamp(sum(s for _, s, _ in found))
    reasons = [msg for _, _, msg in found] or ["No notable patterns"]
    overlays = {"patterns": [{"name": n, "direction": "bull" if s > 0 else "bear"} for n, s, _ in found]}
    return {"score": score, "reasons": reasons, "overlays": overlays}
