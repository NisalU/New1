"""Auction Market Theory: volume profile with POC and value area."""
from .helpers import clamp

BINS = 40


def analyze(candles):
    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    if hi <= lo:
        return {"score": 0, "reasons": ["Flat range"], "overlays": {}}
    step = (hi - lo) / BINS

    # Distribute each candle's volume evenly across the bins it spans
    vols = [0.0] * BINS
    for c in candles:
        b0 = int((c["low"] - lo) / step)
        b1 = int((c["high"] - lo) / step)
        b0, b1 = max(0, min(b0, BINS - 1)), max(0, min(b1, BINS - 1))
        span = b1 - b0 + 1
        for b in range(b0, b1 + 1):
            vols[b] += c["volume"] / span

    total = sum(vols) or 1e-9
    poc_bin = max(range(BINS), key=lambda b: vols[b])
    poc = lo + (poc_bin + 0.5) * step

    # Value area: expand around POC until 70% of volume covered
    covered = vols[poc_bin]
    lo_b, hi_b = poc_bin, poc_bin
    while covered / total < 0.70 and (lo_b > 0 or hi_b < BINS - 1):
        down = vols[lo_b - 1] if lo_b > 0 else -1
        up = vols[hi_b + 1] if hi_b < BINS - 1 else -1
        if up >= down:
            hi_b += 1
            covered += vols[hi_b]
        else:
            lo_b -= 1
            covered += vols[lo_b]
    val = lo + lo_b * step          # value area low
    vah = lo + (hi_b + 1) * step    # value area high

    price = candles[-1]["close"]
    score = 0.0
    reasons = []

    if price > vah:
        score += 0.5
        reasons.append(f"Price above value area high {vah:.6g} (acceptance = bullish auction)")
    elif price < val:
        score -= 0.5
        reasons.append(f"Price below value area low {val:.6g} (acceptance = bearish auction)")
    else:
        # Inside value: rotation back to POC is the expectation
        if price > poc:
            score -= 0.2
            reasons.append(f"Inside value above POC {poc:.6g}, rotation risk down")
        else:
            score += 0.2
            reasons.append(f"Inside value below POC {poc:.6g}, rotation bias up")

    profile = [{"price": lo + (b + 0.5) * step, "volume": vols[b]} for b in range(BINS)]
    overlays = {"volume_profile": {"poc": poc, "vah": vah, "val": val, "bins": profile}}
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
