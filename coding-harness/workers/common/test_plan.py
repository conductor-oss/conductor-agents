"""Pure decision logic for the test_cycle workflow.

This lives in Python rather than in `JSON_JQ_TRANSFORM` expressions because a
throwing jq expression ends the whole workflow FAILED, every expression costs
three fixture cases, and none of it was reachable by an ordinary unit test.
Each function here is total: it accepts whatever a degraded upstream task
returned and always produces a well-formed result.
"""

from __future__ import annotations

import json
from typing import Any

# test_run emits six outcomes: verify_candidate's four plus the two the task
# layer adds. Anything else means the verifier itself did not report.
_RAN = {"tests_passed", "tests_failed_after_fix_budget", "tests_failed_fix_unavailable"}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not _text(value).strip()


def _number(value: object, default: int = 0) -> int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _usable(command: object) -> bool:
    """A command is usable only when every argv element is a non-blank string."""
    argv = _mapping(command).get("argv")
    return (isinstance(argv, list) and bool(argv)
            and all(isinstance(part, str) and part.strip() for part in argv))


def _dedupe(commands: list[dict]) -> list[dict]:
    seen: set[tuple[str, ...]] = set()
    kept: list[dict] = []
    for command in commands:
        key = tuple(command["argv"])
        if key not in seen:
            seen.add(key)
            kept.append(command)
    return kept


def clean_commands(value: object) -> list[dict]:
    """Normalize a prior-verification payload or a bare command list."""
    raw = value
    if isinstance(raw, dict):
        raw = raw.get("commands")
    return _dedupe([command for command in _listed(raw) if _usable(command)])


def user_commands(value: object) -> list[dict]:
    """Lift an operator override into command records.

    Accepts either a bare argv array or a full command object so the input is
    forgiving about how a caller or a template expresses it.
    """
    lifted: list[dict] = []
    for entry in _listed(value):
        if isinstance(entry, list):
            lifted.append({"argv": entry, "source": "user-template", "kind": "user"})
        elif isinstance(entry, dict):
            lifted.append({**entry, "source": entry.get("source") or "user-template",
                           "kind": "user"})
    return _dedupe([command for command in lifted if _usable(command)])


def template_commands(value: object) -> list[dict]:
    """Parse an inline JSON test-plan template into command records.

    Deliberately inline-only: a value starting with ``@`` is the repository
    templating convention for "resolve this from a file in the checkout"
    (``templating.py``'s ``@repo/path`` form), which for a prompt a model
    reads is fine but for argv a worker executes would let a candidate grant
    itself command authority through its own tree. Reject it outright rather
    than resolve it. A blank value, non-string value, or string that is not
    valid JSON all mean "no template" -- this module never raises.
    """
    text = _text(value).strip()
    if not text or text.startswith("@"):
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    return user_commands(parsed)


def resolve_plan(*, discovered: object, prior: object, user: object,
                 discovery_outcome: object, discovery_reason: object,
                 selection: object, template: object = None, agent: object = None) -> dict:
    """Choose the command plan and record which authority supplied it.

    Precedence is the operator override first -- a structured ``testCommands``
    array over an inline ``testPlanTemplate`` string when both are given,
    since the structured form cannot suffer a JSON-parsing surprise -- then
    whatever discovery found, merged with any obligations the caller carried
    in, and only when that is empty, an already-validated agent proposal:
    deterministic evidence first, an agent last resort.
    """
    required = clean_commands(prior)
    supplied = user_commands(user) or template_commands(template)
    from_agent = False
    if supplied:
        commands, source = supplied, "user_template"
    else:
        commands = _dedupe(clean_commands(discovered) + required)
        if _text(selection) == "documented-guide":
            source = "repository_guide"
        elif any(_text(command.get("source")).startswith("repository-config:")
                 for command in commands):
            source = "repository_config"
        elif commands:
            source = "build_system_inference"
        else:
            # discovery and every carried obligation came up empty -- the one
            # case where an already-validated agent proposal is the plan.
            commands = clean_commands(agent)
            from_agent = bool(commands)
            source = "agent_proposal" if from_agent else "none"

    outcome = _text(discovery_outcome)
    if outcome in {"discovered", "no_tests_required"} and commands:
        outcome = "discovered"
    elif (supplied or from_agent) and outcome in {"", "configuration_blocked"}:
        # An operator override or an accepted agent proposal is itself the
        # authority; a repository whose build graph discovery cannot map must
        # still be able to run the given command.
        outcome = "discovered"
    return {"commands": commands, "required": required, "requiredCount": len(required),
            "commandSource": source, "discoveryOutcome": outcome,
            "discoveryReason": _text(discovery_reason)}


def repair_scope(declared: object, discovered: object) -> list[str]:
    """Prefer a declared write scope, else the scope discovery resolved."""
    def usable(value: object) -> list[str]:
        return [item for item in _listed(value) if isinstance(item, str) and item.strip()]
    return usable(declared) or usable(discovered)


def record_repair(*, current_candidate: object, agent: object, commit: object) -> dict:
    """Fold one fix attempt into a new candidate plus an auditable report.

    The candidate only advances on real evidence of a commit: a non-blank SHA,
    at least one staged path, and an explicit non-no-op. Anything weaker leaves
    the previous candidate in place.
    """
    agent_report = _mapping(agent)
    commit_report = _mapping(commit)
    staged = _listed(commit_report.get("stagedPaths"))
    rejected = _listed(commit_report.get("rejectedPaths"))
    denials = _listed(agent_report.get("denials"))

    failures: list[dict] = []
    for turn in _listed(agent_report.get("turns")):
        for command in _listed(_mapping(turn).get("commands")):
            entry = _mapping(command)
            code = entry.get("exitCode", entry.get("exit_code", 0))
            if code:
                failures.append({"command": entry.get("command") or entry.get("cmd") or "",
                                 "exitCode": code, "stderr": entry.get("stderr") or ""})

    sha = commit_report.get("commit")
    advanced = (commit_report.get("noOp") is False and bool(staged)
                and isinstance(sha, str) and not _blank(sha))
    candidate = sha if advanced else current_candidate
    return {
        "candidate": candidate,
        "report": {
            "agentStatus": agent_report.get("status") or "unavailable",
            "agentCompleted": agent_report.get("agentCompleted") is True,
            "stagedPaths": staged, "rejectedPaths": rejected,
            "sandboxDenials": denials, "commandFailures": failures,
            "noOp": commit_report.get("noOp") if isinstance(commit_report.get("noOp"), bool) else True,
            "persisted": advanced,
            "candidateBefore": current_candidate, "candidateAfter": candidate,
            "reason": commit_report.get("reason") or agent_report.get("error") or "",
        },
    }


def record_progress(*, reports: object, report: object, verification: object,
                    runs: object) -> dict:
    """Append one attempt to the history and report whether it moved anything."""
    verified = _mapping(verification)
    entry = {**_mapping(report),
             "verificationState": verified.get("verificationState") or "blocked",
             "executionOutcome": verified.get("executionOutcome") or "infra_blocked",
             "verifiedCandidate": verified.get("candidateCommit")
                                  or _mapping(report).get("candidateAfter")}
    history = _listed(reports) + [entry]
    return {"reports": history, "attempts": len(history),
            # A fix that produced no new commit means re-running would repeat
            # the identical verdict, so the loop stops instead of spending the
            # rest of its budget proving that.
            "candidateAdvanced": _mapping(report).get("persisted") is True,
            "runCount": _number(runs, 1) + 1}


def cycle_outcome(*, outcome: object, candidate: object, advanced: object,
                  attempts: object, runs: object, source: object,
                  verification: object, mode: object) -> dict:
    """Map every terminal condition onto one named state the caller branches on."""
    reported = _text(outcome) or "worker_unavailable"
    has_candidate = not _blank(candidate)
    if reported == "passed":
        state = "tests_passed"
    elif reported == "no_tests_required":
        state = "no_tests_required"
    elif reported == "code_failed":
        state = ("tests_failed_fix_unavailable" if advanced is False
                 else "tests_failed_after_fix_budget")
    elif reported == "configuration_blocked":
        state = "command_discovery_blocked" if has_candidate else "candidate_commit_missing"
    elif reported in {"infra_blocked", "cancelled"}:
        state = "runtime_unavailable"
    else:
        state = "verifier_worker_unavailable"

    commands = _listed(_mapping(verification).get("commands"))
    # A blocked discovery or an absent verifier executed nothing, so reporting a
    # run count would overstate what was proven.
    run_count = _number(runs) if state in _RAN else 0
    fixes = _number(attempts)
    resolved_mode = _text(mode) or "targeted"
    resolved_source = _text(source) or "none"
    return {
        "testCycleState": state,
        "testsRan": state in _RAN,
        "testsPassed": state in {"tests_passed", "no_tests_required"},
        "testRunCount": run_count,
        "fixAttempts": fixes,
        "commandSource": resolved_source,
        "testMode": resolved_mode,
        "commands": commands,
        "testReport": (f"{state}: {resolved_mode} run of {len(commands)} command(s) from "
                       f"{resolved_source} after {run_count} test run(s) and "
                       f"{fixes} fix attempt(s)"),
    }
