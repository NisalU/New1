"""VWAP, Session VWAP, and Anchored VWAP with standard deviation bands.

Institutions use VWAP as their benchmark execution price. Price relative to
VWAP and its bands is one of the most reliable institutional edge indicators.
"""
import math
from .helpers import atr, clamp

# Session length estimates in candles by interval
_SESSION_CANDLES = {
    "1m": 390, "3m": 130, "5m": 78, "15m": 26,
    "30m": 13, "1h": 6, "4h": 1, "1d": 1,
}


def _rolling_vwap(candles, window):
    """Compute VWAP and 1σ/2σ bands over the last `window` candles."""
    subset = candles[-window:] if len(candles) >= window else candles
    tp_vol = 0.0
    vol_sum = 0.0
    tp2_vol = 0.0
    for c in subset:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        v = c["volume"] or 1e-9
        tp_vol += tp * v
        vol_sum += v
        tp2_vol += tp * tp * v
    if vol_sum == 0:
        return None
    vwap = tp_vol / vol_sum
    variance = max(0.0, tp2_vol / vol_sum - vwap ** 2)
    sigma = math.sqrt(variance)
    return {
        "vwap": vwap,
        "upper1": vwap + sigma,
        "upper2": vwap + 2 * sigma,
        "lower1": vwap - sigma,
        "lower2": vwap - 2 * sigma,
        "sigma": sigma,
    }


def _anchored_vwap(candles, anchor_idx):
    """VWAP anchored from a specific swing point index."""
    subset = candles[anchor_idx:]
    tp_vol = 0.0
    vol_sum = 0.0
    for c in subset:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        v = c["volume"] or 1e-9
        tp_vol += tp * v
        vol_sum += v
    if vol_sum == 0:
        return None
    return tp_vol / vol_sum


def analyze(candles):
    if len(candles) < 10:
        return {"score": 0, "reasons": ["Not enough candles for VWAP"], "overlays": {}}

    price = candles[-1]["close"]
    a = atr(candles) or price * 0.005

    # Session VWAP (rolling ~1 trading session)
    session_w = min(26, len(candles))  # default 26 candles (~6.5h on 15m)
    sv = _rolling_vwap(candles, session_w)

    # Swing-anchored VWAP from most recent significant swing low and high
    # Find swing low: lowest low in last 40 candles
    lookback = min(40, len(candles) - 1)
    swing_low_idx = len(candles) - 1 - min(
        range(lookback),
        key=lambda i: candles[len(candles) - 1 - i]["low"]
    )
    swing_high_idx = len(candles) - 1 - min(
        range(lookback),
        key=lambda i: -candles[len(candles) - 1 - i]["high"]
    )
    avwap_low = _anchored_vwap(candles, swing_low_idx)
    avwap_high = _anchored_vwap(candles, swing_high_idx)

    score = 0.0
    reasons = []
    overlays = {}

    if sv:
        vwap = sv["vwap"]
        sigma = sv["sigma"]
        overlays["vwap"] = {
            "value": round(vwap, 6),
            "upper1": round(sv["upper1"], 6),
            "upper2": round(sv["upper2"], 6),
            "lower1": round(sv["lower1"], 6),
            "lower2": round(sv["lower2"], 6),
        }

        dist_atr = (price - vwap) / a if a else 0

        # Price above/below VWAP — institutional bias
        if price > vwap:
            score += 0.25
            reasons.append(f"Price above session VWAP {vwap:.6g} — institutional buyers in control")
        else:
            score -= 0.25
            reasons.append(f"Price below session VWAP {vwap:.6g} — institutional sellers in control")

        # 2σ extension — mean reversion signal (institutions fade these)
        if sigma > 0:
            if price > sv["upper2"]:
                score -= 0.5
                reasons.append(f"Price at +2σ VWAP band {sv['upper2']:.6g} — extreme extension, mean reversion risk")
            elif price > sv["upper1"]:
                score += 0.2
                reasons.append(f"Price at +1σ VWAP band — bullish momentum accepted")
            elif price < sv["lower2"]:
                score += 0.5
                reasons.append(f"Price at -2σ VWAP band {sv['lower2']:.6g} — extreme extension, bounce expected")
            elif price < sv["lower1"]:
                score -= 0.2
                reasons.append(f"Price at -1σ VWAP band — bearish momentum accepted")

        # VWAP reclaim — high probability continuation signal
        prev = candles[-2]["close"] if len(candles) > 1 else price
        if prev < vwap <= price:
            score += 0.4
            reasons.append(f"Bullish VWAP reclaim — price crossed back above {vwap:.6g}")
        elif prev > vwap >= price:
            score -= 0.4
            reasons.append(f"Bearish VWAP loss — price crossed back below {vwap:.6g}")

    # Anchored VWAP from swing low — bullish anchor
    if avwap_low:
        overlays["avwap_low"] = round(avwap_low, 6)
        if price > avwap_low:
            score += 0.15
            reasons.append(f"Price above AVWAP anchored from swing low {avwap_low:.6g}")
        else:
            score -= 0.15
            reasons.append(f"Price below AVWAP from swing low — bullish anchor lost")

    # Anchored VWAP from swing high — bearish anchor
    if avwap_high:
        overlays["avwap_high"] = round(avwap_high, 6)
        if price < avwap_high:
            score -= 0.1
            reasons.append(f"Price below AVWAP anchored from swing high {avwap_high:.6g} — distribution pressure")
        else:
            score += 0.1
            reasons.append(f"Price above AVWAP from swing high — sellers absorbed")

    if not reasons:
        reasons.append("VWAP neutral — price rotating around value")

    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
