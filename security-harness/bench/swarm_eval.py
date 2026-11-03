"""Swarm evaluation (design/SWARM.md sections 7-8): is wider actually better per dollar?

Pure logic so it is unit-testable without a live swarm. The policy: raise wave width only while the
measured confirmed-findings-per-dollar curve is still climbing. "Confirmed" means oracle-verified,
refuter-survived findings -- never raw candidates.
"""

from __future__ import annotations


def confirmed_per_dollar(confirmed_count: int, cost_usd: float) -> float:
    """Headline efficiency metric. Zero cost -> 0.0 (undefined yield is not infinite value)."""
    try:
        c = max(0, int(confirmed_count)); cost = float(cost_usd)
    except (TypeError, ValueError):
        return 0.0
    return round(c / cost, 4) if cost > 0 else 0.0


def width_yield_curve(runs: list) -> list:
    """Turn raw run results into a width-vs-yield curve, ascending by width.

    ``runs`` is a list of ``{width, confirmed, cost}``. Runs at the same width are aggregated
    (confirmed and cost summed) before the ratio is taken."""
    agg: dict[int, dict] = {}
    for r in runs or []:
        try:
            w = int(r.get("width"))
        except (TypeError, ValueError):
            continue
        slot = agg.setdefault(w, {"confirmed": 0, "cost": 0.0})
        slot["confirmed"] += max(0, int(r.get("confirmed", 0) or 0))
        slot["cost"] += float(r.get("cost", 0) or 0)
    curve = [{"width": w, "confirmed": v["confirmed"], "cost": round(v["cost"], 4),
              "confirmed_per_dollar": confirmed_per_dollar(v["confirmed"], v["cost"])}
             for w, v in agg.items()]
    curve.sort(key=lambda p: p["width"])
    return curve


def choose_width(curve: list, tolerance: float = 0.0) -> dict:
    """Scale policy: pick the width at the yield plateau -- the widest point whose confirmed-per-dollar
    still improved on the previous one by more than ``tolerance`` (fractional). Beyond it, widening
    buys less per dollar, so the default ceiling sits there.

    Returns ``{width, confirmed_per_dollar, reason}``."""
    curve = sorted(curve or [], key=lambda p: p["width"])
    if not curve:
        return {"width": 0, "confirmed_per_dollar": 0.0, "reason": "no data"}
    best = curve[0]
    for prev, cur in zip(curve, curve[1:]):
        prev_cpd = prev["confirmed_per_dollar"]
        improved = cur["confirmed_per_dollar"] > prev_cpd * (1.0 + tolerance) if prev_cpd > 0 \
            else cur["confirmed_per_dollar"] > 0
        if improved:
            best = cur
        else:
            return {"width": prev["width"], "confirmed_per_dollar": prev["confirmed_per_dollar"],
                    "reason": f"yield plateaued after width {prev['width']}"}
    return {"width": best["width"], "confirmed_per_dollar": best["confirmed_per_dollar"],
            "reason": "yield still climbing at the widest measured point"}
