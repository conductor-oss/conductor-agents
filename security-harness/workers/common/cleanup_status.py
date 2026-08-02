"""Final cleanup status: CLEAN / RETAINED / UNRESOLVED (PLAN_V3 Phase 5.5).

The cleanup workers (`httptool.cleanup_resources` + `httptool.sweep_resources`) delete tagged
synthetic artifacts and report what was removed vs. what residue remains. This module reduces that
to a single, honest terminal status so a report never leaves cleanup ambiguous:

  - ``UNRESOLVED`` -- residue could not be removed (a created artifact may still exist). Always wins:
    an operator must act, regardless of intent.
  - ``RETAINED``   -- nothing failed, but artifacts were intentionally kept (``--leave-evidence``).
  - ``CLEAN``      -- no residue, nothing retained; the environment was left as found.

NOTE (remaining runtime work, tracked in PLAN_V3 §4 Phase 5.5): a truly rigorous CLEAN also requires
an INDEPENDENT list/GET absence re-check after delete (a 200-delete is not proof of absence). That
re-check is a live-request step; this module computes the status from the cleanup ledger it is given.

Pure logic, unit-tested (`tests/test_cleanup_status.py`).
"""

from __future__ import annotations

CLEAN = "CLEAN"
RETAINED = "RETAINED"
UNRESOLVED = "UNRESOLVED"


def finalize(cleanup: dict | None) -> str:
    """Reduce a cleanup ledger ({deleted, residue, retained, leave_evidence, absence_verified?})
    to one terminal status. Residue -> UNRESOLVED (wins); else intentional retention -> RETAINED;
    else CLEAN."""
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    residue = cleanup.get("residue") or []
    retained = cleanup.get("retained") or []
    leave_evidence = bool(cleanup.get("leave_evidence"))
    if residue:
        return UNRESOLVED
    if leave_evidence or retained:
        return RETAINED
    return CLEAN


def summarize(cleanup: dict | None) -> dict:
    """Attach the terminal status + a one-line human summary to the cleanup ledger (non-mutating)."""
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    status = finalize(cleanup)
    deleted = len(cleanup.get("deleted") or [])
    residue = len(cleanup.get("residue") or [])
    retained = len(cleanup.get("retained") or [])
    if status == UNRESOLVED:
        line = f"UNRESOLVED: {residue} artifact(s) could not be removed and may still exist"
    elif status == RETAINED:
        line = f"RETAINED: {retained} tagged artifact(s) intentionally kept (leave-evidence)"
    else:
        line = f"CLEAN: {deleted} artifact(s) removed, no residue"
    return {**cleanup, "status": status, "status_summary": line}
