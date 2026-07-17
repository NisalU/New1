"""Shared pure-Python math helpers for all strategies (no numpy/pandas)."""


def ema(values, period):
    """Exponential moving average. Returns list aligned to input (None until seeded)."""
    if len(values) < period:
        return [None] * len(values)
    out = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    k = 2 / (period + 1)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr(candles, period=14):
    """Average True Range of the last `period` candles (single float)."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(len(candles) - period, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["high"] - c["low"],
                       abs(c["high"] - p["close"]),
                       abs(c["low"] - p["close"])))
    return sum(trs) / len(trs)


def swing_points(candles, lookback=3):
    """Find swing highs/lows: a candle whose high/low is the extreme of
    `lookback` candles on each side. Returns (highs, lows) as lists of
    (index, price)."""
    highs, lows = [], []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window = candles[i - lookback:i + lookback + 1]
        h, l = candles[i]["high"], candles[i]["low"]
        if h == max(c["high"] for c in window):
            highs.append((i, h))
        if l == min(c["low"] for c in window):
            lows.append((i, l))
    return highs, lows


def cluster_levels(points, tolerance):
    """Group nearby price points into levels.
    Returns list of dicts {price, touches} sorted by touches desc."""
    levels = []
    for _, price in points:
        placed = False
        for lv in levels:
            if abs(lv["price"] - price) <= tolerance:
                lv["prices"].append(price)
                lv["price"] = sum(lv["prices"]) / len(lv["prices"])
                placed = True
                break
        if not placed:
            levels.append({"price": price, "prices": [price]})
    for lv in levels:
        lv["touches"] = len(lv.pop("prices"))
    return sorted(levels, key=lambda x: -x["touches"])


def linear_regression(xs, ys):
    """Least squares fit. Returns (slope, intercept) or (0, mean)."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0, my
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom
    return slope, my - slope * mx


def clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))
