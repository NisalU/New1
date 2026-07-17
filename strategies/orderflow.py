"""Orderflow: per-candle delta, CVD and CVD divergence.

Delta is computed from Binance kline taker-buy volume:
    delta = taker_buy - taker_sell = 2 * taker_buy - total_volume
CVD is the cumulative sum of delta.
"""
from .helpers import swing_points, clamp


def analyze(candles):
    cvd = []
    run = 0.0
    for c in candles:
        run += c["delta"]
        cvd.append({"time": c["time"], "value": run})

    score = 0.0
    reasons = []

    # Recent delta pressure (last 10 candles vs average absolute delta)
    recent = [c["delta"] for c in candles[-10:]]
    avg_abs = (sum(abs(c["delta"]) for c in candles[-60:]) / max(len(candles[-60:]), 1)) or 1e-9
    pressure = sum(recent) / (avg_abs * 10)
    if pressure > 0.25:
        score += min(pressure, 0.6)
        reasons.append("Aggressive buying pressure (positive delta)")
    elif pressure < -0.25:
        score += max(pressure, -0.6)
        reasons.append("Aggressive selling pressure (negative delta)")

    # CVD divergence vs price on swing points
    ph, pl = swing_points(candles, lookback=3)
    cvd_vals = [p["value"] for p in cvd]
    div = None
    if len(ph) >= 2:
        (i1, p1), (i2, p2) = ph[-2], ph[-1]
        if p2 > p1 and cvd_vals[i2] < cvd_vals[i1]:
            score -= 0.6
            div = {"type": "bearish", "from_time": candles[i1]["time"], "to_time": candles[i2]["time"]}
            reasons.append("Bearish CVD divergence: higher high in price, lower high in CVD")
    if len(pl) >= 2:
        (i1, p1), (i2, p2) = pl[-2], pl[-1]
        if p2 < p1 and cvd_vals[i2] > cvd_vals[i1]:
            score += 0.6
            div = {"type": "bullish", "from_time": candles[i1]["time"], "to_time": candles[i2]["time"]}
            reasons.append("Bullish CVD divergence: lower low in price, higher low in CVD")

    # Absorption: big delta but small price move on the last candle
    last = candles[-1]
    rng = last["high"] - last["low"] or 1e-9
    avg_rng = sum(c["high"] - c["low"] for c in candles[-30:]) / 30 or 1e-9
    if abs(last["delta"]) > avg_abs * 2 and rng < avg_rng * 0.6:
        if last["delta"] > 0:
            score -= 0.3
            reasons.append("Buy absorption: heavy buying but price stalled (passive sellers)")
        else:
            score += 0.3
            reasons.append("Sell absorption: heavy selling but price held (passive buyers)")

    overlays = {"cvd": cvd, "divergence": div}
    if not reasons:
        reasons.append("Orderflow neutral")
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
