"""Conductor worker entrypoints for feature_campaign."""

from __future__ import annotations

import json
import os
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import budget_gate, continuous_integrate, contract_freeze, feature_campaign, file_ownership, git, templating
from common.shapes import (iter_joined as _iter_joined, listed as _list, mapping as _mapping,  # noqa: E402
                           num as _num, subtask_worktree as _subtask_worktree, truth as _truth)
from common.code_parallel import CODE_SUBTASK_TOOLS
from common.results import fail, ok
from .checks import ChecksConfigError, load_config, run_profile
from .model import aggregate_usage, select_wave, validate_checkpoint, validate_plan




def _reject_directory_write_roots(result: dict, repo_path: object) -> dict:
    """A plan's ``files`` contract names exact files, never broad directories."""
    root = Path(str(repo_path)).resolve() if str(repo_path or "").strip() else None
    if root is None or not result.get("valid"):
        return result
    errors = list(result.get("errors") or [])
    for task in result.get("tasks") or []:
        for path in task.get("files") or []:
            if (root / path).is_dir():
                errors.append(f"task {task['id']!r} files must name exact files, not directory {path!r}")
    if errors:
        result = {**result, "valid": False, "errors": errors}
    return result



@worker_task(task_definition_name="campaign_validate_plan")
def campaign_validate_plan(task):
    i = task.input_data or {}
    try:
        result = validate_plan(i.get("plan"), max_tasks=int(i.get("maxTasks") or 25))
        result = _reject_directory_write_roots(result, i.get("repoPath"))
        result["planLocation"] = ""
        if result["valid"] and i.get("repoPath"):
            rel = i.get("planPath") or ".conductor-code/campaign-plan.json"
            path = Path(i["repoPath"]) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"tasks": result["tasks"]}, indent=2) + "\n", encoding="utf-8")
            result["planLocation"] = str(rel)
        # The exact files this plan authorizes, so a workflow does not need a jq
        # task to flatten them before handing them to an agent as a write scope.
        result["allowedWriteRoots"] = sorted({
            path for entry in result.get("tasks") or []
            for path in (entry.get("files") or [])
            if isinstance(path, str) and path.strip()})
        return ok(task, result, [f"[campaign_validate_plan] valid={result['valid']} "
                                 f"tasks={len(result['tasks'])} "
                                 f"scope={len(result['allowedWriteRoots'])}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_validate_plan", exc)


@worker_task(task_definition_name="campaign_schedule")
def campaign_schedule(task):
    i = task.input_data or {}
    try:
        validated = validate_plan(i.get("plan"), max_tasks=int(i.get("maxTasks") or 25))
        validated = _reject_directory_write_roots(validated, i.get("repoPath"))
        if not validated["valid"]:
            # ``FORK_JOIN_DYNAMIC`` deserializes these fields before any later
            # checkpoint can report the invalid plan.  Always provide empty
            # collections on this fail-soft path; omitting them resolves to
            # null and crashes the workflow with a Conductor deserialization
            # error instead of preserving the validation evidence.
            return ok(task, {**validated, "ready": [], "readyIds": [],
                             "remainingIds": [], "blockedIds": [],
                             "unresolvedIds": [], "stalled": True,
                             "dynamicTasks": [], "dynamicTasksInput": {},
                             "wave": int(i.get("wave") or 1)})
        wave = select_wave(validated["tasks"], _list(i.get("completedTaskIds")),
                           _list(i.get("blockedTaskIds")),
                           max_parallelism=int(i.get("maxParallelism") or 6))
        repo = i.get("repoPath")
        number = int(i.get("wave") or 1)
        dynamic, inputs = [], {}
        for item in wave["ready"]:
            ref = f"wave_{number}_{item['id']}"
            dynamic.append({"name": "campaign_subtask", "taskReferenceName": ref,
                            "type": "SUB_WORKFLOW",
                            "subWorkflowParam": {"name": "campaign_subtask"}})
            inputs[ref] = {"repoPath": repo, "task": item, "wave": number,
                           "agent": i.get("agent") or "", "model": i.get("model") or "",
                           "maxTurns": int(i.get("maxTurns") or 500),
                           "maxBudgetUsd": float(i.get("maxBudgetUsd") or 50.0),
                           "resumeSessionId": (i.get("sessions") or {}).get(item["id"], ""),
                            "specContextPath": i.get("specContextPath") or "",
                           "contextPaths": i.get("contextPaths") or [],
                           "codePromptTemplate": i.get("codePromptTemplate") or "",
                           "codePromptTemplateSource": i.get("codePromptTemplateSource") or "",
                           "feedback": i.get("feedback") or "",
                           "modelProfile": i.get("modelProfile") or "",
                           "modelPolicy": i.get("modelPolicy") or {},
                           "modelPolicySource": i.get("modelPolicySource") or "",
                           "modelPolicySha256": i.get("modelPolicySha256") or "",
                           "modelsConfig": i.get("modelsConfig") or "",
                           "modelOverrides": i.get("modelOverrides") or {}}
        output = {**wave, "valid": True, "errors": [], "dynamicTasks": dynamic,
                  "dynamicTasksInput": inputs, "wave": number}
        return ok(task, output, [f"[campaign_schedule] wave={number} ready={wave['readyIds']} stalled={wave['stalled']}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_schedule", exc)


@worker_task(task_definition_name="campaign_integrate")
def campaign_integrate(task):
    """Merge successful campaign branches, returning conflicts/failures as data."""
    i = task.input_data or {}
    repo = i.get("repoPath") or ""
    results = i.get("results") or {}
    if isinstance(results, list):
        results = {str(pos): value for pos, value in enumerate(results)}
    merged, completed, failed, conflicts, sessions = [], [], [], [], {}
    usage_records = []
    logs = []
    try:
        for _, wrapper in (results.items() if isinstance(results, dict) else []):
            out = wrapper.get("output", wrapper) if isinstance(wrapper, dict) else {}
            ident = str(out.get("taskId") or "")
            if out.get("sessionId"):
                sessions[ident] = out["sessionId"]
            usage_records.append(out)
            if out.get("status") not in ("success", "success_no_changes"):
                if ident:
                    failed.append(ident)
                continue
            branch = out.get("branch")
            try:
                git.git(repo, "merge", "--no-edit", branch)
                merged.append(branch)
                completed.append(ident)
                worktree = out.get("worktreePath")
                if worktree:
                    git.worktree_remove(repo, os.path.basename(str(worktree)))
            except Exception:  # noqa: BLE001
                paths = git.has_conflicts(repo)
                git.git(repo, "merge", "--abort", check=False)
                conflicts.append({"taskId": ident, "branch": branch, "files": paths})
                failed.append(ident)
        usage = aggregate_usage(usage_records)
        # ``aggregate_usage`` also reports a flat list of session IDs.  The
        # campaign state needs the task-ID-to-session-ID mapping above so a
        # later wave can resume the matching subtask.  Do not let the usage
        # summary overwrite that state field.
        usage.pop("sessions", None)
        output = {"integrated": not failed and not conflicts, "merged": merged,
                  "completedTaskIds": completed, "failedTaskIds": failed,
                  "conflicts": conflicts, "sessions": sessions, **usage}
        logs.append(f"[campaign_integrate] completed={completed} failed={failed} conflicts={len(conflicts)}")
        return ok(task, output, logs)
    except Exception as exc:  # noqa: BLE001
        # Integration is deliberately fail-soft: orchestration returns to the checkpoint.
        return ok(task, {"integrated": False, "merged": merged, "completedTaskIds": completed,
                         "failedTaskIds": failed, "conflicts": conflicts,
                         "sessions": sessions, "error": str(exc),
                         **{key: value for key, value in aggregate_usage(usage_records).items()
                            if key != "sessions"}},
                  logs + [f"[campaign_integrate] fail-soft error: {exc}"])


@worker_task(task_definition_name="campaign_merge_state")
def campaign_merge_state(task):
    """Merge wave progress with explicit, stable Python data semantics."""
    i = task.input_data or {}
    try:
        added = _list(i.get("added"))
        completed = list(dict.fromkeys([*_list(i.get("completed")), *added]))
        new_sessions = _mapping(i.get("newSessions"))
        if not new_sessions and isinstance(i.get("newSessions"), list):
            new_sessions = dict(zip(added, i["newSessions"], strict=False))
        sessions = _mapping(i.get("sessions"))
        sessions.update(new_sessions)
        remaining = [ident for ident in _list(i.get("remaining")) if ident not in completed]
        waves = list(dict.fromkeys([*_list(i.get("waves")), i.get("wave")]))
        waves = [wave for wave in waves if wave is not None]
        result = {"completed": completed, "sessions": sessions,
                  "remaining": remaining, "waves": waves}
        return ok(task, {"result": result},
                  [f"[campaign_merge_state] completed={len(completed)} remaining={len(remaining)}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_merge_state", exc)


# Retained as dormant reference code only. No workflow or registered task definition uses it.
# Do not restore the worker_task decorator unless campaign checks are deliberately reintroduced.
def campaign_checks(task):
    i = task.input_data or {}
    try:
        profile = str(i.get("profile") or "")
        if not profile:
            config = load_config(i["repoPath"], i.get("configPath") or ".conductor-code/checks.json")
            key = "finalProfile" if i.get("phase") == "final" else "waveProfile"
            profile = str((config.get("defaults") or {}).get(key) or "")
        if not profile:
            return ok(task, {"passed": True, "blockingPassed": True, "executionOutcome": "passed", "profile": "",
                             "checks": [], "skipped": True, "reason": "no profile configured"})
        output = run_profile(i["repoPath"], profile, requested=_list(i.get("checks")) or None,
                             config_path=i.get("configPath") or ".conductor-code/checks.json",
                             attached_confirmed=bool(i.get("attachedConfirmed", False)))
        return ok(task, output, [f"[campaign_checks] profile={profile} blockingPassed={output['blockingPassed']}"])
    except (ChecksConfigError, OSError, ValueError) as exc:
        # A configuration/check failure is a checkpoint result, not orchestration failure.
        return ok(task, {"passed": False, "blockingPassed": False, "executionOutcome": "configuration_blocked", "checks": [],
                         "profile": i.get("profile") or "", "error": str(exc)},
                  [f"[campaign_checks] fail-soft: {exc}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_checks", exc)


def _blocking_passed(inputs: dict) -> bool:
    """Decide whether the blocking phase signals allow this checkpoint to pass.

    An explicit ``blockingPassed`` still wins. Otherwise every supplied signal
    must be affirmative: a phase is only clear when its agent reported success
    and any accompanying validity or integration flag is true.
    """
    if inputs.get("blockingPassed") is not None:
        return _truth(inputs.get("blockingPassed"), True)
    signals = [key for key in ("blockingStatus", "blockingValid", "blockingIntegrated")
               if inputs.get(key) is not None]
    if not signals:
        return True
    if "blockingStatus" in signals and str(inputs["blockingStatus"]).strip() != "success":
        return False
    return all(_truth(inputs[key], False) for key in signals if key != "blockingStatus")


@worker_task(task_definition_name="campaign_checkpoint")
def campaign_checkpoint(task):
    i = task.input_data or {}
    try:
        profile = str((i.get("decision") or {}).get("profile") or i.get("profile") or "")
        allowed = []
        if profile and i.get("repoPath"):
            try:
                config = load_config(i["repoPath"], i.get("configPath") or ".conductor-code/checks.json")
                allowed = list((config.get("profiles", {}).get(profile) or {}).get("checks") or [])
            except ChecksConfigError:
                pass
        # The workflow used to compute this in four separate one-line jq tasks
        # (`.status == "success"` and friends). The signals are inputs now and
        # the verdict is derived here, where it is unit-testable.
        result = validate_checkpoint(i.get("decision"), phase=str(i.get("phase") or ""),
                                     blocking_passed=_blocking_passed(i),
                                     allowed_checks=allowed)
        try:
            result["maxTurns"] = int(result.get("maxTurns") or i.get("maxTurns") or 500)
        except (TypeError, ValueError):
            result["maxTurns"] = int(i.get("maxTurns") or 500)
        try:
            result["maxBudgetUsd"] = float(result.get("maxBudgetUsd") or i.get("maxBudgetUsd") or 50.0)
        except (TypeError, ValueError):
            result["maxBudgetUsd"] = float(i.get("maxBudgetUsd") or 50.0)
        result["profiles"] = i.get("profiles") or {}
        if result["action"] == "set_profiles" and isinstance((i.get("decision") or {}).get("profiles"), dict):
            result["profiles"] = dict(result["profiles"])
            result["profiles"].update({k: v for k, v in (i.get("decision") or {})["profiles"].items()
                                       if v not in (None, "")})
        return ok(task, result, [f"[campaign_checkpoint] phase={result['phase']} action={result['action']} valid={result['valid']}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_checkpoint", exc)


def _usage_records(value):
    records = []
    if isinstance(value, dict):
        if any(k in value for k in ("tokenUsed", "totalTokens", "costUsd", "totalCostUsd")):
            records.append(value)
        for child in value.values():
            records.extend(_usage_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(_usage_records(child))
    return records


@worker_task(task_definition_name="campaign_summary")
def campaign_summary(task):
    i = task.input_data or {}
    try:
        usage = aggregate_usage(_usage_records(i.get("usage")))
        repo = i.get("repoPath") or ""
        base = str(i.get("baseCommit") or "HEAD~1")
        committed = git.git(repo, "diff", "--name-only", f"{base}..HEAD", check=False).stdout.splitlines()
        changed = sorted(set(committed) | set(git.status_files(repo)))
        sessions = i.get("sessions") or {}
        if isinstance(sessions, dict):
            usage["sessions"] = sessions
        uncommitted = sorted(git.status_files(repo))
        candidate = git.head(repo)
        return ok(task, {"outcome": i.get("outcome") or "incomplete", "branch": i.get("branch") or "",
                         "verifiedBranch": i.get("branch") if i.get("outcome") == "verified" else "",
                         "candidateCommit": candidate, "repoPath": repo,
                         "sourceRepoPath": i.get("sourceRepoPath") or repo,
                         "inPlace": _truth(i.get("inPlace"), False),
                         "changedFiles": changed, "uncommittedFiles": uncommitted,
                         **usage})
    except Exception as exc:  # noqa: BLE001
        return fail(task, "campaign_summary", exc)


@worker_task(task_definition_name="campaign_implementation_done")
def campaign_implementation_done(task):
    i = task.input_data or {}
    done = feature_campaign.is_implementation_done(outcome=i.get("outcome"), remaining=i.get("remaining"))
    return ok(task, {"done": done}, [f"[campaign_implementation_done] done={done}"])


@worker_task(task_definition_name="campaign_publication_plan")
def campaign_publication_plan(task):
    i = task.input_data or {}
    out = feature_campaign.resolve_campaign_publication_plan(
        outcome=i.get("outcome"), requested_draft=i.get("requestedDraft"),
        test_state=i.get("testState"), tests_passed=i.get("testsPassed"),
        tested=i.get("tested"), committed=i.get("committed"),
        requested_title=i.get("requestedTitle"), requested_body=i.get("requestedBody"),
        issue_title=i.get("issueTitle"), instruction=i.get("instruction"))
    return ok(task, out, [f"[campaign_publication_plan] draft={out['draft']} outcome={out['outcome']} "
                          f"titleChars={len(out['prTitle'])} bodyChars={len(out['prBody'])}"])



@worker_task(task_definition_name="budget_gate_check")
def budget_gate_check(task):
    """Aggregate per-run spend gate (roadmap H5). Folds the board's spend facts, decides
    ok/warn/halt, and enforces the not-startable rule for swarm-mode runs. The halt is caused only by
    accumulated spend, never by elapsed time (AGENTS.md timeout policy). Pure logic lives in
    common/budget_gate.py; this is the thin worker wrapper."""
    i = task.input_data or {}
    ceiling = _num(i.get("ceilingUsd"))
    warn_fraction = _num(i.get("warnFraction"))
    if warn_fraction is None:
        warn_fraction = budget_gate.DEFAULT_WARN_FRACTION
    swarm_mode = i.get("swarmMode", True)
    if isinstance(swarm_mode, str):
        swarm_mode = swarm_mode.strip().lower() not in ("false", "0", "no", "")
    startable = budget_gate.is_startable(ceiling, swarm_mode=bool(swarm_mode))
    out = budget_gate.evaluate(i.get("correlationId"), ceiling_usd=ceiling, warn_fraction=warn_fraction)
    out["startable"] = startable
    # Clean-halt semantics (the caller branches on this): dispatch new work only while below the
    # ceiling AND the run is startable. 'halt' or 'unbounded' => no new work; let in-flight finish.
    out["dispatchNewWork"] = out["state"] in ("ok", "warn") and startable
    return ok(task, out,
              [f"[budget_gate_check] state={out['state']} spend=${out['report']['usd']} "
               f"ceiling={out['report']['ceiling_usd']} startable={startable} "
               f"dispatchNewWork={out['dispatchNewWork']}"])


# ─────────────────────── convergence spike (roadmap Phase 1, H1/H4) ───────────────────────
# The convergence spike proves (or disproves) H1/H4: can ≥3 agents modify OVERLAPPING code behind a
# frozen contract and converge to a green shared branch without a big-bang merge? These workers are the
# live glue over the pure-logic primitives (common/contract_freeze.py, common/file_ownership.py,
# common/continuous_integrate.py). code_parallel's own fan-out is not reusable: it partitions
# file-DISJOINT work and does a big-bang merge_worktrees fan-in — the opposite premise (design.md D3).
#
# Dispatch is driven in WAVES by the workflow's DO_WHILE: each wave, convergence_build_forks dispatches
# only the items whose shared files are free (ownership GATES dispatch — two agents never hold the same
# file concurrently); convergence_integrate rebases that wave onto the moving shared head, RELEASES the
# wave's files so blocked items become dispatchable next wave, and RECORDS each agent's real spend on
# the board; the reused budget_gate_check then decides whether the next wave may dispatch. Items that do
# not fit this wave stay in ``remaining`` for the next one; the loop ends when nothing remains or the
# budget gate halts.



@worker_task(task_definition_name="convergence_build_forks")
def convergence_build_forks(task):
    """One wave of ownership-GATED dispatch. From the pending items, dispatch only those whose shared
    files are all free (``file_ownership.can_dispatch``), claiming each dispatched item's files so no two
    agents hold the same file concurrently; items blocked by a live owner stay in ``remaining`` for the
    next wave. Each dispatched fork reuses code_subtask, is rooted at the moving shared head, and is told
    the contract is frozen. Pure logic lives in common/{contract_freeze,file_ownership}.py."""
    i = task.input_data or {}
    try:
        shared_repo = str(i.get("repoPath") or "").strip()
        correlation_id = str(i.get("correlationId") or "").strip() or None
        subtasks = _list(i.get("subtasks"))
        contract_paths = [str(p).strip() for p in _list(i.get("contractPaths")) if str(p).strip()]

        # Record the frozen contract once per run (append-only; skip if already present).
        if correlation_id and not file_ownership.bb.read_facts(correlation_id, kind="contract"):
            file_ownership.bb.post_fact(correlation_id, "integrator", "contract",
                                        {"paths": contract_paths, "frozen": True})

        dynamic_tasks, dynamic_inputs, group_ids = [], {}, []
        dispatched, remaining = [], []
        for entry in subtasks:
            entry = _mapping(entry)
            sid = str(entry.get("id") or "").strip()
            if not sid:
                continue
            files = [str(p).strip() for p in _list(entry.get("files")) if str(p).strip()]
            attempt = int(entry.get("attempt") or 0)
            # Ownership gate: dispatch only if every shared file this item needs is free.
            if correlation_id:
                guard = file_ownership.can_dispatch(correlation_id, sid, files)
                if not guard["dispatchable"]:
                    remaining.append(entry)
                    continue
                for path in files:
                    file_ownership.claim(correlation_id, sid, path)
            # Carry the full item (+ attempt) so the integrator can re-queue it on failure.
            dispatched.append({"id": sid, "description": entry.get("description") or "",
                               "files": files, "attempt": attempt})
            group_ids.append(sid)
            prompt_context = {"description": entry.get("description") or "",
                              "files": ", ".join(files) or "(none)",
                              "contractPaths": ", ".join(contract_paths) or "(none)"}
            prompt = templating.render_default("convergence_subtask", prompt_context)
            dynamic_tasks.append({
                "name": "code_subtask", "taskReferenceName": sid, "type": "SUB_WORKFLOW",
                "optional": True, "subWorkflowParam": {"name": "code_subtask"},
            })
            dynamic_inputs[sid] = {
                "repoPath": shared_repo, "name": sid, "model": i.get("codeModel") or "",
                "maxTurns": i.get("maxTurns") or 250, "maxBudgetUsd": i.get("maxBudgetUsd") or 10,
                "prompt": prompt, "promptTemplate": "", "promptTemplateSource": "",
                "templateKey": "convergence_subtask", "promptContext": prompt_context,
                "specContextPath": "", "contextPaths": [],
                "allowedTools": CODE_SUBTASK_TOOLS, "allowedWriteRoots": files,
                "modelProfile": i.get("modelProfile") or "", "modelPolicy": i.get("modelPolicy") or {},
                "modelPolicySource": i.get("modelPolicySource") or "",
                "modelPolicySha256": i.get("modelPolicySha256") or "",
                "modelsConfig": i.get("modelsConfig") or "",
                "modelOverrides": i.get("modelOverrides") or {},
            }
        return ok(task, {
            "dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
            "groupIds": ",".join(group_ids), "contractPaths": contract_paths,
            "dispatched": dispatched, "remaining": remaining,
            "forkCount": len(group_ids), "remainingCount": len(remaining),
        }, [f"[convergence_build_forks] wave dispatched={len(group_ids)} "
            f"blocked/remaining={len(remaining)} ids={','.join(group_ids)}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "convergence_build_forks", e)


from common.continuous_integrate import RETRYABLE_REOPEN as _RETRYABLE_REOPEN  # noqa: E402


@worker_task(task_definition_name="convergence_integrate")
def convergence_integrate(task):
    """Finalize one wave and compute the next wave's pending set. For each dispatched fork: a fork that
    delivered no commit is an ``empty_delivery`` (never counted as integrated — the honest fix for a
    failed/no-op agent); one that edited the frozen contract is a ``contract_controlled_event`` (terminal,
    needs re-plan); the rest are rebased onto the moving shared head (NO big-bang merge) and a conflict is
    a ``rebase_conflict``. Retryable failures (empty_delivery / rebase_conflict) under the attempt cap are
    **re-queued** for a later wave — the moving head is exactly what lets a retry land cleanly — and only
    terminal failures post a ``reopen`` fact. The wave's file ownership is released (so re-queued and
    blocked items can dispatch next wave) and each agent's real spend is recorded so budget_gate_check can
    halt. Pure logic lives in common/{continuous_integrate,contract_freeze}.py, common/cost.py."""
    i = task.input_data or {}
    try:
        shared_repo = str(i.get("sharedRepo") or "").strip()
        shared_branch = str(i.get("sharedBranch") or "").strip()
        correlation_id = str(i.get("correlationId") or "").strip() or None
        contract_paths = [str(p).strip() for p in _list(i.get("contractPaths")) if str(p).strip()]
        group_ids = [g.strip() for g in str(i.get("groupIds") or "").split(",") if g.strip()]
        dispatched = _list(i.get("dispatched"))
        remaining = _list(i.get("remaining"))
        max_attempts = max(int(i.get("maxAttempts") or 2), 1)
        by_id = {str(_mapping(d).get("id")): _mapping(d) for d in dispatched}

        eligible, failures = [], []  # failures: (gid, reason, mechanism, conflicts)
        for idx, gid in enumerate(group_ids):
            worktree = _subtask_worktree(shared_repo, gid)
            branch = git.group_branch(shared_repo, gid)
            ahead = "0"
            changed = []
            if os.path.isdir(worktree):
                ahead = git.git(worktree, "rev-list", "--count", f"{shared_branch}..{branch}",
                                check=False).stdout.strip() or "0"
                names = git.git(worktree, "diff", "--name-only", f"{shared_branch}...{branch}",
                                check=False).stdout
                changed = [n for n in names.split("\n") if n.strip()]
            if ahead == "0" or not changed:
                failures.append((gid, "empty_delivery",
                                 f"fork {gid} produced no commit on {branch} (agent delivered nothing or "
                                 f"its code_subtask failed); not counted as integrated", []))
            elif contract_paths and not contract_freeze.is_integration_eligible(changed, contract_paths):
                failures.append((gid, "contract_controlled_event",
                                 f"fork {gid} edited the frozen contract; routed as a controlled event "
                                 f"(own board item + re-plan), not an ambient merge",
                                 contract_freeze.contract_paths_touched(changed, contract_paths)))
            else:
                eligible.append({"id": gid, "worktree": worktree, "branch": branch, "readyAt": idx})

        # Rebase the clean deliveries onto the moving head. Pass no correlation_id so we — not
        # integrate_ready — decide whether each conflict becomes a retry or a terminal reopen fact.
        result = continuous_integrate.integrate_ready(shared_repo, shared_branch, eligible)
        for rec in result.get("reopened", []):
            failures.append((str(rec.get("item")), "rebase_conflict",
                             str(rec.get("mechanism") or "rebase conflict"), _list(rec.get("conflicts"))))

        # Classify every failure into a bounded retry or a terminal reopen.
        retry, terminal, controlled = [], [], []
        for gid, reason, mechanism, conflicts in failures:
            rec = {"op": "reopen", "item": gid, "reason": reason,
                   "mechanism": mechanism, "conflicts": conflicts}
            item = by_id.get(gid, {"id": gid})
            attempt = int(item.get("attempt") or 0)
            if reason in _RETRYABLE_REOPEN and (attempt + 1) < max_attempts:
                retry.append({**item, "attempt": attempt + 1})
                if correlation_id:
                    file_ownership.bb.post_fact(correlation_id, gid, "retry",
                                                {**rec, "op": "retry", "nextAttempt": attempt + 1})
            else:
                terminal.append(rec)
                if reason == "contract_controlled_event":
                    controlled.append(rec)
                if correlation_id:
                    file_ownership.bb.post_fact(correlation_id, gid, "reopen", rec)

        # Release this wave's ownership so re-queued and blocked items can dispatch next wave.
        released = 0
        if correlation_id:
            for item in dispatched:
                item = _mapping(item)
                sid = str(item.get("id") or "")
                for path in _list(item.get("files")):
                    file_ownership.release(correlation_id, sid, str(path))
                    released += 1

        # Record each agent's real spend on the board (lock-free, per-author) so the reused
        # budget_gate_check sees accumulated spend and can halt the next wave.
        recorded = 0
        if correlation_id:
            for ref, out in _iter_joined(i.get("joined")):
                usd = out.get("costUsd")
                tok = out.get("tokenUsed")
                budget_gate.record_spend(
                    correlation_id, ref,
                    usd=float(usd) if isinstance(usd, (int, float)) and not isinstance(usd, bool) else None,
                    tokens=int(tok) if isinstance(tok, (int, float)) and not isinstance(tok, bool) else None,
                    model=out.get("model") or "")
                recorded += 1

        result["reopened"] = terminal          # only terminal failures count as reopened
        result["controlledEvents"] = controlled
        result["retry"] = retry
        result["nextPending"] = remaining + retry
        result["releasedFiles"] = released
        result["spendRecorded"] = recorded
        return ok(task, result,
                  [f"[convergence_integrate] integrated={len(result['integrated'])} "
                   f"retry={len(retry)} terminalReopen={len(terminal)} released={released} "
                   f"spendRecorded={recorded} bigBang={result['big_bang_merge']} "
                   f"head={result['final_head'][:12]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "convergence_integrate", e)


@worker_task(task_definition_name="convergence_outcome")
def convergence_outcome(task):
    """Fold the whole run into an honest GO/NO-GO verdict against the pre-registered bar (design.md
    Decision 4): GO iff every item integrated onto one shared branch (zero big-bang merge), nothing was
    reopened, nothing was left undispatched by a budget halt, and the branch is green. Counts come from
    the durable board (reopen facts) + the final pending set, so the verdict is an audit, not a tally
    threaded through the loop. A NO-GO names the precise mechanism; neither outcome is massaged."""
    i = task.input_data or {}
    try:
        correlation_id = str(i.get("correlationId") or "").strip() or None
        total = len(_list(i.get("subtasks")))
        remaining = _list(i.get("pendingRemaining"))
        undispatched = len(remaining)
        final_head = str(i.get("finalHead") or "")
        tests_passed = _truth(i.get("testsPassed"), False)
        test_state = str(i.get("testCycleState") or "not_run")
        big_bang = False  # the integrator never does a big-bang merge; this is the H1 invariant.

        reopen_facts = file_ownership.bb.read_facts(correlation_id, kind="reopen") if correlation_id else []
        reopened = [f.get("data") or {} for f in reopen_facts]
        reopened_count = len(reopened)
        spend_usd = budget_gate.read_spend(correlation_id)["usd"] if correlation_id else 0.0
        integrated_count = total - reopened_count - undispatched
        if integrated_count < 0:
            integrated_count = 0

        converged = (total > 0 and reopened_count == 0 and undispatched == 0
                     and integrated_count == total and tests_passed and not big_bang)
        verdict = "GO" if converged else "NO-GO"

        if converged:
            report = (f"GO: {integrated_count}/{total} overlapping items converged onto the shared "
                      f"branch (zero big-bang merge); branch green (test_cycle={test_state}). "
                      f"Shared head {final_head[:12]}.")
        else:
            reasons = []
            if undispatched:
                ids = ", ".join(str(_mapping(r).get("id")) for r in remaining)
                reasons.append(f"{undispatched} item(s) left unresolved — the run ended (budget halt or "
                               f"max waves) before they integrated ({ids})")
            if reopened_count:
                mechs = "; ".join(str(r.get("mechanism") or r.get("reason")) for r in reopened)
                reasons.append(f"{reopened_count} reopened ({mechs})")
            if integrated_count != total and not undispatched and not reopened_count:
                reasons.append(f"only {integrated_count}/{total} items integrated")
            if not tests_passed:
                reasons.append(f"shared branch not green (test_cycle={test_state})")
            report = "NO-GO: " + "; ".join(reasons or ["did not meet the GO bar"]) + \
                     f". Shared head {final_head[:12]}."

        return ok(task, {
            "converged": converged, "verdict": verdict, "bigBangMerge": big_bang,
            "finalHead": final_head, "integratedCount": integrated_count, "forkCount": total,
            "reopenedCount": reopened_count, "undispatchedCount": undispatched, "reopened": reopened,
            "testCycleState": test_state, "testsPassed": tests_passed, "spendUsd": spend_usd,
            "report": report,
        }, [f"[convergence_outcome] {verdict} — {report} spend=${spend_usd}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "convergence_outcome", e)
