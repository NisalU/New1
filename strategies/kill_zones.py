"""Institutional Kill Zone detector — time-based session filters.

Institutions execute the majority of their volume in specific windows.
Trading inside a kill zone dramatically increases the probability that a
move is institutional and not random noise.

Kill zones (UTC):
  Asia        00:00 – 04:00   Tokyo / Singapore institutional flow
  London      07:00 – 10:00   Highest institutional activity globally
  NY Open     12:00 – 15:00   Major reversals, news-driven momentum
  London/NY   12:00 – 16:00   Overlap — peak liquidity and volatility
  NY Close    20:00 – 00:00   Position squaring, end-of-day moves

The score is directionally neutral (this module only measures TIMING
quality, not direction). Higher score = better time to trade.
"""
import time as _time
from .helpers import clamp

# Each zone: (name, utc_start_hour, utc_end_hour, score_boost, description)
KILL_ZONES = [
    ("london_open",  7, 10, 0.7, "London Open — highest institutional flow globally"),
    ("ny_open",     12, 15, 0.6, "New York Open — major reversals and momentum"),
    ("london_ny",   12, 16, 0.5, "London/NY Overlap — peak global liquidity"),
    ("asia_open",    0,  4, 0.3, "Asia Open — Tokyo/Singapore institutional flow"),
    ("ny_close",    20, 24, 0.3, "NY Close — position squaring and end-of-day moves"),
]

# Dead zones: avoid new signals in these windows
DEAD_ZONES = [
    ("asia_dead",   4, 7,  "Low-volume Asia dead zone — avoid new signals"),
    ("lunch",      10, 12, "Pre-NY lunch lull — reduced institutional participation"),
    ("post_ny",    16, 20, "Post-NY low volume — retail noise dominates"),
]


def _utc_hour_from_candles(candles):
    """Extract UTC hour from the latest candle timestamp (ms epoch)."""
    try:
        ts_ms = candles[-1]["time"]
        ts_s = ts_ms / 1000 if ts_ms > 1e10 else ts_ms
        return int((_time.gmtime(ts_s).tm_hour))
    except Exception:
        return _time.gmtime().tm_hour


def analyze(candles):
    if not candles:
        return {"score": 0, "reasons": ["No candle data"], "overlays": {}}

    utc_hour = _utc_hour_from_candles(candles)
    score = 0.0
    reasons = []
    active_zones = []
    in_dead_zone = False
    dead_zone_name = ""

    # Check kill zones
    for name, start, end, boost, desc in KILL_ZONES:
        in_zone = start <= utc_hour < end
        if in_zone:
            score += boost
            reasons.append(f"Inside {desc} (UTC {utc_hour:02d}:xx)")
            active_zones.append(name)

    # Check dead zones (penalty for bad timing)
    for name, start, end, desc in DEAD_ZONES:
        if start <= utc_hour < end:
            score -= 0.4
            in_dead_zone = True
            dead_zone_name = desc
            reasons.append(f"Dead zone: {desc} — low institutional participation")

    # No zone at all (neutral hours)
    if not active_zones and not in_dead_zone:
        reasons.append(f"Outside kill zones (UTC {utc_hour:02d}:xx) — reduced institutional flow")
        score -= 0.1

    # London Open bonus: first 30 min is highest probability
    # Approximated by checking if no prior kill zone score was added for london
    overlays = {
        "kill_zones": {
            "utc_hour": utc_hour,
            "active": active_zones,
            "in_dead_zone": in_dead_zone,
            "dead_zone": dead_zone_name if in_dead_zone else None,
            "quality": (
                "prime" if score >= 0.6
                else "good" if score >= 0.3
                else "neutral" if score >= 0
                else "poor"
            ),
        }
    }

    if not reasons:
        reasons.append("Kill zone timing neutral")

    return {"score": clamp(score, -1.0, 1.0), "reasons": reasons, "overlays": overlays}
