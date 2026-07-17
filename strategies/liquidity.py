"""Liquidity Sweeps, Inducement, Liquidity Voids, and Pool Mapping.

Enhanced institutional liquidity model:
  - Equal highs/lows (buy-side / sell-side liquidity pools)
  - Stop hunt / liquidity sweep detection (wick beyond level, closes back)
  - Inducement sweeps: engineered move to raid stops before real reversal
  - Liquidity voids: fast price moves with no trading → price returns to fill
  - Relative equal highs/lows: price will eventually collect these pools
"""
from .helpers import swing_points, cluster_levels, atr, clamp


def _liquidity_voids(candles, a):
    """Detect gaps / liquidity voids: large candle with body > 2.5 ATR
    where price moved through quickly with no significant wicking.
    These voids act as magnets — price returns to fill them.
    """
    voids = []
    for i in range(len(candles) - 30, len(candles) - 1):
        if i < 0:
            continue
        c = candles[i]
        body = abs(c["close"] - c["open"])
        total_range = c["high"] - c["low"]
        # Large body relative to ATR, low wicking (body is >75% of range)
        if body > a * 2.5 and total_range > 0 and body / total_range > 0.75:
            if c["close"] > c["open"]:
                voids.append({
                    "type": "bullish_void",
                    "top": c["close"],
                    "bottom": c["open"],
                    "mid": (c["close"] + c["open"]) / 2,
                    "time": c["time"],
                })
            else:
                voids.append({
                    "type": "bearish_void",
                    "top": c["open"],
                    "bottom": c["close"],
                    "mid": (c["open"] + c["close"]) / 2,
                    "time": c["time"],
                })
    price = candles[-1]["close"]
    # Only unfilled voids (price hasn't traded through the midpoint)
    unfilled = []
    for v in voids:
        if v["type"] == "bullish_void" and price > v["mid"]:
            unfilled.append(v)   # above — potential fill on pullback
        elif v["type"] == "bearish_void" and price < v["mid"]:
            unfilled.append(v)   # below — potential fill on bounce
    return unfilled[-3:]


def _inducement_sweep(candles, eq_highs, eq_lows, a):
    """Inducement: price engineered to collect liquidity just above/below
    a minor equal level, then reverses strongly. Differs from a true sweep
    in that the move is smaller (0.3–1.0 ATR) and reverses within 1-2 candles.

    Indicates SM created a false move to fill their own orders before the
    true directional move.
    """
    events = []
    tol = a * 0.4
    for c in candles[-8:]:
        # Inducement above equal highs (bearish — SM collected buy stops, now shorting)
        for lv in eq_highs:
            if (c["high"] > lv["price"] and
                    c["high"] - lv["price"] < a * 1.0 and   # small pierce (not a full sweep)
                    c["close"] < lv["price"]):               # closed back below
                events.append({
                    "type": "bearish_inducement",
                    "level": lv["price"],
                    "time": c["time"],
                    "msg": f"Bearish inducement above equal highs {lv['price']:.6g} — buy stops collected"
                })
        # Inducement below equal lows (bullish — SM collected sell stops, now longing)
        for lv in eq_lows:
            if (c["low"] < lv["price"] and
                    lv["price"] - c["low"] < a * 1.0 and    # small pierce
                    c["close"] > lv["price"]):               # closed back above
                events.append({
                    "type": "bullish_inducement",
                    "level": lv["price"],
                    "time": c["time"],
                    "msg": f"Bullish inducement below equal lows {lv['price']:.6g} — sell stops collected"
                })
    return events[-2:]


def analyze(candles):
    highs, lows = swing_points(candles, lookback=3)
    a = atr(candles) or (candles[-1]["close"] * 0.005)
    tol = a * 0.4
    price = candles[-1]["close"]

    eq_highs = [lv for lv in cluster_levels(highs, tol) if lv["touches"] >= 2]
    eq_lows = [lv for lv in cluster_levels(lows, tol) if lv["touches"] >= 2]

    score = 0.0
    reasons = []
    sweeps = []

    # --- Standard liquidity sweeps (wick through level, close back) ---
    for c in candles[-5:]:
        for lv in eq_lows:
            if c["low"] < lv["price"] - tol * 0.2 and c["close"] > lv["price"]:
                score += 0.7
                reasons.append(f"Bullish liquidity sweep below equal lows {lv['price']:.6g} — sell stops taken, reversal likely")
                sweeps.append({"type": "bullish", "price": lv["price"], "time": c["time"]})
        for lv in eq_highs:
            if c["high"] > lv["price"] + tol * 0.2 and c["close"] < lv["price"]:
                score -= 0.7
                reasons.append(f"Bearish liquidity sweep above equal highs {lv['price']:.6g} — buy stops taken, reversal likely")
                sweeps.append({"type": "bearish", "price": lv["price"], "time": c["time"]})

    # --- Inducement sweeps ---
    inducements = _inducement_sweep(candles, eq_highs, eq_lows, a)
    for ind in inducements:
        if ind["type"] == "bullish_inducement":
            score += 0.5
        else:
            score -= 0.5
        reasons.append(ind["msg"])

    # --- Liquidity voids (unfilled) ---
    voids = _liquidity_voids(candles, a)
    for v in voids:
        dist = abs(price - v["mid"]) / a
        if dist < 3.0:  # only score nearby voids
            if v["type"] == "bearish_void" and price < v["mid"]:
                # Void above price — magnet up
                score += 0.3
                reasons.append(f"Bullish liquidity void {v['bottom']:.6g}–{v['top']:.6g} acting as overhead magnet")
            elif v["type"] == "bullish_void" and price > v["mid"]:
                # Void below price — magnet down
                score -= 0.3
                reasons.append(f"Bearish liquidity void {v['bottom']:.6g}–{v['top']:.6g} acting as downside magnet")

    # --- Resting pools near current price (proximity-based awareness) ---
    buy_pools = sorted(
        [lv for lv in eq_highs if lv["price"] > price],
        key=lambda x: x["price"] - price
    )[:2]
    sell_pools = sorted(
        [lv for lv in eq_lows if lv["price"] < price],
        key=lambda x: price - x["price"]
    )[:2]

    # Pool proximity — price approaching a pool = SM likely to engineer a sweep
    if buy_pools:
        nearest = buy_pools[0]
        dist = (nearest["price"] - price) / a
        if dist < 1.5:
            score += 0.2
            reasons.append(f"Buy-side liquidity pool {nearest['price']:.6g} close above ({dist:.1f} ATR) — sweep target")
    if sell_pools:
        nearest = sell_pools[0]
        dist = (price - nearest["price"]) / a
        if dist < 1.5:
            score -= 0.2
            reasons.append(f"Sell-side liquidity pool {nearest['price']:.6g} close below ({dist:.1f} ATR) — sweep target")

    pools = (
        [{"type": "buy_side", "price": lv["price"], "touches": lv["touches"]} for lv in eq_highs[:3]] +
        [{"type": "sell_side", "price": lv["price"], "touches": lv["touches"]} for lv in eq_lows[:3]]
    )

    overlays = {
        "sweeps": sweeps[-3:],
        "liquidity_pools": pools,
        "inducements": inducements,
        "voids": [{"type": v["type"], "top": v["top"], "bottom": v["bottom"]} for v in voids],
    }

    if not reasons:
        reasons.append("No recent liquidity sweep or inducement")
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
