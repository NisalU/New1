"""Smart Money Concepts: BOS/CHoCH, order blocks, fair value gaps."""
from .helpers import swing_points, atr, clamp


def _structure(candles):
    """Detect Break of Structure / Change of Character from swing sequence."""
    highs, lows = swing_points(candles, lookback=3)
    events = []
    price = candles[-1]["close"]

    # Determine prior trend from last swings
    trend = 0
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            trend = 1
        elif lh and ll:
            trend = -1

    # BOS: close beyond most recent swing extreme in trend direction
    # CHoCH: close beyond most recent swing extreme AGAINST prior trend
    if highs and price > highs[-1][1]:
        events.append(("CHoCH" if trend == -1 else "BOS", 1,
                       f"{'CHoCH' if trend == -1 else 'BOS'} above swing high {highs[-1][1]:.6g}"))
    if lows and price < lows[-1][1]:
        events.append(("CHoCH" if trend == 1 else "BOS", -1,
                       f"{'CHoCH' if trend == 1 else 'BOS'} below swing low {lows[-1][1]:.6g}"))
    return trend, events


def _order_blocks(candles, a):
    """Last opposite-color candle before a strong impulsive move."""
    obs = []
    for i in range(len(candles) - 40, len(candles) - 2):
        if i < 1:
            continue
        c = candles[i]
        move = candles[i + 1]["close"] - c["close"]
        body = abs(c["close"] - c["open"])
        # bullish OB: bearish candle followed by impulse up > 1.5 ATR
        if c["close"] < c["open"] and move > a * 1.5 and body > 0:
            obs.append({"type": "bullish", "top": c["high"], "bottom": c["low"],
                        "time": c["time"]})
        if c["close"] > c["open"] and move < -a * 1.5 and body > 0:
            obs.append({"type": "bearish", "top": c["high"], "bottom": c["low"],
                        "time": c["time"]})
    # keep unmitigated (price hasn't fully traded through) - last 3 of each
    price = candles[-1]["close"]
    valid = [ob for ob in obs
             if (ob["type"] == "bullish" and price > ob["bottom"]) or
                (ob["type"] == "bearish" and price < ob["top"])]
    return valid[-4:]


def _fair_value_gaps(candles):
    """3-candle imbalance: candle1.high < candle3.low (bullish FVG) or reverse."""
    fvgs = []
    for i in range(len(candles) - 30, len(candles) - 2):
        if i < 0:
            continue
        c1, c3 = candles[i], candles[i + 2]
        if c1["high"] < c3["low"]:
            fvgs.append({"type": "bullish", "top": c3["low"], "bottom": c1["high"],
                         "time": candles[i + 1]["time"]})
        if c1["low"] > c3["high"]:
            fvgs.append({"type": "bearish", "top": c1["low"], "bottom": c3["high"],
                         "time": candles[i + 1]["time"]})
    # unfilled only
    price = candles[-1]["close"]
    open_fvgs = []
    for f in fvgs:
        mid = (f["top"] + f["bottom"]) / 2
        if f["type"] == "bullish" and price > f["bottom"]:
            open_fvgs.append({**f, "mid": mid})
        elif f["type"] == "bearish" and price < f["top"]:
            open_fvgs.append({**f, "mid": mid})
    return open_fvgs[-4:]


def analyze(candles):
    a = atr(candles) or (candles[-1]["close"] * 0.005)
    price = candles[-1]["close"]
    trend, events = _structure(candles)
    obs = _order_blocks(candles, a)
    fvgs = _fair_value_gaps(candles)

    score = 0.0
    reasons = []

    for name, direction, msg in events:
        score += direction * (0.6 if name == "CHoCH" else 0.45)
        reasons.append(msg)

    if trend == 1 and not events:
        score += 0.25
        reasons.append("Market structure bullish (HH + HL)")
    elif trend == -1 and not events:
        score -= 0.25
        reasons.append("Market structure bearish (LH + LL)")

    # Price inside an order block or FVG -> expected reaction zone
    for ob in obs:
        if ob["bottom"] - a * 0.2 <= price <= ob["top"] + a * 0.2:
            if ob["type"] == "bullish":
                score += 0.4
                reasons.append(f"Price in bullish order block {ob['bottom']:.6g}-{ob['top']:.6g}")
            else:
                score -= 0.4
                reasons.append(f"Price in bearish order block {ob['bottom']:.6g}-{ob['top']:.6g}")
    for f in fvgs:
        if f["bottom"] <= price <= f["top"]:
            if f["type"] == "bullish":
                score += 0.3
                reasons.append("Price filling bullish FVG (demand imbalance)")
            else:
                score -= 0.3
                reasons.append("Price filling bearish FVG (supply imbalance)")

    overlays = {"order_blocks": obs, "fvgs": fvgs,
                "structure": {"trend": trend,
                              "events": [{"name": n, "direction": d} for n, d, _ in events]}}
    if not reasons:
        reasons.append("No SMC setup in play")
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
