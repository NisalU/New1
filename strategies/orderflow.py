"""Orderflow / Cumulative Volume Delta (CVD) analysis.

Enhanced with institutional orderflow concepts:
  - Delta divergence: price makes new high/low but delta disagrees → reversal
  - Volume-weighted absorption: large volume with no price movement = hidden orders
  - CVD trend vs price trend: hidden accumulation/distribution detection
  - Stacked imbalances: consecutive one-sided delta bars = strong conviction
  - Exhaustion detection: massive delta spike with immediate reversal
"""
from .helpers import clamp


def _build_cvd(candles):
    """Build cumulative volume delta from candle delta field."""
    cvd = []
    cumulative = 0.0
    for c in candles:
        delta = c.get("delta", 0) or 0
        if delta == 0:
            # Approximate delta from candle body direction
            if c["close"] > c["open"]:
                delta = c["volume"] * 0.6
            elif c["close"] < c["open"]:
                delta = -c["volume"] * 0.6
        cumulative += delta
        cvd.append({"time": c["time"], "value": round(cumulative, 2)})
    return cvd


def _delta_divergence(candles, cvd, lookback=10):
    """Detect divergence between price and delta:
      - Bullish divergence: price makes lower low but delta makes higher low → buyers absorbing
      - Bearish divergence: price makes higher high but delta makes lower high → sellers absorbing
    Returns (divergence_type, strength) or (None, 0)
    """
    if len(candles) < lookback or len(cvd) < lookback:
        return None, 0

    prices = [c["close"] for c in candles[-lookback:]]
    deltas = [d["value"] for d in cvd[-lookback:]]

    price_hi = max(prices)
    price_lo = min(prices)
    delta_hi = max(deltas)
    delta_lo = min(deltas)

    current_price = prices[-1]
    current_delta = deltas[-1]

    recent_price_hi = max(prices[-3:])
    recent_price_lo = min(prices[-3:])
    recent_delta_hi = max(deltas[-3:])
    recent_delta_lo = min(deltas[-3:])

    strength = 0.0

    # Bearish divergence: new price high but delta not confirming
    if recent_price_hi >= price_hi * 0.998:
        if recent_delta_hi < delta_hi * 0.90:  # delta notably lower
            strength = min(1.0, (1 - recent_delta_hi / (delta_hi + 1e-9)) * 2)
            return "bearish", strength

    # Bullish divergence: new price low but delta not confirming
    if recent_price_lo <= price_lo * 1.002:
        if recent_delta_lo > delta_lo * 0.90:  # delta notably higher (less negative)
            strength = min(1.0, (1 - recent_delta_lo / (delta_lo - 1e-9)) * 2) if delta_lo < 0 else 0.3
            return "bullish", strength

    return None, 0.0


def _stacked_imbalances(candles, n=5):
    """Stacked imbalances: N consecutive candles all with the same delta direction.
    Indicates strong institutional conviction in one direction.
    """
    if len(candles) < n:
        return None, 0
    recent = candles[-n:]
    bull_count = sum(1 for c in recent if (c.get("delta", 0) or 0) > 0)
    bear_count = sum(1 for c in recent if (c.get("delta", 0) or 0) < 0)
    if bull_count == n:
        return "bullish", bull_count
    elif bear_count == n:
        return "bearish", bear_count
    return None, 0


def _absorption(candles, lookback=5):
    """Absorption: high volume with minimal price movement.
    Large orders being absorbed by hidden institutional supply/demand.
    """
    if len(candles) < lookback + 5:
        return None
    avg_vol = sum(c["volume"] for c in candles[-20:]) / 20 if len(candles) >= 20 else 0
    if avg_vol == 0:
        return None

    recent = candles[-lookback:]
    high_vol_bars = [c for c in recent if c["volume"] > avg_vol * 1.5]
    if not high_vol_bars:
        return None

    # Check if price moved significantly on these high-volume bars
    for c in high_vol_bars:
        body = abs(c["close"] - c["open"])
        total_range = c["high"] - c["low"]
        if total_range == 0:
            continue
        # Small body relative to range on high volume = absorption
        if body / total_range < 0.3:
            net_move = c["close"] - c["open"]
            if net_move > 0:
                return "sell_absorption"  # buying absorbed by sellers
            else:
                return "buy_absorption"   # selling absorbed by buyers
    return None


def _cvd_trend_vs_price(candles, cvd, n=20):
    """Compare CVD trend to price trend over n candles.
    Divergence = hidden accumulation (bullish) or distribution (bearish).
    """
    if len(candles) < n or len(cvd) < n:
        return None

    price_change = candles[-1]["close"] - candles[-n]["close"]
    delta_change = cvd[-1]["value"] - cvd[-n]["value"]

    # Normalize by magnitude to detect meaningful divergence
    price_dir = 1 if price_change > 0 else -1
    delta_dir = 1 if delta_change > 0 else -1

    if price_dir != delta_dir:
        if price_dir == 1 and delta_dir == -1:
            return "hidden_distribution"  # price up, delta down = SM selling into rallies
        elif price_dir == -1 and delta_dir == 1:
            return "hidden_accumulation"  # price down, delta up = SM buying dips
    return None


def analyze(candles):
    cvd = _build_cvd(candles)
    score = 0.0
    reasons = []

    # --- CVD Trend vs Price (macro hidden flow) ---
    macro_div = _cvd_trend_vs_price(candles, cvd, n=20)
    if macro_div == "hidden_accumulation":
        score += 0.45
        reasons.append("Hidden accumulation: price declining but delta rising — institutions buying dips")
    elif macro_div == "hidden_distribution":
        score -= 0.45
        reasons.append("Hidden distribution: price rising but delta falling — institutions selling rallies")

    # --- Delta divergence (short-term reversal signal) ---
    div_type, div_strength = _delta_divergence(candles, cvd, lookback=10)
    if div_type == "bullish":
        score += 0.5 * div_strength
        reasons.append(f"Bullish delta divergence — price made lower low but delta held up (buyers absorbing)")
    elif div_type == "bearish":
        score -= 0.5 * div_strength
        reasons.append(f"Bearish delta divergence — price made higher high but delta weakened (sellers absorbing)")

    # --- Stacked imbalances ---
    stack_dir, stack_n = _stacked_imbalances(candles, n=4)
    if stack_dir == "bullish":
        score += 0.35
        reasons.append(f"{stack_n} consecutive bullish delta bars — stacked buy-side imbalance")
    elif stack_dir == "bearish":
        score -= 0.35
        reasons.append(f"{stack_n} consecutive bearish delta bars — stacked sell-side imbalance")

    # --- Absorption detection ---
    absorption = _absorption(candles, lookback=5)
    if absorption == "buy_absorption":
        score += 0.3
        reasons.append("Buy absorption: heavy selling with price holding — passive buyers present")
    elif absorption == "sell_absorption":
        score -= 0.3
        reasons.append("Sell absorption: heavy buying but price stalled — passive sellers blocking")

    # --- Recent CVD direction (short-term momentum) ---
    if len(cvd) >= 5:
        recent_delta = cvd[-1]["value"] - cvd[-5]["value"]
        avg_vol = sum(abs(c.get("delta", 0) or 0) for c in candles[-20:]) / 20 if len(candles) >= 20 else 1
        if avg_vol > 0:
            rel_delta = recent_delta / (avg_vol * 5)
            if rel_delta > 0.5:
                score += 0.2
                reasons.append("Recent CVD strongly positive — buy pressure dominant")
            elif rel_delta < -0.5:
                score -= 0.2
                reasons.append("Recent CVD strongly negative — sell pressure dominant")

    overlays = {
        "cvd": cvd[-30:],
        "divergence": div_type,
        "macro_flow": macro_div,
    }

    if not reasons:
        reasons.append("Orderflow neutral — no clear delta bias or divergence")
    return {"score": clamp(score), "reasons": reasons, "overlays": overlays}
