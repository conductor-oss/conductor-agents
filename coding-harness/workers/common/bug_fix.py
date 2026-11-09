"""Reproduction-first bug-fix logic (roadmap Phase 4, G2) — pure decisions.

A bug fix is only trustworthy if it is proven: derive a **failing** test that reproduces the bug,
confirm it is **red** at the parent commit, fix the code, and prove it is **green** at the fix — and
commit the repro as a regression guard. "Cannot reproduce" is an honest outcome, never a silent
"fixed" (roadmap G2). This module is the pure gate + verdict; the thin workers and the workflow wire
it to the existing ``code_subtask`` (repro + fix agents), ``test_run`` (raw red/green), and
``test_cycle`` (the green gate).
"""

from __future__ import annotations


def classify_repro(verification_state: object) -> str:
    """Map a raw ``test_run`` verdict on the repro commit to a reproduction outcome.

    The repro test is authored to FAIL on the buggy code, so a **failed** run *confirms* the bug is
    reproduced; a **passed** run means the test did not capture the bug (cannot reproduce); a
    blocked/no-command run is an honest infrastructure block, not a reproduction."""
    state = str(verification_state or "").strip().lower()
    if state == "failed":
        return "reproduced"
    if state == "passed":
        return "cannot_reproduce"
    return "blocked"


def repro_gate(verification_state: object) -> dict:
    """Decision for the repro SWITCH: proceed to the fix only when the bug is genuinely reproduced."""
    outcome = classify_repro(verification_state)
    reproduced = outcome == "reproduced"
    if reproduced:
        report = "reproduced: the regression test is red at the parent commit (the bug is real)."
    elif outcome == "cannot_reproduce":
        report = ("cannot_reproduce: the authored test passes on the current code, so it does not "
                  "capture the reported bug — stopping rather than claiming a fix.")
    else:
        report = ("blocked: could not run the repro test (no runnable test command discovered) — "
                  "an honest infrastructure block, not a reproduction.")
    return {"reproduced": reproduced, "outcome": outcome, "report": report}


def fix_outcome(*, reproduced: object, tests_passed: object, test_state: object = "",
                final_head: str = "", repro_test_tampered: object = False) -> dict:
    """Fold the run into an honest verdict. FIXED iff the bug was reproduced (red at parent) AND the
    branch is now green (the repro test, committed as a regression guard, passes at the fix) AND the
    fix did not tamper with the repro test (H13 — the author must not weaken the oracle to pass)."""
    reproduced = reproduced is True
    passed = tests_passed is True
    tampered = repro_test_tampered is True
    head = str(final_head or "")
    if not reproduced:
        return {"fixed": False, "verdict": "CANNOT_REPRODUCE",
                "report": ("CANNOT_REPRODUCE: no failing test captured the reported bug at the parent "
                           "commit, so there is nothing to fix and prove. Head " + head[:12] + ".")}
    if tampered:
        return {"fixed": False, "verdict": "NOT_FIXED",
                "report": ("NOT_FIXED: the fix modified the regression test after it was committed red "
                           "(oracle tampering, H13) — a green under a weakened test is not a fix. "
                           f"Head {head[:12]}.")}
    if passed:
        return {"fixed": True, "verdict": "FIXED",
                "report": ("FIXED: the regression test was red at the parent commit and is green at the "
                           f"fix (test_cycle={test_state}); the repro is committed as a guard and was "
                           f"not modified by the fix. Head {head[:12]}.")}
    return {"fixed": False, "verdict": "NOT_FIXED",
            "report": ("NOT_FIXED: the bug was reproduced (red at parent) but the branch is not green "
                       f"after the fix (test_cycle={test_state}). Head {head[:12]}.")}
