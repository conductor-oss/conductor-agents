"""Conductor tasks for verification discovery and exact-SHA local checks."""

from __future__ import annotations

import os
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import check_execution, plan_validation, polling, test_plan, verification
from common.progress import ProgressReporter
from common.results import fail, ok
from common.task_flight import TaskFlight

# Conductor may redeliver a still-SCHEDULED task before this worker's final
# result is posted (confirmed live -- see common/task_flight.py). A commit +
# real command execution is not safe to run twice against the same checkout,
# so concurrent/duplicate deliveries coalesce onto one primary execution.
_COMMIT_VERIFY_FLIGHT = TaskFlight()


@worker_task(task_definition_name="verification_health")
def verification_health(task):
    """Expose whether this isolated verification worker is actually polling."""
    modules = {part.strip() for part in os.environ.get("WORKER_MODULES", "").split(",") if part.strip()}
    report = check_execution.runtime_health_report()
    worker_guards = polling.registered_worker_guard_report()
    report["workerGuards"] = worker_guards
    report["healthy"] = bool(
        report["healthy"] and report["idleSleepInhibitionActive"] and worker_guards["healthy"]
    )
    return ok(task, {**report, "workerRole": "verification",
                     "isolatedWorker": "verification" in modules, "modules": sorted(modules)},
              ["[verification_health] verification worker is polling"])


@worker_task(task_definition_name="verification_discover")
def verification_discover(task):
    """Inspect a candidate parallel plan and return actionable replan feedback.

    Despite the historical task name, this task does not discover, select, or
    execute tests.  Its only concern is whether parallel tasks are safe to run.
    """
    i = task.input_data or {}
    result = plan_validation.inspect_subtasks(
        i.get("subtasks"), i.get("repoPath"),
        current=i.get("currentSubtasks"), exhausted=bool(i.get("exhausted")))
    return ok(task, result, [
        f"[verification_discover] planningState={result['planningState']} issues={len(result['issues'])}"
    ])


def _test_mode(value: object) -> str:
    """Resolve the discovery mode, defaulting to the historical targeted scope."""
    mode = str(value or "targeted").strip().lower()
    if mode not in {"targeted", "full"}:
        raise verification.VerificationBlocked(
            f"testMode must be 'targeted' or 'full', not {mode!r}")
    return mode


# Discovery is filesystem reads plus one `git diff-tree`; it is I/O bound and
# cheap, so several can overlap comfortably.
@worker_task(task_definition_name="test_discover", thread_count=4)
def test_discover(task):
    """Discover the test plan for a candidate, targeted at its diff or repository-wide."""
    i = task.input_data or {}
    try:
        mode = _test_mode(i.get("testMode"))
        # Heavy suites and browser suites are only ever reachable through an
        # explicit full-mode request, never inferred, because relaxing either
        # filter turns a documented command in a repository guide into an
        # execution vector.
        allow_heavy = mode == "full" and bool(i.get("allowHeavySuites"))
        include_browser_tests = mode == "full" and bool(i.get("includeBrowserTests"))
        discovered = verification.discover_commands(
            str(i["repoPath"]), i.get("candidateCommit"), mode=mode, allow_heavy=allow_heavy,
            include_browser_tests=include_browser_tests)
        state = "passed" if discovered.get("executionOutcome") == "no_tests_required" else "discovered"
        # The write scope a repair may use: a declared override wins, else what
        # discovery resolved. Returning it here removes a whole workflow task.
        discovered["repairScope"] = test_plan.repair_scope(
            i.get("allowedWriteRoots"), discovered.get("repairRoots"))
        return ok(task, {"verificationState": state, "executionOutcome": "discovered", **discovered},
                  [f"[test_discover] mode={mode} {discovered['selection']} "
                   f"candidates={len(discovered['candidates'])} "
                   f"rejected={len(discovered.get('rejectedCandidates') or [])}"])
    except verification.VerificationBlocked as exc:
        # allowAgentAuthoredTests' shape check needs the real candidate's
        # changed paths even on a block -- this is precisely the outcome that
        # gates authoring, so losing them here silently rejects every
        # authored test regardless of its content.
        return ok(task, {"verificationState": "blocked", "executionOutcome": "configuration_blocked",
                         "reason": str(exc), "candidates": [],
                         "changedPaths": list(getattr(exc, "changed_paths", None) or []),
                         "repairScope": test_plan.repair_scope(i.get("allowedWriteRoots"), [])},
                  [f"[test_discover] BLOCKED: {exc}"])
    except Exception as exc:
        reason = f"test discovery infrastructure error: {exc}"
        return ok(task, {"verificationState": "blocked", "executionOutcome": "infra_blocked",
                         "reason": reason, "candidates": [],
                         "repairScope": test_plan.repair_scope(i.get("allowedWriteRoots"), [])},
                  [f"[test_discover] BLOCKED: {reason}"])


# A parallel fan-out asks for one verification per subtask, and at thread_count
# 1 they queue behind each other and serialize the whole phase. Each run makes
# its own disposable clone and isolated environment, so they are disk-isolated;
# the ceiling is CPU and memory, since every run can start a real build
# toolchain on a host that is already driving coding agents.
@worker_task(task_definition_name="test_run", thread_count=3)
def test_run(task):
    i = task.input_data or {}
    candidate = str(i.get("candidateCommit") or "").strip()
    try:
        if not candidate:
            raise verification.VerificationBlocked("candidateCommit is required")
        discovery_outcome = str(i.get("discoveryOutcome") or "discovered")
        if discovery_outcome == "no_tests_required":
            return ok(task, {"verificationState": "passed", "executionOutcome": "no_tests_required",
                             "candidateCommit": candidate, "commands": [],
                             "coverage": "not-applicable", "affectedUnits": [],
                             "reason": "candidate changes only documentation or non-executable files"},
                      ["[test_run] no tests required for this candidate"])
        if discovery_outcome != "discovered":
            raise verification.VerificationBlocked(str(i.get("discoveryReason") or
                                                        "verification discovery did not produce a runnable plan"))
        commands = i.get("commands")
        if not isinstance(commands, list) or not commands:
            raise verification.VerificationBlocked("verification discovery produced no runnable commands")
        mode = _test_mode(i.get("testMode"))
        allow_heavy = mode == "full" and bool(i.get("allowHeavySuites"))
        # A real build/test command can run for a very long time as a single
        # blocking call (a bare `pytest` full-suite run has taken over an hour
        # on this exact task type), during which Conductor sees nothing but an
        # opaque IN_PROGRESS. Push periodic snapshots the same way coding_agent
        # does, so the task never looks frozen and never silently outlives its
        # response timeout for lack of a callback.
        reporter = ProgressReporter(task, heartbeat_s=10.0).start()
        try:
            outcome = verification.verify_candidate(
                str(i["repoPath"]), candidate, commands,
                scope="repository" if mode == "full" else "changed", allow_heavy=allow_heavy,
                on_progress=reporter.update)
        finally:
            reporter.stop()
        outcome = {**outcome, "testMode": mode}
        failed = [item for item in outcome["commands"] if item["exitCode"] != 0]
        logs = [f"[test_run] mode={mode} candidate={outcome['candidateCommit'][:12]} "
                f"state={outcome['verificationState']}"]
        if failed:
            logs.append("[test_run] failed: " + " ".join(failed[0]["argv"]) + "\n" + failed[0]["output"][-4000:])
        return ok(task, outcome, logs)
    except verification.VerificationBlocked as exc:
        return ok(task, {"verificationState": "blocked", "executionOutcome": "configuration_blocked",
                         "reason": str(exc), "commands": []},
                  [f"[test_run] BLOCKED: {exc}"])
    except Exception as exc:
        reason = f"verification execution infrastructure error: {exc}"
        return ok(task, {"verificationState": "blocked", "executionOutcome": "infra_blocked", "candidateCommit": candidate,
                         "reason": reason, "commands": []},
                  [f"[test_run] BLOCKED: {reason}"])


@worker_task(task_definition_name="test_plan_resolve", thread_count=4)
def test_plan_resolve(task):
    """Choose the command plan for one candidate and name its authority."""
    i = task.input_data or {}
    out = test_plan.resolve_plan(
        discovered=i.get("discovered"), prior=i.get("priorVerification"),
        user=i.get("testCommands"), discovery_outcome=i.get("discoveryOutcome"),
        discovery_reason=i.get("discoveryReason"), selection=i.get("selection"),
        template=i.get("testPlanTemplate"), agent=i.get("agentCommands"))
    template_source = str(i.get("testPlanTemplateSource") or "").strip()
    logs = [f"[test_plan_resolve] {out['commandSource']} "
            f"commands={len(out['commands'])} outcome={out['discoveryOutcome']}"]
    if out["commandSource"] == "user_template" and not i.get("testCommands") and template_source:
        logs.append(f"[test_plan_resolve] testPlanTemplate supplied by: {template_source}")
    return ok(task, out, logs)


@worker_task(task_definition_name="test_agent_plan_resolve", thread_count=4)
def test_agent_plan_resolve(task):
    """Validate a coding agent's last-resort test-command proposal.

    Only reached when ``allowAgentTestPlan`` is true and deterministic
    discovery could not map the candidate's changed files. This is the
    strictest layer in the precedence order, and its output is carried
    forward as an obligation rather than re-invoked after every repair.
    """
    i = task.input_data or {}
    try:
        mode = _test_mode(i.get("testMode"))
        root = Path(str(i["repoPath"])).resolve()
        changed_paths = i.get("changedPaths")
        candidates = verification.validate_agent_argv(
            i.get("proposal"), mode=mode, root=root,
            changed_paths=changed_paths if isinstance(changed_paths, list) else [])
        repair_roots = sorted({path for candidate in candidates
                              for path in candidate.get("repairRoots", [])})
        return ok(task, {"verificationState": "discovered", "executionOutcome": "discovered",
                         "candidates": candidates, "selection": "agent-proposal",
                         "coverage": "agent-asserted",
                         "affectedUnits": [c["affectedUnit"] for c in candidates],
                         "repairRoots": repair_roots},
                  [f"[test_agent_plan_resolve] accepted {len(candidates)} agent-proposed command(s)"])
    except verification.VerificationBlocked as exc:
        return ok(task, {"verificationState": "blocked", "executionOutcome": "configuration_blocked",
                         "reason": str(exc), "candidates": [], "repairRoots": []},
                  [f"[test_agent_plan_resolve] BLOCKED: {exc}"])
    except Exception as exc:
        reason = f"agent plan validation infrastructure error: {exc}"
        return ok(task, {"verificationState": "blocked", "executionOutcome": "infra_blocked",
                         "reason": reason, "candidates": [], "repairRoots": []},
                  [f"[test_agent_plan_resolve] BLOCKED: {reason}"])


@worker_task(task_definition_name="test_authored_test_validate", thread_count=4)
def test_authored_test_validate(task):
    """Pre-check an agent-authored test file before spending a red/green run on it.

    Only reached when allowAgentAuthoredTests is true and the read-only
    propose_commands search also found nothing. Read-only itself: inspects
    the working tree the author_missing_test agent step just left dirty, but
    makes no mutation.
    """
    i = task.input_data or {}
    try:
        changed_paths = i.get("changedPaths")
        agent_touched_paths = i.get("agentTouchedPaths")
        result = verification.validate_authored_test_shape(
            str(i["repoPath"]), candidate_commit=str(i["candidateCommit"]),
            changed_paths=changed_paths if isinstance(changed_paths, list) else [],
            agent_touched_paths=agent_touched_paths if isinstance(agent_touched_paths, list) else None)
        return ok(task, result,
                  [f"[test_authored_test_validate] accepted={result['accepted']} "
                   f"path={result.get('authoredPath') or '(none)'} reason={result.get('reason') or ''}"])
    except Exception as exc:
        reason = f"authored-test shape validation infrastructure error: {exc}"
        return ok(task, {"accepted": False, "reason": reason, "authoredPath": "",
                         "touchedPaths": [], "matchedIdentifier": ""},
                  [f"[test_authored_test_validate] BLOCKED: {reason}"])


@worker_task(task_definition_name="test_authored_test_discard", thread_count=4)
def test_authored_test_discard(task):
    """Undo a rejected agent-authored test attempt so it never rides along on a
    later, unrelated repair commit."""
    i = task.input_data or {}
    try:
        paths = i.get("paths")
        result = verification.discard_authored_test_attempt(
            str(i["repoPath"]), paths if isinstance(paths, list) else [])
        return ok(task, result, [f"[test_authored_test_discard] discarded={result['discarded']}"])
    except Exception as exc:
        reason = f"authored-test discard infrastructure error: {exc}"
        return ok(task, {"discarded": []}, [f"[test_authored_test_discard] BLOCKED: {reason}"])


@worker_task(task_definition_name="test_authored_test_commit_verify", thread_count=3)
def test_authored_test_commit_verify(task):
    """Commit a shape-validated authored test, discover it, and red/green-check it.

    One task replaces what used to be three (commit, test_discover,
    test_authored_test_redgreen) plus a SWITCH between them: the intermediate
    states have no independent value to test_cycle, only the final
    accepted/rejected verdict does. See
    verification.commit_and_verify_authored_test for the exact mechanism,
    including the automatic rollback on rejection.

    A real commit plus command execution is not safe to run twice for the
    same task_id (confirmed live: a redelivered duplicate raced the primary's
    git commit and its "no committable change" result overwrote the primary's
    real "accepted" one) -- coalesced through _COMMIT_VERIFY_FLIGHT.
    """
    def _run():
        i = task.input_data or {}
        reporter = ProgressReporter(task, heartbeat_s=10.0).start()
        try:
            try:
                result = verification.commit_and_verify_authored_test(
                    str(i["repoPath"]), pre_candidate_commit=str(i["preCandidateCommit"]),
                    message=str(i.get("message") or "test_cycle: agent-authored test for previously unmapped changes"),
                    test_mode=str(i.get("testMode") or "targeted"),
                    allow_heavy_suites=bool(i.get("allowHeavySuites")),
                    include_browser_tests=bool(i.get("includeBrowserTests")),
                    on_progress=reporter.update)
                return ok(task, result,
                          [f"[test_authored_test_commit_verify] accepted={result['accepted']} "
                           f"reason={result.get('reason') or ''}"])
            except Exception as exc:
                reason = f"authored-test commit/verify infrastructure error: {exc}"
                return ok(task, {"accepted": False, "reason": reason, "commit": i.get("preCandidateCommit", ""),
                                 "repairRoots": [], "commands": []},
                          [f"[test_authored_test_commit_verify] BLOCKED: {reason}"])
        finally:
            reporter.stop()

    return _COMMIT_VERIFY_FLIGHT.run(task, _run)


@worker_task(task_definition_name="test_repair_record", thread_count=4)
def test_repair_record(task):
    """Fold one fix attempt into a new candidate plus an auditable report."""
    i = task.input_data or {}
    out = test_plan.record_repair(current_candidate=i.get("currentCandidate"),
                                  agent=i.get("agent"), commit=i.get("commit"))
    return ok(task, out, [f"[test_repair_record] advanced={out['report']['persisted']} "
                          f"candidate={str(out['candidate'])[:12]}"])


@worker_task(task_definition_name="test_cycle_progress", thread_count=4)
def test_cycle_progress(task):
    """Append one attempt to the fix history and report whether it moved."""
    i = task.input_data or {}
    out = test_plan.record_progress(reports=i.get("reports"), report=i.get("report"),
                                    verification=i.get("verification"), runs=i.get("runs"))
    return ok(task, out, [f"[test_cycle_progress] attempts={out['attempts']} "
                          f"runs={out['runCount']} advanced={out['candidateAdvanced']}"])


@worker_task(task_definition_name="test_cycle_outcome", thread_count=4)
def test_cycle_outcome(task):
    """Map every terminal condition onto one named state for the caller."""
    i = task.input_data or {}
    out = test_plan.cycle_outcome(
        outcome=i.get("outcome"), candidate=i.get("candidate"), advanced=i.get("advanced"),
        attempts=i.get("attempts"), runs=i.get("runs"), source=i.get("commandSource"),
        verification=i.get("verification"), mode=i.get("testMode"))
    return ok(task, out, [f"[test_cycle_outcome] {out['testCycleState']}"])
