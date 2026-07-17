"""Liquidity sweeps: stop hunts through equal highs/lows that reverse."""
from .helpers import swing_points, cluster_levels, atr, clamp


def analyze(candles):
    highs, lows = swing_points(candles, lookback=3)
    a = atr(candles) or (candles[-1]["close"] * 0.005)
    tol = a * 0.4

    eq_highs = [lv for lv in cluster_levels(highs, tol) if lv["touches"] >= 2]
    eq_lows = [lv for lv in cluster_levels(lows, tol) if lv["touches"] >= 2]

    score = 0.0
    reasons = []
    sweeps = []

    # A sweep: wick pierces the level but candle closes back on the other side
    for c in candles[-5:]:
        for lv in eq_lows:
            if c["low"] < lv["price"] - tol * 0.2 and c["close"] > lv["price"]:
                score += 0.7
                reasons.append(f"Bullish liquidity sweep below equal lows {lv['price']:.6g}")
                sweeps.append({"type": "bullish", "price": lv["price"], "time": c["time"]})
        for lv in eq_highs:
            if c["high"] > lv["price"] + tol * 0.2 and c["close"] < lv["price"]:
                score -= 0.7
                reasons.append(f"Bearish liquidity sweep above equal highs {lv['price']:.6g}")
                sweeps.append({"type": "bearish", "price": lv["price"], "time": c["time"]})

    # Resting liquidity pools (magnets) — informational overlay
    pools = (
        [{"type": "buy_side", "price": lv["price"], "touches": lv["touches"]} for lv in eq_highs[:3]] +
        [{"type": "sell_side", "price": lv["price"], "touches": lv["touches"]} for lv in eq_lows[:3]]
    )

    overlays = {"sweeps": sweeps[-3:], "liquidity_pools": pools}
    if not reasons:
        reasons.append("No recent liquidity sweep")
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
