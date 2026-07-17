"""Wyckoff Market Cycle Phase Detector.

Wyckoff's method classifies the market into four phases based on price
action, volume spread, and structural context. Institutions use this
framework to identify when smart money is accumulating or distributing.

Phases:
  Accumulation  — SM absorbing supply, range-bound, spring/shakeout common
  Markup        — SM has absorbed supply, price trends up on expanding volume
  Distribution  — SM offloading positions, range-bound near highs, UTAD common
  Markdown      — SM has sold, price trends down on expanding volume

Key Wyckoff events detected:
  Spring / Shakeout  — bear trap below support, reverses back up quickly
  UTAD               — upthrust after distribution, bull trap above resistance
  LPS                — last point of support (shallow pullback in uptrend)
  SOW                — sign of weakness (break below support in range)
  SOS                — sign of strength (break above resistance on volume)
  BC / SC            — buying / selling climax (exhaustion candle + reversal)
"""
from .helpers import swing_points, atr, clamp


def _volume_trend(candles, short=10, long=30):
    """Compare recent avg volume vs longer baseline."""
    if len(candles) < long:
        return 1.0
    recent = sum(c["volume"] for c in candles[-short:]) / short
    base = sum(c["volume"] for c in candles[-long:-short]) / (long - short)
    return recent / base if base > 0 else 1.0


def _price_spread_trend(candles, n=10):
    """Avg candle body size relative to ATR: expanding = conviction, shrinking = absorption."""
    a = atr(candles) or 1
    recent = sum(abs(c["close"] - c["open"]) for c in candles[-n:]) / n
    return recent / a


def _detect_spring(candles, sup_price, a):
    """Spring: wick below support but closes back above it in last 5 candles."""
    for c in candles[-5:]:
        if c["low"] < sup_price - a * 0.1 and c["close"] > sup_price:
            return True, c
    return False, None


def _detect_utad(candles, res_price, a):
    """UTAD: wick above resistance but closes back below it in last 5 candles."""
    for c in candles[-5:]:
        if c["high"] > res_price + a * 0.1 and c["close"] < res_price:
            return True, c
    return False, None


def _detect_climax(candles, n=3):
    """Buying/Selling climax: large body + above-average volume + immediate reversal."""
    if len(candles) < n + 3:
        return None
    avg_vol = sum(c["volume"] for c in candles[-20:]) / 20 if len(candles) >= 20 else 0
    a = atr(candles) or 1
    events = []
    for i in range(len(candles) - n - 2, len(candles) - 2):
        if i < 0:
            continue
        c = candles[i]
        body = abs(c["close"] - c["open"])
        # Large body + high volume = climax candidate
        if body > a * 1.5 and c["volume"] > avg_vol * 1.8:
            # Check reversal in next 2 candles
            c1 = candles[i + 1]
            c2 = candles[i + 2]
            if c["close"] > c["open"]:  # bullish candle = potential BC
                if c1["close"] < c["close"] and c2["close"] < c1["close"]:
                    events.append(("BC", i, "Buying climax — exhaustion reversal, distribution risk"))
            else:  # bearish candle = potential SC
                if c1["close"] > c["close"] and c2["close"] > c1["close"]:
                    events.append(("SC", i, "Selling climax — exhaustion reversal, accumulation likely"))
    return events[-1] if events else None


def analyze(candles):
    if len(candles) < 40:
        return {"score": 0, "reasons": ["Not enough candles for Wyckoff analysis"], "overlays": {}}

    price = candles[-1]["close"]
    a = atr(candles) or price * 0.005
    highs, lows = swing_points(candles, lookback=3)

    vol_ratio = _volume_trend(candles)        # >1 = expanding volume
    spread = _price_spread_trend(candles)     # >1 = large body (conviction)

    score = 0.0
    reasons = []
    phase = "unknown"
    events = []

    # Determine price trend: HH+HL = uptrend, LH+LL = downtrend
    uptrend = False
    downtrend = False
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        uptrend = hh and hl
        downtrend = lh and ll

    # --- Range detection (needed for accumulation/distribution) ---
    closes_20 = [c["close"] for c in candles[-20:]]
    range_hi = max(closes_20)
    range_lo = min(closes_20)
    range_width = range_hi - range_lo
    in_range = range_width < a * 6  # narrow range = potential acc/dist

    # --- Support/Resistance from range extremes ---
    sup = range_lo
    res = range_hi

    # --- Phase classification ---
    if uptrend and vol_ratio > 1.1 and spread > 0.8:
        phase = "markup"
        score += 0.6
        reasons.append(f"Wyckoff Markup phase — HH+HL with expanding volume ({vol_ratio:.1f}x)")

    elif downtrend and vol_ratio > 1.1 and spread > 0.8:
        phase = "markdown"
        score -= 0.6
        reasons.append(f"Wyckoff Markdown phase — LH+LL with expanding volume ({vol_ratio:.1f}x)")

    elif in_range:
        # Distinguish accumulation vs distribution by price position in range and volume on up/down bars
        up_vol = sum(c["volume"] for c in candles[-20:] if c["close"] > c["open"])
        dn_vol = sum(c["volume"] for c in candles[-20:] if c["close"] <= c["open"])

        if up_vol > dn_vol * 1.3:
            # More volume on up bars in range = accumulation (SM absorbing supply)
            phase = "accumulation"
            score += 0.35
            reasons.append("Wyckoff Accumulation — range with up-bar volume dominance (SM absorbing supply)")
        elif dn_vol > up_vol * 1.3:
            # More volume on down bars in range = distribution (SM offloading)
            phase = "distribution"
            score -= 0.35
            reasons.append("Wyckoff Distribution — range with down-bar volume dominance (SM offloading)")
        else:
            phase = "range"
            reasons.append("Wyckoff Range — volume balanced, phase unclear (wait for SOS/SOW)")

    # --- Key event detection ---
    # Spring (in range/accumulation)
    if in_range or phase == "accumulation":
        sprung, sc = _detect_spring(candles, sup, a)
        if sprung:
            score += 0.5
            reasons.append(f"Wyckoff Spring detected — bear trap below {sup:.6g}, reversal confirmed")
            events.append({"type": "spring", "price": sup})

    # UTAD (in range/distribution)
    if in_range or phase == "distribution":
        utad, uc = _detect_utad(candles, res, a)
        if utad:
            score -= 0.5
            reasons.append(f"Wyckoff UTAD detected — bull trap above {res:.6g}, distribution confirmed")
            events.append({"type": "utad", "price": res})

    # SOS — sign of strength (break above resistance on strong volume)
    if highs and price > res and vol_ratio > 1.2:
        score += 0.3
        reasons.append(f"SOS — Sign of Strength, break above {res:.6g} on expanding volume")
        events.append({"type": "sos", "price": res})

    # SOW — sign of weakness (break below support on strong volume)
    if lows and price < sup and vol_ratio > 1.2:
        score -= 0.3
        reasons.append(f"SOW — Sign of Weakness, break below {sup:.6g} on expanding volume")
        events.append({"type": "sow", "price": sup})

    # LPS — last point of support (shallow pullback in markup, low volume)
    if phase == "markup" and (price - sup) / a < 2 and vol_ratio < 0.9:
        score += 0.2
        reasons.append("LPS — Last Point of Support, shallow pullback on declining volume (continuation likely)")
        events.append({"type": "lps", "price": price})

    # Climax detection
    climax = _detect_climax(candles)
    if climax:
        ctype, cidx, cmsg = climax
        if ctype == "BC":
            score -= 0.35
        else:
            score += 0.35
        reasons.append(cmsg)
        events.append({"type": ctype.lower(), "candle_idx": cidx})

    overlays = {
        "wyckoff": {
            "phase": phase,
            "volume_ratio": round(vol_ratio, 2),
            "spread_ratio": round(spread, 2),
            "events": events,
            "range": {"high": round(res, 6), "low": round(sup, 6)} if in_range else None,
        }
    }

    if not reasons:
        reasons.append("Wyckoff structure unclear — insufficient price/volume pattern")

    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
