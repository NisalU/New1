"""Support & resistance zones from clustered swing points."""
from .helpers import swing_points, cluster_levels, atr, clamp


def analyze(candles):
    highs, lows = swing_points(candles, lookback=3)
    a = atr(candles) or (candles[-1]["close"] * 0.005)
    tol = a * 0.8

    res_levels = [lv for lv in cluster_levels(highs, tol) if lv["touches"] >= 2][:4]
    sup_levels = [lv for lv in cluster_levels(lows, tol) if lv["touches"] >= 2][:4]

    price = candles[-1]["close"]
    score = 0.0
    reasons = []

    # Nearest support below and resistance above
    supports_below = sorted([lv for lv in sup_levels if lv["price"] < price],
                            key=lambda x: price - x["price"])
    resistance_above = sorted([lv for lv in res_levels if lv["price"] > price],
                              key=lambda x: x["price"] - price)

    if supports_below:
        s = supports_below[0]
        dist = (price - s["price"]) / a
        if dist < 1.5:
            score += 0.5 + min(0.1 * s["touches"], 0.4)
            reasons.append(f"Price near support {s['price']:.6g} ({s['touches']} touches)")
    if resistance_above:
        r = resistance_above[0]
        dist = (r["price"] - price) / a
        if dist < 1.5:
            score -= 0.5 + min(0.1 * r["touches"], 0.4)
            reasons.append(f"Price near resistance {r['price']:.6g} ({r['touches']} touches)")

    # Breakout: closed above a resistance level in the last 2 candles
    for lv in res_levels:
        if candles[-2]["close"] <= lv["price"] < candles[-1]["close"]:
            score += 0.4
            reasons.append(f"Breakout above resistance {lv['price']:.6g}")
    for lv in sup_levels:
        if candles[-2]["close"] >= lv["price"] > candles[-1]["close"]:
            score -= 0.4
            reasons.append(f"Breakdown below support {lv['price']:.6g}")

    overlays = {
        "support": [{"price": lv["price"], "touches": lv["touches"]} for lv in sup_levels],
        "resistance": [{"price": lv["price"], "touches": lv["touches"]} for lv in res_levels],
    }
    if not reasons:
        reasons.append("Price between S/R zones, no edge")
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
