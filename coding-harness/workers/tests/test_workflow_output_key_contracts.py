"""Prove every ``${ref.output.KEY}`` names a key its producer actually returns.

The failure this guards against is silent.  Conductor resolves a reference to a
key the producer never emitted as ``null`` rather than erroring, so a typo'd
path produces a well-formed-but-empty payload that flows all the way to a human
gate and to GitHub.  ``pr_review`` shipped with
``${normalize_review.output.result}`` while ``pr_review_summary`` returns
``{summary, verdict, comments}`` flat: every checkpoint rendered "Verdict: None
/ Inline comments (0)" and an approval would have published a review with the
blocking comments stripped out.

Coverage is deliberately limited to producers whose output keys are decidable
offline -- the pure normalizers in ``workers/common`` -- rather than every
worker.  Those are exactly the tasks whose output is wired straight into
SET_VARIABLE state and WAIT drafts, which is where a dropped key is invisible.
``scripts/prove_task_output_contracts.py`` covers the rest from live execution
history; this test is the part that can fail in CI before a run is ever
started.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from common import gate_decision, pr_review


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
REFERENCE = re.compile(r"\$\{([A-Za-z0-9_-]+)\.output\.([A-Za-z0-9_]+)")

_REVIEW = {"summary": "Changes requested.", "verdict": "request_changes",
           "comments": [{"severity": "blocking", "path": "a.py", "line": 1, "body": "fix"}]}


def _output_keys() -> dict[str, set[str]]:
    """Task-definition name -> the top-level output keys its worker emits.

    Derived by calling the normalizer rather than restating its keys, so the
    expectation cannot drift away from the implementation.
    """
    return {
        "pr_review_summary": set(gate_decision.summarize_review(_REVIEW)),
        "review_decision": {"action", "requested", "feedback", "review"},
        "pr_decision": {"action", "requested", "feedback", "title", "body"},
        "address_decision": {"action", "requested", "feedback", "body"},
        "pr_review_investigation": set(pr_review.normalize_investigation(
            structured={"answer": "a", "review": _REVIEW}, status="COMPLETED", error="",
            session_id="s", prior_session_id="s", question="q", prior_review=_REVIEW,
            history=[], prior_tokens=0, tokens=0, prior_cost=0, cost=0)),
    }


def _walk(value):
    if isinstance(value, dict):
        if "taskReferenceName" in value:
            yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.json")), ids=lambda p: p.stem)
def test_output_references_name_keys_the_producer_emits(path: Path):
    workflow = json.loads(path.read_text(encoding="utf-8"))
    known = _output_keys()
    tasks = list(_walk(workflow))
    producer = {task["taskReferenceName"]: task.get("name") for task in tasks}

    bad: list[str] = []
    for task in tasks:
        for text in _strings(task.get("inputParameters") or {}):
            for reference, key in REFERENCE.findall(text):
                keys = known.get(producer.get(reference) or "")
                if keys is not None and key not in keys:
                    bad.append(
                        f"{task['taskReferenceName']} reads ${{{reference}.output.{key}}}, but "
                        f"{producer[reference]} emits only {sorted(keys)}")
    assert not bad, f"{path.name}: unresolvable output references\n  " + "\n  ".join(bad)


def test_the_check_detects_the_pr_review_regression_it_was_written_for():
    """A mutation probe: restore the shipped bug and prove this test rejects it."""
    workflow = json.loads((WORKFLOWS / "pr_review.json").read_text(encoding="utf-8"))
    capture = next(t for t in _walk(workflow) if t["taskReferenceName"] == "capture_initial_review")
    assert capture["inputParameters"]["currentReview"] == "${normalize_review.output}"

    known = _output_keys()
    reference, key = REFERENCE.findall("${normalize_review.output.result}")[0]
    assert reference == "normalize_review"
    assert key not in known["pr_review_summary"]
