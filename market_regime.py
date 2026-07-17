"""Market regime classifier — runs BEFORE strategies/AI are trusted.

Cheap, deterministic, and pure Python (no extra deps). Its only job is to
answer "is this an environment worth spending an AI call on?" so the bot
can skip Groq entirely during chop/extreme-volatility conditions instead of
generating a trade idea for a market that shouldn't be traded at all.

Classification is derived from data the engine already computed: SMC trend
(strategies/smc.py), composite score, and simple range/ATR expansion ratios
on the same candle set — no separate network calls.
"""
from strategies.helpers import atr

import config


def classify(analysis):
    candles = analysis["candles"]
    price = analysis["price"]
    overlays = analysis.get("overlays", {})
    composite = analysis["composite"]
    a = atr(candles) or price * 0.005

    structure = overlays.get("structure") or {}
    trend = structure.get("trend", 0)

    # Range compression: how wide the last 20 closes are relative to ATR.
    closes = [c["close"] for c in candles[-20:]] or [price]
    range20 = max(closes) - min(closes)
    compression = range20 / (a * 20) if a else 1.0  # <0.4 tight, >1 expansive

    # Volatility expansion: recent true range vs a longer baseline.
    recent_n = min(14, len(candles))
    base_n = min(60, len(candles))
    vol_recent = sum(c["high"] - c["low"] for c in candles[-recent_n:]) / recent_n
    vol_base = sum(c["high"] - c["low"] for c in candles[-base_n:]) / base_n if base_n else vol_recent
    expansion = (vol_recent / vol_base) if vol_base else 1.0

    fundamentals = overlays.get("fundamentals")
    reasons = []
    tradeable = True

    if expansion > config.REGIME_VOLATILITY_SPIKE:
        regime = "high_volatility"
        reasons.append(f"True range expanded {expansion:.1f}x vs the 60-candle baseline")
        tradeable = False
    elif trend == 1 and composite >= config.SIGNAL_THRESHOLD * 0.5:
        regime = "trending_bullish"
        reasons.append("Higher highs/higher lows with bullish composite score")
    elif trend == -1 and composite <= -config.SIGNAL_THRESHOLD * 0.5:
        regime = "trending_bearish"
        reasons.append("Lower highs/lower lows with bearish composite score")
    elif compression < config.REGIME_COMPRESSION_TIGHT and abs(composite) < config.SIGNAL_THRESHOLD * 0.6:
        # Sideways: try to tell accumulation from distribution using OI + funding
        if fundamentals and fundamentals.get("oi_change_pct", 0) > 2:
            regime = "distribution" if fundamentals.get("funding_rate", 0) > 0 else "accumulation"
            reasons.append("Range-bound with rising open interest — positioning is building")
        else:
            regime = "range"
            reasons.append(f"Price compressed to {compression:.2f}x ATR over 20 candles")
        tradeable = False
    else:
        regime = "mixed"
        reasons.append("No clean trend or range read — structure and composite disagree")
        tradeable = False

    if not reasons:
        reasons.append("No standout regime signature")

    return {
        "regime": regime,
        "tradeable": tradeable,
        "compression": round(compression, 2),
        "volatility_expansion": round(expansion, 2),
        "reasons": reasons,
    }
