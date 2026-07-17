"""EMA 7 / 25 / 99 trend and crossover analysis."""
from .helpers import ema, clamp


def analyze(candles):
    closes = [c["close"] for c in candles]
    e7, e25, e99 = ema(closes, 7), ema(closes, 25), ema(closes, 99)

    overlays = {
        "ema7": [{"time": candles[i]["time"], "value": e7[i]} for i in range(len(candles)) if e7[i]],
        "ema25": [{"time": candles[i]["time"], "value": e25[i]} for i in range(len(candles)) if e25[i]],
        "ema99": [{"time": candles[i]["time"], "value": e99[i]} for i in range(len(candles)) if e99[i]],
    }

    if not e99[-1]:
        return {"score": 0, "reasons": ["Not enough data for EMA99"], "overlays": overlays}

    price = closes[-1]
    score = 0.0
    reasons = []

    # Stack alignment: strongest signal is full bullish/bearish stack
    if e7[-1] > e25[-1] > e99[-1]:
        score += 0.6
        reasons.append("Bullish EMA stack (7 > 25 > 99)")
    elif e7[-1] < e25[-1] < e99[-1]:
        score -= 0.6
        reasons.append("Bearish EMA stack (7 < 25 < 99)")

    # Price relative to EMA99 (long-term bias)
    if price > e99[-1]:
        score += 0.2
        reasons.append("Price above EMA99")
    else:
        score -= 0.2
        reasons.append("Price below EMA99")

    # Fresh EMA7/EMA25 cross in the last 3 candles
    for i in range(len(candles) - 3, len(candles)):
        if not (e7[i] and e25[i] and e7[i - 1] and e25[i - 1]):
            continue
        if e7[i - 1] <= e25[i - 1] and e7[i] > e25[i]:
            score += 0.3
            reasons.append("Fresh bullish EMA 7/25 cross")
            break
        if e7[i - 1] >= e25[i - 1] and e7[i] < e25[i]:
            score -= 0.3
            reasons.append("Fresh bearish EMA 7/25 cross")
            break

    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
