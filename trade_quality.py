"""Trade quality engine — grades a setup (A+ / A / B / Reject) instead of
producing another confluence number.

Five sub-qualities are rated independently from the underlying market
structure (not from a single blended score) and combined into one grade.
This is used twice per analysis:

1. A "structural" pre-check (no trade plan yet) fed to the AI as context,
   so it knows how strong the surrounding conditions are before it forms a
   thesis.
2. A final grade computed on the AI's *actual* entry/stop/tp1 once it has
   published a call, returned to the dashboard as `trade_quality`.
"""
from strategies.helpers import atr

import config

_LOC_SCORE = {"Poor": 0, "Fair": 1, "Good": 2, "Excellent": 3}
_STRUCT_SCORE = {"Unclear": 0, "Trending": 1, "Strong": 2}
_LIQ_SCORE = {"Weak": 0, "Present": 1, "Strong": 2}
_OF_SCORE = {"Weak": 0, "Moderate": 1, "Strong": 2}
_RISK_SCORE = {"Unrated": 0, "Poor": 0, "Good": 2}


def _location_quality(overlays, price, a):
    near_ob = any(
        ob["bottom"] - a * 0.3 <= price <= ob["top"] + a * 0.3
        for ob in overlays.get("order_blocks") or []
    )
    near_fvg = any(f["bottom"] <= price <= f["top"] for f in overlays.get("fvgs") or [])
    near_sr = any(
        abs(lv["price"] - price) < a * 1.2
        for lv in (overlays.get("support") or []) + (overlays.get("resistance") or [])
    )
    hits = sum([near_ob, near_fvg, near_sr])
    return ["Poor", "Fair", "Good", "Excellent"][min(hits, 3)]


def _structure_quality(overlays):
    structure = overlays.get("structure") or {}
    if structure.get("events"):
        return "Strong"
    if structure.get("trend"):
        return "Trending"
    return "Unclear"


def _liquidity_quality(overlays):
    if overlays.get("sweeps"):
        return "Strong"
    if overlays.get("liquidity_pools"):
        return "Present"
    return "Weak"


def _orderflow_quality(overlays):
    div = overlays.get("divergence")
    cvd = overlays.get("cvd") or []
    strong_delta = False
    if len(cvd) >= 10:
        recent_slope = cvd[-1]["value"] - cvd[-10]["value"]
        span = max(abs(p["value"]) for p in cvd[-30:]) or 1.0
        strong_delta = abs(recent_slope) / span > 0.15
    if div or strong_delta:
        return "Strong"
    if cvd:
        return "Moderate"
    return "Weak"


def _risk_quality(plan):
    if not plan:
        return "Unrated"
    entry, stop, tp1 = plan.get("entry"), plan.get("stop"), plan.get("tp1")
    if entry is None or stop is None or tp1 is None:
        return "Unrated"
    risk = abs(entry - stop)
    if risk <= 0:
        return "Poor"
    rr = abs(tp1 - entry) / risk
    return "Good" if rr >= config.AI_MIN_RISK_REWARD else "Poor"


def grade(analysis, plan=None, regime=None):
    """`plan` is optional: {entry, stop, tp1}. Without it, only the
    structural (location/structure/liquidity/orderflow) qualities are rated
    and risk_quality is "Unrated"."""
    overlays = analysis.get("overlays", {})
    price = analysis["price"]
    a = atr(analysis["candles"]) or price * 0.005

    location_quality = _location_quality(overlays, price, a)
    structure_quality = _structure_quality(overlays)
    liquidity_quality = _liquidity_quality(overlays)
    orderflow_quality = _orderflow_quality(overlays)
    risk_quality = _risk_quality(plan)

    score = (
        _LOC_SCORE[location_quality]
        + _STRUCT_SCORE[structure_quality]
        + _LIQ_SCORE[liquidity_quality]
        + _OF_SCORE[orderflow_quality]
        + _RISK_SCORE[risk_quality]
    )

    if regime is not None and not regime.get("tradeable", True):
        grade_label = "Reject"
    elif score >= 9:
        grade_label = "A+"
    elif score >= 7:
        grade_label = "A"
    elif score >= 4:
        grade_label = "B"
    else:
        grade_label = "Reject"

    return {
        "grade": grade_label,
        "score": score,
        "location_quality": location_quality,
        "structure_quality": structure_quality,
        "liquidity_quality": liquidity_quality,
        "orderflow_quality": orderflow_quality,
        "risk_quality": risk_quality,
    }
