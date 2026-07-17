"""Smart Money Concepts: BOS/CHoCH, Order Blocks, Fair Value Gaps,
Breaker Blocks, and Mitigation Blocks.

Enhanced with institutional-grade concepts:
  - Breaker Blocks: invalidated OBs that become magnets on return visits
  - Mitigation Blocks: unfilled imbalances price will revisit
  - Displacement confirmation: FVGs must be created by strong displacement candles
  - Premium/Discount zones: 50% of the current swing range
"""
from .helpers import swing_points, atr, clamp


def _structure(candles):
    """Detect Break of Structure / Change of Character from swing sequence."""
    highs, lows = swing_points(candles, lookback=3)
    events = []
    price = candles[-1]["close"]

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

    if highs and price > highs[-1][1]:
        events.append(("CHoCH" if trend == -1 else "BOS", 1,
                       f"{'CHoCH' if trend == -1 else 'BOS'} above swing high {highs[-1][1]:.6g}"))
    if lows and price < lows[-1][1]:
        events.append(("CHoCH" if trend == 1 else "BOS", -1,
                       f"{'CHoCH' if trend == 1 else 'BOS'} below swing low {lows[-1][1]:.6g}"))
    return trend, events


def _order_blocks(candles, a):
    """Last opposite-color candle before a strong impulsive move (1.5 ATR+)."""
    obs = []
    for i in range(len(candles) - 40, len(candles) - 2):
        if i < 1:
            continue
        c = candles[i]
        move = candles[i + 1]["close"] - c["close"]
        body = abs(c["close"] - c["open"])
        if c["close"] < c["open"] and move > a * 1.5 and body > 0:
            obs.append({"type": "bullish", "top": c["high"], "bottom": c["low"],
                        "mid": (c["high"] + c["low"]) / 2,
                        "time": c["time"], "mitigated": False})
        if c["close"] > c["open"] and move < -a * 1.5 and body > 0:
            obs.append({"type": "bearish", "top": c["high"], "bottom": c["low"],
                        "mid": (c["high"] + c["low"]) / 2,
                        "time": c["time"], "mitigated": False})

    price = candles[-1]["close"]
    # Mark mitigated OBs (price has traded through midpoint)
    for ob in obs:
        mid = ob["mid"]
        if ob["type"] == "bullish" and price < mid:
            ob["mitigated"] = True
        elif ob["type"] == "bearish" and price > mid:
            ob["mitigated"] = True

    valid = [ob for ob in obs if not ob["mitigated"] and (
        (ob["type"] == "bullish" and price > ob["bottom"]) or
        (ob["type"] == "bearish" and price < ob["top"])
    )]
    return valid[-4:]


def _breaker_blocks(candles, a):
    """Breaker blocks: OBs that WERE mitigated — price broke through them.
    These become strong magnets when price returns to them from the other side.

    Bullish breaker: was a bearish OB, now price is below it → magnet up
    Bearish breaker: was a bullish OB, now price is above it → magnet down
    """
    obs_all = []
    for i in range(len(candles) - 60, len(candles) - 2):
        if i < 1:
            continue
        c = candles[i]
        move = candles[i + 1]["close"] - c["close"]
        body = abs(c["close"] - c["open"])
        if c["close"] < c["open"] and move > a * 1.5 and body > 0:
            obs_all.append({"original_type": "bullish", "top": c["high"],
                            "bottom": c["low"], "mid": (c["high"] + c["low"]) / 2,
                            "time": c["time"]})
        if c["close"] > c["open"] and move < -a * 1.5 and body > 0:
            obs_all.append({"original_type": "bearish", "top": c["high"],
                            "bottom": c["low"], "mid": (c["high"] + c["low"]) / 2,
                            "time": c["time"]})

    price = candles[-1]["close"]
    breakers = []
    for ob in obs_all:
        if ob["original_type"] == "bullish" and price < ob["bottom"]:
            # Bullish OB was broken bearishly → bearish breaker
            breakers.append({"type": "bearish_breaker",
                             "top": ob["top"], "bottom": ob["bottom"],
                             "mid": ob["mid"], "time": ob["time"]})
        elif ob["original_type"] == "bearish" and price > ob["top"]:
            # Bearish OB was broken bullishly → bullish breaker
            breakers.append({"type": "bullish_breaker",
                             "top": ob["top"], "bottom": ob["bottom"],
                             "mid": ob["mid"], "time": ob["time"]})
    return breakers[-3:]


def _fair_value_gaps(candles, a):
    """3-candle imbalance. Only counts FVGs created by displacement candles
    (candle body > 1.2× ATR = institutionally significant move)."""
    fvgs = []
    avg_body = sum(abs(c["close"] - c["open"]) for c in candles[-20:]) / 20 if len(candles) >= 20 else a

    for i in range(len(candles) - 30, len(candles) - 2):
        if i < 0:
            continue
        c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]
        displacement_body = abs(c2["close"] - c2["open"])
        is_displacement = displacement_body > avg_body * 1.2  # institutional move

        if c1["high"] < c3["low"]:
            fvgs.append({
                "type": "bullish", "top": c3["low"], "bottom": c1["high"],
                "mid": (c3["low"] + c1["high"]) / 2,
                "time": c2["time"],
                "displacement": is_displacement,
            })
        if c1["low"] > c3["high"]:
            fvgs.append({
                "type": "bearish", "top": c1["low"], "bottom": c3["high"],
                "mid": (c1["low"] + c3["high"]) / 2,
                "time": c2["time"],
                "displacement": is_displacement,
            })

    price = candles[-1]["close"]
    open_fvgs = []
    for f in fvgs:
        if f["type"] == "bullish" and price > f["bottom"]:
            open_fvgs.append(f)
        elif f["type"] == "bearish" and price < f["top"]:
            open_fvgs.append(f)
    return open_fvgs[-4:]


def _premium_discount(candles, highs, lows):
    """Identify if price is in a premium (above 50% of range) or discount zone."""
    if not highs or not lows:
        return None, 0.0
    swing_hi = max(h[1] for h in highs[-6:]) if highs else None
    swing_lo = min(l[1] for l in lows[-6:]) if lows else None
    if swing_hi is None or swing_lo is None or swing_hi <= swing_lo:
        return None, 0.0
    eq = (swing_hi + swing_lo) / 2
    price = candles[-1]["close"]
    pct = (price - swing_lo) / (swing_hi - swing_lo)
    if pct > 0.5:
        return "premium", pct      # above 50% = premium = expensive = look for shorts
    else:
        return "discount", pct     # below 50% = discount = cheap = look for longs


def analyze(candles):
    a = atr(candles) or (candles[-1]["close"] * 0.005)
    price = candles[-1]["close"]
    highs, lows = swing_points(candles, lookback=3)
    trend, events = _structure(candles)
    obs = _order_blocks(candles, a)
    breakers = _breaker_blocks(candles, a)
    fvgs = _fair_value_gaps(candles, a)
    zone, zone_pct = _premium_discount(candles, highs, lows)

    score = 0.0
    reasons = []

    # --- Structure events (BOS/CHoCH) ---
    for name, direction, msg in events:
        score += direction * (0.6 if name == "CHoCH" else 0.45)
        reasons.append(msg)

    if trend == 1 and not events:
        score += 0.25
        reasons.append("Market structure bullish (HH + HL)")
    elif trend == -1 and not events:
        score -= 0.25
        reasons.append("Market structure bearish (LH + LL)")

    # --- Order Blocks: proximity-based score ---
    for ob in obs:
        if ob["type"] == "bullish":
            dist = (price - ob["bottom"]) / a
            if 0 <= dist < 1.5:
                boost = 0.5 if dist < 0.5 else 0.3
                score += boost
                reasons.append(f"Price at bullish OB {ob['bottom']:.6g}–{ob['top']:.6g}")
        elif ob["type"] == "bearish":
            dist = (ob["top"] - price) / a
            if 0 <= dist < 1.5:
                boost = 0.5 if dist < 0.5 else 0.3
                score -= boost
                reasons.append(f"Price at bearish OB {ob['bottom']:.6g}–{ob['top']:.6g}")

    # --- Breaker Blocks: strong magnet signals ---
    for bb in breakers:
        if bb["type"] == "bullish_breaker":
            dist = (price - bb["bottom"]) / a
            if 0 <= dist < 2.0:
                score += 0.4
                reasons.append(f"Bullish breaker block {bb['bottom']:.6g}–{bb['top']:.6g} acting as support (former bearish OB reclaimed)")
        elif bb["type"] == "bearish_breaker":
            dist = (bb["top"] - price) / a
            if 0 <= dist < 2.0:
                score -= 0.4
                reasons.append(f"Bearish breaker block {bb['bottom']:.6g}–{bb['top']:.6g} acting as resistance (former bullish OB lost)")

    # --- Fair Value Gaps ---
    for fvg in fvgs:
        displacement_bonus = 0.15 if fvg["displacement"] else 0.0
        if fvg["type"] == "bullish":
            dist = (price - fvg["bottom"]) / a
            if 0 <= dist < 1.2:
                score += 0.35 + displacement_bonus
                tag = " (displacement FVG)" if fvg["displacement"] else ""
                reasons.append(f"Price in bullish FVG {fvg['bottom']:.6g}–{fvg['top']:.6g}{tag}")
        elif fvg["type"] == "bearish":
            dist = (fvg["top"] - price) / a
            if 0 <= dist < 1.2:
                score -= 0.35 + displacement_bonus
                tag = " (displacement FVG)" if fvg["displacement"] else ""
                reasons.append(f"Price in bearish FVG {fvg['bottom']:.6g}–{fvg['top']:.6g}{tag}")

    # --- Premium/Discount zone ---
    if zone == "premium":
        score -= 0.2
        reasons.append(f"Price in premium zone ({zone_pct:.0%} of swing range) — expensive, favor shorts")
    elif zone == "discount":
        score += 0.2
        reasons.append(f"Price in discount zone ({zone_pct:.0%} of swing range) — cheap, favor longs")

    if not reasons:
        reasons.append("SMC neutral — no active OBs, FVGs, or structure events near price")

    overlays = {
        "structure": {"trend": trend, "events": [e[2] for e in events]},
        "order_blocks": obs,
        "breaker_blocks": breakers,
        "fvg": fvgs,
        "premium_discount": {"zone": zone, "pct": round(zone_pct, 3)} if zone else None,
    }
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
