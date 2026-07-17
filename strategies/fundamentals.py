"""Fundamentals / market context from Binance futures data:
funding rate, open interest change, long/short account ratio.
Degrades gracefully when the futures API is unreachable.
"""
from .helpers import clamp


def analyze(candles, futures_stats):
    if not futures_stats:
        return {"score": 0, "reasons": ["Futures data unavailable (skipped)"],
                "overlays": {"fundamentals": None}}

    fr = futures_stats["funding_rate"]
    oi_chg = futures_stats["oi_change_pct"]
    ls = futures_stats["long_short_ratio"]
    price_chg = (candles[-1]["close"] - candles[-24]["close"]) / candles[-24]["close"] * 100 \
        if len(candles) > 24 else 0

    score = 0.0
    reasons = []

    # Extreme funding is a contrarian signal (crowded trade)
    if fr > 0.0005:
        score -= 0.4
        reasons.append(f"High positive funding {fr * 100:.4f}% — longs crowded")
    elif fr < -0.0005:
        score += 0.4
        reasons.append(f"Negative funding {fr * 100:.4f}% — shorts crowded")

    # OI rising with price = trend confirmation; OI rising while price falls = shorts building
    if oi_chg > 2 and price_chg > 0:
        score += 0.3
        reasons.append(f"Open interest +{oi_chg:.1f}% with rising price (trend fuel)")
    elif oi_chg > 2 and price_chg < 0:
        score -= 0.3
        reasons.append(f"Open interest +{oi_chg:.1f}% with falling price (shorts building)")
    elif oi_chg < -2:
        reasons.append(f"Open interest {oi_chg:.1f}% — positions unwinding")

    # Extreme long/short ratio, contrarian
    if ls > 3:
        score -= 0.3
        reasons.append(f"Long/short ratio {ls:.2f} — retail heavily long")
    elif ls < 0.5:
        score += 0.3
        reasons.append(f"Long/short ratio {ls:.2f} — retail heavily short")

    if not reasons:
        reasons.append("Funding, OI and positioning are neutral")
    return {"score": clamp(score), "reasons": reasons,
            "overlays": {"fundamentals": futures_stats}}
