"""Resilience knee analysis (ROADMAP E3) — safe-by-construction availability testing.

We never try to cause an outage. Instead we ramp a SMALL, bounded amount of concurrency
and measure the *derivative* of latency/error vs load: the goal is to locate the "knee"
(the concurrency at which p95 latency or error-rate inflects) and STOP there. Pure logic
so it is unit-testable; the load worker feeds it measured steps and aborts at the knee.
"""

from __future__ import annotations

# A step is degraded if errors exceed this, or p95 latency exceeds this multiple of baseline.
_ERR_DEGRADED = 0.2
_LAT_DEGRADED_X = 3.0
# Hard abort thresholds (stop pushing load immediately — protect the target).
_ERR_ABORT = 0.5
_LAT_ABORT_X = 10.0


def step_verdict(step: dict, baseline_ms: float) -> dict:
    """Classify one measured load step. Returns {degraded, abort, reason}."""
    err = float(step.get("error_rate") or 0.0)
    p95 = float(step.get("p95_ms") or 0.0)
    ratio = (p95 / baseline_ms) if baseline_ms > 0 else 1.0
    if err >= _ERR_ABORT or ratio >= _LAT_ABORT_X:
        return {"degraded": True, "abort": True,
                "reason": f"abort: error_rate={err:.2f} p95={p95:.0f}ms ({ratio:.1f}x baseline)"}
    if err >= _ERR_DEGRADED or ratio >= _LAT_DEGRADED_X:
        return {"degraded": True, "abort": False,
                "reason": f"degraded: error_rate={err:.2f} p95={p95:.0f}ms ({ratio:.1f}x baseline)"}
    return {"degraded": False, "abort": False, "reason": ""}


def analyze(steps: list) -> dict:
    """Given measured steps (each {concurrency, p95_ms, error_rate}) sorted by concurrency,
    return {baseline_ms, knee_at, degraded, summary}. ``knee_at`` is the first concurrency
    that degraded (None if the bounded ramp stayed flat)."""
    steps = [s for s in (steps or []) if isinstance(s, dict)]
    if not steps:
        return {"baseline_ms": 0.0, "knee_at": None, "degraded": False, "summary": "no load steps"}
    steps = sorted(steps, key=lambda s: s.get("concurrency", 0))
    baseline = float(steps[0].get("p95_ms") or 0.0) or 1.0
    knee_at = None
    for s in steps:
        v = step_verdict(s, baseline)
        if v["degraded"]:
            knee_at = s.get("concurrency")
            break
    return {
        "baseline_ms": baseline,
        "knee_at": knee_at,
        "degraded": knee_at is not None,
        "max_concurrency": max(s.get("concurrency", 0) for s in steps),
        "summary": (f"latency/error knee at concurrency={knee_at}" if knee_at is not None
                    else f"no degradation through concurrency={steps[-1].get('concurrency')}"),
    }
