"""Conductor tasks for verification discovery and exact-SHA local checks."""

from __future__ import annotations

import os
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import check_execution, plan_validation, polling, test_plan, verification
from common.progress import ProgressReporter
from common.results import ok
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
        state = ("passed" if discovered.get("executionOutcome") in {"no_tests_required", "no_tests_found"}
                 else "discovered")
        # The write scope a repair may use: a declared override wins, else what
        # discovery resolved. Returning it here removes a whole workflow task.
        discovered["repairScope"] = test_plan.repair_scope(
            i.get("allowedWriteRoots"), discovered.get("repairRoots"))
        return ok(task, {"verificationState": state, "executionOutcome": "discovered", **discovered},
                  [f"[test_discover] mode={mode} {discovered['selection']} "
                   f"candidates={len(discovered['candidates'])} "
                   f"rejected={len(discovered.get('rejectedCandidates') or [])}"])
    except verification.VerificationBlocked as exc:
        # Discovery itself never blocks; this is an input the caller wired wrong (no candidate commit,
        # a commit the repository does not have, a path that is not a git worktree).
        return ok(task, {"verificationState": "blocked", "executionOutcome": "invalid_input",
                         "reason": str(exc), "candidates": [],
                         "changedPaths": list(getattr(exc, "changed_paths", None) or []),
                         "repairScope": test_plan.repair_scope(i.get("allowedWriteRoots"), [])},
                  [f"[test_discover] INVALID INPUT: {exc}"])
    except Exception as exc:
        reason = f"test discovery infrastructure error: {exc}"
        return ok(task, {"verificationState": "blocked", "executionOutcome": "infra_blocked",
                         "reason": reason, "candidates": [],
                         "repairScope": test_plan.repair_scope(i.get("allowedWriteRoots"), [])},
                  [f"[test_discover] BLOCKED: {reason}"])


@worker_task(task_definition_name="test_mode_select", thread_count=4)
def test_mode_select(task):
    """Choose how to test a merged change: targeted when it maps to exact tests, else the whole suite."""
    i = task.input_data or {}
    out = verification.integration_test_mode(str(i.get("repoPath") or ""), str(i.get("candidateCommit") or ""))
    return ok(task, out, [f"[test_mode_select] {out['testMode']}: {out['reason']}"])


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
        reason = str(i.get("discoveryReason") or "")
        if discovery_outcome in {"invalid_input", "infra_blocked"}:
            return ok(task, {"verificationState": "blocked", "executionOutcome": discovery_outcome,
                             "candidateCommit": candidate, "commands": [], "reason": reason},
                      [f"[test_run] not run: {discovery_outcome}: {reason}"])
        commands = i.get("commands")
        if discovery_outcome in {"no_tests_required", "no_tests_found"} or not (isinstance(commands, list) and commands):
            # Nothing to run is a named, passed outcome: the candidate changed nothing executable, or the
            # repository has no test command. Discovery already climbed every rung before saying so.
            outcome = discovery_outcome if discovery_outcome in {"no_tests_required", "no_tests_found"} else "no_tests_found"
            reason = reason or ("the candidate changes only documentation or non-executable files"
                                if outcome == "no_tests_required" else "no test command found in the repository")
            return ok(task, {"verificationState": "passed", "executionOutcome": outcome,
                             "candidateCommit": candidate, "commands": [],
                             "coverage": "not-applicable", "affectedUnits": [], "reason": reason},
                      [f"[test_run] {outcome}: {reason}"])
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
                on_progress=reporter.update,
                sandbox=str(i.get("sandboxChecks", "")).strip().lower() in ("1", "true", "yes"),
                in_place=str(i.get("sharedTree", "")).strip().lower() in ("1", "true", "yes"))
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
        return ok(task, {"verificationState": "blocked", "executionOutcome": "invalid_input",
                         "reason": str(exc), "commands": []},
                  [f"[test_run] INVALID INPUT: {exc}"])
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
        template=i.get("testPlanTemplate"))
    template_source = str(i.get("testPlanTemplateSource") or "").strip()
    logs = [f"[test_plan_resolve] {out['commandSource']} "
            f"commands={len(out['commands'])} outcome={out['discoveryOutcome']}"]
    if out["commandSource"] == "user_template" and not i.get("testCommands") and template_source:
        logs.append(f"[test_plan_resolve] testPlanTemplate supplied by: {template_source}")
    return ok(task, out, logs)


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
