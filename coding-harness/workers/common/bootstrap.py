"""Greenfield on-ramp logic (roadmap Phase 5, G3) — pure outcome folding.

``project_bootstrap`` turns a one-paragraph description + an empty dir into a **buildable, tested
skeleton** (with the verification command documented so ``test_cycle`` works from run one), then hands
off to the team to add the first real feature — in one driven sequence. The skeleton being green is a
hard gate: a skeleton that does not build or has no runnable test is not a hand-off-ready project.
"""

from __future__ import annotations


def bootstrap_outcome(*, skeleton_green: object, feature_delivered: object = None,
                      feature_verdict: object = "", final_head: str = "") -> dict:
    """Fold the two phases into an honest verdict. The skeleton must be green (buildable + tested from
    run one) before the hand-off counts; then the first feature's own delivery decides the rest."""
    head = str(final_head or "")
    if skeleton_green is not True:
        return {"bootstrapped": False, "verdict": "SKELETON_NOT_GREEN",
                "report": ("SKELETON_NOT_GREEN: the scaffolded skeleton is not buildable/green under its "
                           "own documented test command, so it is not ready to hand to the team. "
                           f"Head {head[:12]}.")}
    if feature_delivered is True:
        return {"bootstrapped": True, "verdict": "BOOTSTRAPPED",
                "report": ("BOOTSTRAPPED: a tested green skeleton was scaffolded from the description and "
                           "the team added the first feature on top (verified green), in one sequence. "
                           f"Head {head[:12]}.")}
    if feature_delivered is None:
        return {"bootstrapped": True, "verdict": "SKELETON_READY",
                "report": ("SKELETON_READY: a tested green skeleton was scaffolded from the description "
                           f"and is ready for the team; no first feature was requested. Head {head[:12]}.")}
    return {"bootstrapped": False, "verdict": "FEATURE_INCOMPLETE",
            "report": ("FEATURE_INCOMPLETE: the skeleton is green but the first feature did not fully "
                       f"land (downstream verdict: {feature_verdict}). Head {head[:12]}.")}
