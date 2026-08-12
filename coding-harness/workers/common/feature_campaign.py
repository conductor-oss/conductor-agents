"""Pure decision logic for feature_campaign."""

from __future__ import annotations


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def is_implementation_done(*, outcome: object, remaining: object) -> bool:
    """The implementation loop is done when nothing is left, or a terminal outcome landed."""
    if isinstance(remaining, list) and not remaining:
        return True
    return isinstance(outcome, str) and bool(outcome) and outcome != "running"


def resolve_campaign_publication_plan(*, outcome: object, requested_draft: object,
                                      test_state: object, tests_passed: object,
                                      tested: object, committed: object,
                                      agent_authored_test: object = False) -> dict:
    """Decide the publish head and draft state after the final test cycle.

    ``agent_authored_test`` forces draft even when tests passed: a red/green
    check proves the new test isn't vacuous, but a human should still see
    that no pre-existing test covered this change before the PR is treated
    as fully verified (defense in depth, not a substitute for the check).
    """
    tested_text = _text(tested)
    head = tested_text if not _blank(tested_text) else committed
    authored = agent_authored_test is True
    note = ("\n\n> This PR includes a test file authored by a coding agent (no pre-existing "
            "test covered these changes); it was verified with a red/green check before "
            "being accepted." if authored else "")
    return {
        "draft": outcome != "verified" or requested_draft is True or tests_passed is not True or authored,
        "outcome": outcome if isinstance(outcome, str) and outcome else "incomplete",
        "head": head,
        "testCycleState": test_state if isinstance(test_state, str) else "not_run",
        "testsPassed": tests_passed is True,
        "agentAuthoredTest": authored,
        "agentAuthoredTestNote": note,
    }
