"""Discrete git worker tasks for the code_parallel stack — one per operation for
visibility and distribution-readiness. Ported from ``git_ops.ts``.

Local git: prepare_repo, create_branch, commit, worktree_add, merge_worktrees.
Remote transport (provider-agnostic git): git_clone, git_fetch, git_pull, git_push,
git_remote. GitHub PR ops (via gh): pr_create, pr_checkout, pr_status, pr_comment,
pr_merge. The remote/PR ops authenticate through gh (`gh auth login` / `GH_TOKEN`).
"""

from __future__ import annotations

import json as _json
import os
import re
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import (code_subtask, gate_decision, git, github, issue_to_pr, pr_description,
                    pr_review, pr_reply, publish_salvage)
from common.results import fail, ok


def _int(val, default=None):
    if val is None or val == "":
        return default
    return int(val)


def _bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _items(val):
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return [x.strip() for x in str(val or "").split(",") if x.strip()]


def _repo_paths(val) -> list[str]:
    """Normalize an exact repository-path list without accepting escapes."""
    paths: list[str] = []
    for raw in _items(val):
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
            raise ValueError(f"repository paths must be safe and relative: {raw}")
        paths.append(path.as_posix().lstrip("./"))
    return sorted(set(paths))


def _command_failures(report: object) -> list[dict]:
    """Collect only structured command failures from an agent report."""
    if not isinstance(report, dict):
        return []
    failures: list[dict] = []
    supplied = report.get("commandFailures")
    if isinstance(supplied, list):
        failures.extend(item for item in supplied if isinstance(item, dict))
    for turn in report.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        for command in turn.get("commands") or []:
            if not isinstance(command, dict):
                continue
            exit_code = command.get("exitCode", command.get("exit_code"))
            if exit_code not in (None, 0, "0"):
                failures.append({
                    "command": command.get("command", command.get("cmd", "")),
                    "exitCode": exit_code,
                    "stderr": command.get("stderr", ""),
                })
    unique: list[dict] = []
    seen: set[str] = set()
    for failure in failures:
        key = _json.dumps(failure, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(failure)
    return unique


@worker_task(task_definition_name="delivery_audit")
def delivery_audit(task):
    """Compare a subtask's exact planned files with its live worktree changes.

    Audit errors are evidence, not orchestration failures: callers receive an
    ``audit_unavailable`` state and can still preserve and publish the branch.
    Agent text is never interpreted to decide completeness.
    """
    i = task.input_data or {}
    report = i.get("agentReport") if isinstance(i.get("agentReport"), dict) else {}
    try:
        repo = str(i["repoPath"])
        planned = _repo_paths(i.get("plannedPaths"))
        classified = git.vetted_changes(repo)
        actual = sorted(set(classified["changed"]))
        missing = sorted(set(planned) - set(actual))
        rejected = sorted(set(classified["rejected"]))
        denials = [str(value) for value in (report.get("denials") or []) if str(value)]
        command_failures = _command_failures(report)
        complete = not missing and not rejected
        state = "passed" if complete else "incomplete_delivery"
        out = {
            "auditAvailable": True,
            "state": state,
            "complete": complete,
            "plannedPaths": planned,
            "actualPaths": actual,
            "missingPaths": missing,
            "rejectedPaths": rejected,
            "commandFailures": command_failures,
            "sandboxDenials": denials,
            "agentStatus": str(report.get("status") or "unknown"),
            "agentCompleted": bool(report.get("agentCompleted")),
            "previousReport": report.get("structured") or {},
            "branch": git.git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "head": git.head(repo),
        }
        return ok(task, out, [
            f"[delivery_audit] state={state} planned={len(planned)} "
            f"actual={len(actual)} missing={len(missing)} rejected={len(rejected)}"
        ])
    except Exception as exc:  # noqa: BLE001
        repo = str(i.get("repoPath") or "")
        branch = ""
        head = ""
        if repo:
            branch_result = git.git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
            branch = branch_result.stdout.strip() if branch_result.code == 0 else ""
            head_result = git.git(repo, "rev-parse", "HEAD", check=False)
            head = head_result.stdout.strip() if head_result.code == 0 else ""
        out = {
            "auditAvailable": False,
            "state": "audit_unavailable",
            "complete": False,
            "plannedPaths": [],
            "actualPaths": [],
            "missingPaths": [],
            "rejectedPaths": [],
            "commandFailures": _command_failures(report),
            "sandboxDenials": [str(value) for value in (report.get("denials") or [])],
            "agentStatus": str(report.get("status") or "unknown"),
            "agentCompleted": bool(report.get("agentCompleted")),
            "previousReport": report.get("structured") or {},
            "branch": branch,
            "head": head,
            "reason": str(exc),
        }
        return ok(task, out, [f"[delivery_audit] audit_unavailable: {exc}"])


def _relative_roots(val) -> list[str]:
    roots: list[str] = []
    for raw in _items(val):
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
            raise ValueError(f"materializedSourcePaths must be safe repository-relative paths: {raw}")
        roots.append(path.as_posix().rstrip("/"))
    return sorted(set(roots))


def _slug(value: str) -> str:
    try:
        return github.repo_slug(value).lower()
    except Exception:  # noqa: BLE001
        return ""


def _publication_block(error: Exception) -> tuple[str, str] | None:
    """Classify remote policy/auth rejections that cannot succeed on retry.

    Keep returned reasons stable and credential-free; raw process output remains
    in worker logs only for genuinely retryable/unclassified failures.
    """
    text = "\n".join(str(value or "") for value in (
        error, getattr(error, "stdout", ""), getattr(error, "stderr", "")
    )).lower()
    if "without `workflow` scope" in text or "without workflow scope" in text:
        return "permission_blocked", "GitHub credential lacks permission to update workflow files"
    if any(marker in text for marker in (
        "protected branch hook declined",
        "protected branch update failed",
        "repository rule violations found",
        "push cannot contain secrets",
        "push declined due to repository rule",
    )):
        return "policy_blocked", "remote repository policy rejected this branch update"
    if any(marker in text for marker in (
        "authentication failed",
        "permission to ",
        "write access to repository not granted",
        "could not read username",
    )):
        return "permission_blocked", "remote authentication does not grant branch write access"
    return None


def _existing_pr(error: Exception) -> tuple[int, str] | None:
    text = "\n".join(str(value or "") for value in (
        error, getattr(error, "stdout", ""), getattr(error, "stderr", "")
    ))
    if "already exists" not in text.lower():
        return None
    match = re.search(r"https://[^\s]+/pull/(\d+)", text)
    return (int(match.group(1)), match.group(0)) if match else None


@worker_task(task_definition_name="plan_source_detect")
def plan_source_detect(task):
    """Select OpenSpec only when the target checkout already has ``openspec/``.

    A folder of ordinary Markdown, text, or other design material is deliberately
    not treated as an OpenSpec project.  Those documents are passed directly to
    the generic planner through ``contextPaths``.
    """
    i = task.input_data or {}
    try:
        repo = Path(str(i.get("repoPath") or "")).expanduser().resolve()
        if not repo.is_dir():
            raise ValueError("repoPath must be an existing directory")
        openspec = repo / "openspec"
        use_openspec = openspec.is_dir()
        return ok(task, {
            "mode": "openspec" if use_openspec else "documents",
            "useOpenSpec": use_openspec,
            "openspecPath": str(openspec) if use_openspec else "",
        }, [f"[plan_source_detect] mode={'openspec' if use_openspec else 'documents'} repo={repo}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "plan_source_detect", e)


@worker_task(task_definition_name="workspace_prepare")
def workspace_prepare(task):
    """Create one run-owned branch containing the checkout's Git-visible state.

    In-place runs create that branch in the supplied checkout before committing
    the baseline. Isolated runs snapshot the source without changing its branch,
    index, or files. Nested workflows inherit the parent's branch and never create
    a second outcome branch.
    """
    i = task.input_data or {}
    try:
        if _bool(i.get("inPlace"), False):
            if _bool(i.get("createPr"), False):
                raise ValueError("inPlace execution is local-only; createPr must be false")
            run_id = str(i.get("branchRunId") or i.get("workflowId")
                         or getattr(task, "workflow_instance_id", "")
                         or getattr(task, "task_id", "workspace"))
            prepared = git.prepare_inplace(
                i.get("repoPath") or "", run_id=run_id,
                branch_name=str(i.get("branch") or ""))
            git.exclude_worktrees(prepared["repoPath"])
            return ok(task, {
                "sourceRepoPath": prepared["repoPath"],
                "worktreePath": prepared["repoPath"],
                "branch": prepared["branch"],
                "baseCommit": prepared["checkpointCommit"],
                "originalBranch": prepared["originalBranch"],
                "sourceHead": prepared["originalHead"],
                "originalHead": prepared["originalHead"],
                "baselineCommit": prepared["baselineCommit"],
                "baselineCreated": prepared["baselineCreated"],
                "baselineIncludedPaths": prepared["includedPaths"],
                "statusFingerprint": prepared["statusFingerprintAfter"],
                "ignoredSourceChanges": 0,
                "ignoredSourcePaths": [],
                "materializedSourcePaths": [],
                "owned": False,
                "resumed": False,
                "sourceCloned": False,
                "inPlace": True,
            }, [f"[workspace_prepare] in-place {prepared['repoPath']} branch={prepared['branch']} "
                f"baseline={prepared['baselineCommit'][:12]} created={prepared['baselineCreated']}"])
        inherited = str(i.get("workspacePath") or "").strip()
        if inherited:
            workspace = str(Path(inherited).expanduser().resolve())
            inside = git.git(workspace, "rev-parse", "--is-inside-work-tree", check=False)
            if inside.code != 0 or inside.stdout.strip() != "true":
                raise ValueError(f"inherited workspacePath is not a git worktree: {workspace}")
            source = str(Path(i.get("repoPath") or workspace).expanduser().resolve())
            inherited_branch = git.git(
                workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            inherited_head = git.head(workspace)
            return ok(task, {
                "sourceRepoPath": source,
                "worktreePath": workspace,
                "branch": inherited_branch,
                "baseCommit": inherited_head,
                "originalBranch": inherited_branch,
                "sourceHead": inherited_head,
                "originalHead": inherited_head,
                "baselineCommit": inherited_head,
                "baselineCreated": False,
                "baselineIncludedPaths": [],
                "ignoredSourceChanges": 0,
                "ignoredSourcePaths": [],
                "materializedSourcePaths": [],
                "owned": False,
                "resumed": True,
                "sourceCloned": False,
                "inPlace": False,
            }, [f"[workspace_prepare] inherited {workspace}"])

        source_value = str(i.get("repoPath") or "").strip()
        source_cloned = False
        if source_value:
            source = git.validate_repo_path(source_value)
        else:
            repo_url = str(i.get("repoUrl") or "").strip()
            if not repo_url:
                raise ValueError("workspace_prepare requires repoPath or repoUrl")
            github.ensure_git_auth()
            run_id = str(i.get("branchRunId") or i.get("workflowId")
                         or getattr(task, "workflow_instance_id", "")
                         or getattr(task, "task_id", "workspace"))
            source = str(Path(i.get("cloneDest") or
                              f"/tmp/conductor-source-{git._safe_name(run_id)}").resolve())
            inside = git.git(source, "rev-parse", "--is-inside-work-tree", check=False)
            if inside.code != 0:
                if os.path.exists(source):
                    raise ValueError(f"clone destination exists but is not a git repository: {source}")
                git.clone(github.clone_url(repo_url), source)
            source_cloned = True

        prepared = git.ensure_ready(source, preserve_worktree=True)
        source = prepared["repoPath"]
        git.exclude_worktrees(source)
        changes = sorted(git.status_files(source))
        materialized_roots = _relative_roots(i.get("materializedSourcePaths"))
        materialized = [path for path in changes if any(
            path == root or path.startswith(root + "/") for root in materialized_roots)]
        expected = {_slug(x) for x in _items(i.get("expectedRepos"))}
        expected.discard("")
        if expected and not source_cloned:
            actual = {_slug(url) for url in git.remote_urls(source).values()}
            if not (expected & actual):
                raise ValueError(
                    "local checkout remotes do not match the requested repository; "
                    f"expected one of {sorted(expected)}, found {sorted(x for x in actual if x)}")

        fetch_source = str(i.get("fetchSource") or "").strip()
        fetch_refspec = str(i.get("fetchRefspec") or "").strip()
        if fetch_refspec:
            github.ensure_git_auth()
            source_ref = github.clone_url(fetch_source) if fetch_source else "origin"
            git.fetch_source(source, source_ref, fetch_refspec)

        snapshot = git.snapshot_worktree(
            source,
            run_id=str(i.get("branchRunId") or i.get("workflowId")
                       or getattr(task, "workflow_instance_id", "")
                       or getattr(task, "task_id", "workspace")),
            original_branch=prepared["branch"],
            original_head=prepared["head"],
        )
        out = git.workspace_add(
            source,
            str(i.get("branchRunId") or i.get("workflowId")
                or getattr(task, "workflow_instance_id", "")
                or getattr(task, "task_id", "workspace")),
            branch_name=str(i.get("branch") or "").strip() or None,
            start_point=snapshot["baselineCommit"],
            preserve_existing=_bool(i.get("preserveExisting"), True),
        )
        output = {
            "sourceRepoPath": source,
            "worktreePath": out["worktreePath"],
            "branch": out["branch"],
            "baseCommit": out["initialCommit"],
            "originalBranch": prepared["branch"],
            "originalHead": prepared["head"],
            "baselineCommit": snapshot["baselineCommit"],
            "baselineCreated": snapshot["baselineCreated"],
            "baselineIncludedPaths": snapshot["includedPaths"],
            "ignoredSourceChanges": 0,
            "ignoredSourcePaths": [],
            "materializedSourcePaths": materialized,
            "owned": True,
            "resumed": out["resumed"],
            "sourceCloned": source_cloned,
            "sourceHead": prepared["head"],
            "inPlace": False,
        }
        return ok(task, output, [
            f"[workspace_prepare] {source} -> {out['worktreePath']} branch={out['branch']}",
            f"[workspace_prepare] included source changes={len(snapshot['includedPaths'])} "
            f"materialized={len(materialized)} resumed={out['resumed']}",
        ])
    except Exception as e:  # noqa: BLE001
        return fail(task, "workspace_prepare", e)


@worker_task(task_definition_name="workspace_cleanup")
def workspace_cleanup(task):
    """Optionally remove an owned run worktree while preserving all git branches."""
    i = task.input_data or {}
    try:
        keep = _bool(i.get("keepWorktree"), True)
        owned = _bool(i.get("owned"), False)
        outcome = str(i.get("outcome") or "completed").strip().lower()
        successful = outcome in {"completed", "success", "succeeded", "verified", "passed"}
        if keep or not owned or not successful:
            reason = "requested" if keep else ("inherited" if not owned else f"outcome={outcome}")
            return ok(task, {
                "removed": False,
                "retained": True,
                "reason": reason,
                "worktreePath": i.get("worktreePath") or "",
                "branch": i.get("branch") or "",
            }, [f"[workspace_cleanup] retained ({reason})"])
        result = git.worktree_remove_path(
            str(i["sourceRepoPath"]), str(i["worktreePath"]), remove_nested=True)
        return ok(task, {
            "removed": True,
            "retained": False,
            "reason": "cleanup requested",
            "worktreePath": i.get("worktreePath") or "",
            "branch": i.get("branch") or "",
            "removedPaths": result["removed"],
        }, [f"[workspace_cleanup] removed {len(result['removed'])} worktree(s); branch retained"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "workspace_cleanup", e)


@worker_task(task_definition_name="prepare_repo")
def prepare_repo(task):
    """Make a repo git-ready before worktrees are created: git init if needed,
    set a local identity if none is configured, and ensure an initial commit.
    Idempotent — the first step of code_parallel so callers don't have to set up
    git by hand."""
    i = task.input_data or {}
    try:
        if _bool(i.get("inPlace"), False):
            run_id = str(i.get("branchRunId") or i.get("workflowId")
                         or getattr(task, "workflow_instance_id", "")
                         or getattr(task, "task_id", "code"))
            out = git.prepare_inplace(
                i["repoPath"], run_id=run_id,
                branch_name=str(i.get("branch") or ""))
            git.exclude_worktrees(out["repoPath"])
            return ok(task, out, [f"[prepare_repo] in-place branch={out['branch']} "
                                 f"baseline={out['baselineCommit'][:12]} created={out['baselineCreated']}"])
        out = git.ensure_ready(
            i["repoPath"],
            name=i.get("identityName") or "conductor-code",
            email=i.get("identityEmail") or "harness@conductor.local",
            preserve_worktree=True,
        )
        return ok(task, out, [f"[prepare_repo] init={out['initialized']} "
                              f"initialCommit={out['initialCommitCreated']} branch={out['branch']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "prepare_repo", e)


@worker_task(task_definition_name="create_branch")
def create_branch(task):
    i = task.input_data or {}
    try:
        out = git.branch(
            i["repoPath"], i["name"],
            run_id=str(i.get("branchRunId") or i.get("workflowId")
                       or getattr(task, "workflow_instance_id", "")
                       or getattr(task, "task_id", "branch")),
            force_new=_bool(i.get("forceNew"), False))
        if _bool(i.get("inPlace"), False):
            out["inPlace"] = True
            return ok(task, out, [f"[create_branch] in-place outcome branch {out['branch']}"])
        return ok(task, out, [f"[create_branch] {out['branch']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "create_branch", e)


@worker_task(task_definition_name="inplace_guard")
def inplace_guard(task):
    """Fail closed if the supplied integration checkout moved during a run."""
    i = task.input_data or {}
    try:
        out = git.assert_inplace_state(
            i["repoPath"], branch_name=str(i["branch"]),
            expected_head=str(i["expectedHead"]),
            expected_status=str(i.get("expectedStatusFingerprint") or ""),
        )
        return ok(task, out, [f"[inplace_guard] {out['branch']}@{out['head'][:12]} matched"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "inplace_guard", e)


@worker_task(task_definition_name="commit")
def commit(task):
    i = task.input_data or {}
    try:
        out = git.commit(i["repoPath"], i.get("message", "conductor-code change"))
        return ok(task, out, [f"[commit] HEAD={out['commit']} msg={i.get('message','')}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "commit", e)


@worker_task(task_definition_name="worktree_add")
def worktree_add(task):
    i = task.input_data or {}
    try:
        out = git.worktree_add(i["repoPath"], i["name"],
                               preserve_existing=_bool(i.get("preserveExisting"), False))
        return ok(task, out, [f"[worktree_add] {out['branch']} -> {out['worktreePath']} HEAD={out['initialCommit'][:7]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "worktree_add", e)


@worker_task(task_definition_name="merge_worktrees")
def merge_worktrees(task):
    """Merge each group branch into the current change branch. On conflict, a
    Claude Agent SDK session resolves the markers. Ported from ``integrate.ts``."""
    i = task.input_data or {}
    repo = i["repoPath"]
    ids = i.get("groupIds")
    ids = ids.split(",") if isinstance(ids, str) else (ids or [])
    ids = [x.strip() for x in ids if x and x.strip()]
    model = i.get("modelBuilder") or None
    max_resolution_attempts = max(1, min(int(i.get("maxResolutionAttempts") or 1), 3))

    merged, conflicts, resolved, unresolved, errors = [], [], [], [], []
    total_tokens, total_cost = 0, 0.0
    logs = [f"[merge_worktrees] merging: {', '.join(ids)}"]
    try:
        # Never discover a branch conflict in the supplied integration checkout.
        # A disposable worktree proves the complete ordered merge set first.
        if _bool(i.get("preflight"), False) and ids:
            name = f"preflight-{git._safe_name(str(getattr(task, 'workflow_instance_id', '') or getattr(task, 'task_id', 'merge')))}"
            trial = git.worktree_add(repo, name)
            try:
                for gid in ids:
                    branch = git.GROUP_BRANCH.format(name=gid)
                    probe = git.git(trial["worktreePath"], "merge", "--no-edit", branch, check=False)
                    if probe.code != 0:
                        git.git(trial["worktreePath"], "merge", "--abort", check=False)
                        logs.append(f"[merge_worktrees] preflight conflict/error for {branch}; resolving in the integration checkout")
                        break
                logs.append(f"[merge_worktrees] preflight passed in {trial['worktreePath']}")
            finally:
                git.worktree_remove(repo, name)
        for gid in ids:
            br = git.GROUP_BRANCH.format(name=gid)
            try:
                git.git(repo, "merge", "--no-edit", br)
                merged.append(br)
                logs.append(f"[merge_worktrees] merged {br} cleanly")
            except Exception as merge_error:  # noqa: BLE001
                conflicted = git.has_conflicts(repo)
                if not conflicted:
                    # A failed merge without conflict markers is still a failed
                    # merge (for example an absent branch, a dirty-index guard,
                    # or a hook failure).  Treating it as a harmless `continue`
                    # made merge_remediation classify the run as "merged" because
                    # it keys its state from `unresolved`.  Preserve the error as
                    # fail-soft evidence and stop before later branches can mask it.
                    detail = (getattr(merge_error, "stderr", "") or
                              getattr(merge_error, "stdout", "") or str(merge_error)).strip()
                    errors.append({"branch": br, "reason": detail})
                    unresolved.append(br)
                    logs.append(f"[merge_worktrees] merge error on {br} (no conflict markers): {detail}")
                    # One branch we cannot merge must not strand the others. Breaking here
                    # skipped every later group branch (its commits never reached the change
                    # branch) and skipped the per-branch worktree_remove below, leaking the
                    # worktree too. Abort any partial merge, drop this worktree, and carry
                    # on; the failure is already recorded in `errors`/`unresolved`.
                    # `continue` (not fall-through): there are no conflict markers here, so
                    # the resolver path below must not run for this branch.
                    git.git(repo, "merge", "--abort", check=False)
                    git.worktree_remove(repo, gid)
                    continue
                conflicts.append(br)
                logs.append(f"[merge_worktrees] conflict on {br}: {', '.join(conflicted)}")
                # Keep the clean/preflight path usable in deployments that do
                # not install a coding backend; only an actual conflict needs it.
                from common.claude import run_agent
                prompt = (
                    f"Resolve ALL git merge conflicts in these files: {', '.join(conflicted)}. "
                    "Keep both sides' changes where possible. Remove every conflict marker "
                    "(<<<<<<<, =======, >>>>>>>). Edit only the conflicted files."
                )
                for attempt in range(1, max_resolution_attempts + 1):
                    res = run_agent(prompt, cwd=repo, model=model, write=True,
                                    max_budget_usd=float(i.get("maxBudgetUsd") or 50.0))
                    total_tokens += res["tokens"]
                    total_cost += res["cost_usd"]
                    if res["ok"]:
                        unsafe = [path for path in conflicted if not git.is_vetted_change_path(path)]
                        if unsafe:
                            raise ValueError("resolved conflict contains generated/cache path: " + ", ".join(unsafe))
                        # A merge that resolves to our side can have no ordinary cached
                        # diff, yet still needs these paths staged to conclude the merge.
                        git.git(repo, "add", "--", *conflicted)
                        committed = git.git(repo, "commit", "-m", f"merge_worktrees: resolve conflict from {br}", check=False)
                        if committed.code == 0 and not git.has_conflicts(repo):
                            resolved.append(br)
                            logs.append(f"[merge_worktrees] resolved {br} on attempt {attempt} (tokens={res['tokens']} cost=${res['cost_usd']:.4f})")
                            break
                    logs.append(f"[merge_worktrees] resolution attempt {attempt}/{max_resolution_attempts} did not complete {br}")
                else:
                    unresolved.append(br)
                    git.git(repo, "merge", "--abort", check=False)
                    logs.append(f"[merge_worktrees] unresolved after {max_resolution_attempts} attempts: {br}")
            git.worktree_remove(repo, gid)
        git.git(repo, "worktree", "prune", check=False)
        logs.append(f"[merge_worktrees] merged={len(merged)} conflicts={len(conflicts)} resolved={len(resolved)} unresolved={len(unresolved)} errors={len(errors)}")
        return ok(task, {
            "merged": merged, "conflicts": conflicts, "resolved": resolved, "unresolved": unresolved,
            "errors": errors,
            # The routable verdict, named here rather than derived by a jq task:
            # anything left unresolved means the merge did not complete.
            "mergeState": "merged" if not unresolved else "conflicted",
            "tokenUsed": total_tokens, "costUsd": round(total_cost, 6),
            "commit": git.head(repo),
        }, logs)
    except Exception as e:  # noqa: BLE001
        return fail(task, "merge_worktrees", e, logs)


# --------------------------------------------------------------------------- remote git

@worker_task(task_definition_name="git_clone")
def git_clone(task):
    """Clone a remote repo. Input: repoUrl, dest?, branch?, depth?."""
    i = task.input_data or {}
    try:
        github.ensure_git_auth()
        # Accept a bare owner/name slug (like the gh-based tasks) — git clone needs a real URL.
        url = github.clone_url(i["repoUrl"])
        out = git.clone(url, i.get("dest") or None,
                        branch=i.get("branch") or None, depth=_int(i.get("depth")))
        return ok(task, out, [f"[git_clone] {url} -> {out['repoPath']} "
                              f"branch={out['branch']} HEAD={out['head'][:7]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "git_clone", e)


@worker_task(task_definition_name="git_fetch")
def git_fetch(task):
    """Fetch from a remote. Input: repoPath, remote?, refspec?, prune?."""
    i = task.input_data or {}
    try:
        out = git.fetch(i["repoPath"], remote=i.get("remote") or "origin",
                        refspec=i.get("refspec") or None, prune=_bool(i.get("prune")))
        return ok(task, out, [f"[git_fetch] {out['remote']} {out.get('refspec','')}".rstrip()])
    except Exception as e:  # noqa: BLE001
        return fail(task, "git_fetch", e)


@worker_task(task_definition_name="git_pull")
def git_pull(task):
    """Fetch + integrate the remote branch. Input: repoPath, remote?, branch?, rebase?.
    Fail-soft on conflict: returns conflicts[] and leaves the tree clean (not FAILED)."""
    i = task.input_data or {}
    try:
        out = git.pull(i["repoPath"], remote=i.get("remote") or "origin",
                       branch_name=i.get("branch") or None,
                       rebase=_bool(i.get("rebase"), True))
        log = (f"[git_pull] pulled={out['pulled']} branch={out['branch']} "
               f"conflicts={out['conflicts']}")
        return ok(task, out, [log])
    except Exception as e:  # noqa: BLE001
        return fail(task, "git_pull", e)


@worker_task(task_definition_name="git_push")
def git_push(task):
    """Push a branch to a remote. Input: repoPath, branch?, destinationBranch?,
    remote?/remoteUrl?, setUpstream?, forceWithLease?. Never a bare --force."""
    i = task.input_data or {}
    try:
        github.ensure_git_auth()
        out = git.push(i["repoPath"], branch_name=i.get("branch") or None,
                       remote=i.get("remoteUrl") or i.get("remote") or "origin",
                       destination_branch=i.get("destinationBranch") or None,
                       set_upstream=_bool(i.get("setUpstream"), True),
                       force_with_lease=_bool(i.get("forceWithLease")),
                       expected_head=i.get("expectedHead"))
        return ok(task, out, [f"[git_push] {out['remote']} {out['branch']} HEAD={out['head'][:7]}"])
    except Exception as e:  # noqa: BLE001
        blocked = _publication_block(e)
        if blocked:
            state, reason = blocked
            repo = str(i.get("repoPath") or "")
            head = git.head(repo) if repo else ""
            branch = str(i.get("destinationBranch") or i.get("branch") or "")
            out = {"pushed": False, "publicationState": state, "retryable": False,
                   "reason": reason, "head": head, "branch": branch,
                   "remote": str(i.get("remoteUrl") or i.get("remote") or "origin")}
            return ok(task, out, [f"[git_push] {state}: {reason}; retained {branch}@{head[:12]}"])
        return fail(task, "git_push", e)


@worker_task(task_definition_name="git_remote")
def git_remote(task):
    """Add/set a remote's URL (idempotent). Input: repoPath, url, name?."""
    i = task.input_data or {}
    try:
        out = git.remote_set(i["repoPath"], i["url"], name=i.get("name") or "origin")
        return ok(task, out, [f"[git_remote] {out['remote']} -> {out['url']} existed={out['existed']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "git_remote", e)


# --------------------------------------------------------------------------- GitHub PRs

@worker_task(task_definition_name="issue_fetch")
def issue_fetch(task):
    """Fetch a GitHub issue's title/body/labels. Input: repo (owner/name or URL) or
    repoUrl, number. Used to seed an instruction for a downstream code workflow."""
    i = task.input_data or {}
    try:
        repo_ref = i.get("repo") or i.get("repoUrl") or ""
        out = github.issue_fetch(repo_ref, _int(i["number"]))
        return ok(task, out, [f"[issue_fetch] #{out['number']} {out['state']}: {out['title'][:80]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "issue_fetch", e)


@worker_task(task_definition_name="pr_comments")
def pr_comments(task):
    """Gather + consolidate a PR's review feedback (conversation + reviews + inline),
    skipping the harness's own comments. Input: repo (owner/name or URL) or repoUrl,
    number. Returns metadata + a consolidated `feedback` blob + hasFeedback."""
    i = task.input_data or {}
    try:
        repo_ref = i.get("repo") or i.get("repoUrl") or ""
        out = github.pr_comments(repo_ref, _int(i["number"]))
        return ok(task, out, [f"[pr_comments] #{out['number']} feedback={out['commentCount']} "
                              f"links={out.get('linkCount', 0)} warnings={len(out.get('linkWarnings', []))} "
                              f"head={out['head']} hasFeedback={out['hasFeedback']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "pr_comments", e)


@worker_task(task_definition_name="pr_diff")
def pr_diff(task):
    """Return a PR's unified diff (capped) + changed files, to feed the reviewer.
    Input: repo (owner/name or URL) or repoUrl, number."""
    i = task.input_data or {}
    try:
        repo_ref = i.get("repo") or i.get("repoUrl") or ""
        out = github.pr_diff(repo_ref, _int(i["number"]), repo_path=i.get("repoPath"))
        return ok(task, out, [f"[pr_diff] {len(out['changedFiles'])} file(s), "
                              f"{len(out['diff'])} chars truncated={out['truncated']} "
                              f"source={out.get('diffSource')}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "pr_diff", e)


@worker_task(task_definition_name="local_diff")
def local_diff(task):
    """Build a local checkout's review diff against a freshly fetched remote branch.

    The operation is deliberately review-only: it never alters files, stages, commits,
    pushes, or checks out a branch.  The fetch updates the remote-tracking baseline so
    the following coding-agent review has an accurate comparison point.
    """
    i = task.input_data or {}
    try:
        out = git.local_diff_against_remote(
            i["repoPath"], remote=i.get("baseRemote") or "origin",
            branch=i.get("baseBranch") or "main")
        return ok(task, out, [
            f"[local_diff] {out['baseRef']} {out['baseCommit'][:7]} "
            f"files={len(out['changedFiles'])} untracked={len(out['untrackedFiles'])} "
            f"chars={out['diffChars']} truncated={out['truncated']}",
        ])
    except Exception as e:  # noqa: BLE001
        return fail(task, "local_diff", e)


@worker_task(task_definition_name="pr_submit_review")
def pr_submit_review(task):
    """Post a formal PR review (inline comments + summary + verdict) from the agent's
    structured findings. Input: repo (or repoUrl), number, structured
    ({summary, verdict, comments[]}; dict or JSON string), event?."""
    i = task.input_data or {}
    try:
        repo_ref = i.get("repo") or i.get("repoUrl") or ""
        structured = i.get("structured")
        if isinstance(structured, str):
            structured = _json.loads(structured) if structured.strip() else {}
        structured = structured or {}
        verdict = str(structured.get("verdict") or "approve").lower()
        requested_event = str(i.get("event") or "").strip().upper()
        event = requested_event or (
            "REQUEST_CHANGES" if verdict == "request_changes" else "APPROVE"
        )
        out = github.submit_review(
            repo_ref, _int(i["number"]),
            summary=structured.get("summary") or (
                "Changes requested." if event == "REQUEST_CHANGES" else "LGTM"
            ),
            event=event, comments=structured.get("comments") or [])
        return ok(task, out, [f"[pr_submit_review] #{i.get('number')} event={out['event']} "
                              f"inline={out['inlineCount']} (posted inline={out['inline']})"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "pr_submit_review", e)


@worker_task(task_definition_name="pr_create")
def pr_create(task):
    """Open a PR from the change branch. Input: repoPath, title, body?, base?, head?,
    draft?, fill?. Returns number + url."""
    i = task.input_data or {}
    description = pr_description.format_summary(
        i["repoPath"],
        i.get("summary") or i.get("summaryFallback") or i.get("body") or i.get("title") or "",
        issue_body=i.get("issueBody") or "",
    )
    try:
        out = github.pr_create(i["repoPath"], title=i.get("title") or "",
                               body=description["body"], base=i.get("base") or None,
                               summary=description["summary"],
                               head_branch=i.get("head") or None,
                               draft=_bool(i.get("draft")), fill=_bool(i.get("fill")))
        out["publicationState"] = "published"
        return ok(task, out, [f"[pr_create] #{out['number']} {out['url']}"])
    except Exception as e:  # noqa: BLE001
        existing = _existing_pr(e)
        if existing:
            number, url = existing
            github.update_pr_body(i["repoPath"], number, description["body"])
            draft_state = github.pr_set_draft(i["repoPath"], number, _bool(i.get("draft")))
            return ok(task, {"created": False, "existing": True, "number": number, "url": url,
                             "draft": draft_state["draft"], "publicationState": "published",
                             **description},
                      [f"[pr_create] reused existing PR #{number} {url}"])
        blocked = _publication_block(e)
        if blocked:
            state, reason = blocked
            return ok(task, {"created": False, "existing": False, "number": 0, "url": "",
                             "draft": _bool(i.get("draft")), "publicationState": state,
                             "reason": reason, "branch": str(i.get("head") or "")},
                      [f"[pr_create] {state}: {reason}; retained {i.get('head') or 'local branch'}"])
        return fail(task, "pr_create", e)


@worker_task(task_definition_name="pr_checkout")
def pr_checkout(task):
    """Check out an existing PR by number.

    ``repo`` optionally selects the upstream repository that owns the PR; the
    checkout's origin remains the working repository (often a contributor fork).
    """
    i = task.input_data or {}
    try:
        out = github.pr_checkout(i["repoPath"], _int(i["number"]),
                                 pr_repo=i.get("repo") or None,
                                 branch=i.get("branch") or None, force=_bool(i.get("force")))
        return ok(task, out, [f"[pr_checkout] #{out['number']} -> {out['branch']} HEAD={out['head'][:7]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "pr_checkout", e)


@worker_task(task_definition_name="pr_status")
def pr_status(task):
    """Read a PR's review/merge state + CI checks. Input: repoPath, number?."""
    i = task.input_data or {}
    try:
        out = github.pr_status(i["repoPath"], _int(i.get("number")))
        return ok(task, out, [f"[pr_status] #{out['number']} state={out['state']} "
                              f"mergeable={out['mergeable']} checks:"
                              f"pass={out['passing']}/fail={out['failing']}/pending={out['pending']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "pr_status", e)


@worker_task(task_definition_name="pr_branch_guard")
def pr_branch_guard(task):
    """Compare the current remote PR tip to the snapshot taken before edits.

    Drift is a normal race outcome, not a task exception: the workflow routes it
    to a checkpoint and never pushes the stale local candidate.
    """
    i = task.input_data or {}
    try:
        expected = str(i.get("expectedHeadSha") or "").strip()
        if not expected:
            raise ValueError("expectedHeadSha is required")
        actual = github.remote_branch_head(i["repo"], i["branch"])
        matched = actual == expected
        return ok(task, {"verificationState": "matched" if matched else "branch_drift",
                         "expectedHeadSha": expected, "actualHeadSha": actual,
                         "branch": i["branch"]},
                  [f"[pr_branch_guard] branch={i['branch']} expected={expected[:12]} actual={actual[:12]} matched={matched}"])
    except Exception as e:  # noqa: BLE001
        # Remote-head lookup errors are not a match and must never permit a
        # push.  Return structured unknown evidence so the caller pauses at its
        # branch-drift checkpoint instead of losing the candidate to a failed
        # workflow execution.
        expected = str(i.get("expectedHeadSha") or "")
        reason = f"GitHub PR branch lookup failed: {e}"
        return ok(task, {"verificationState": "unknown", "expectedHeadSha": expected,
                         "actualHeadSha": "", "branch": i.get("branch") or "",
                         "reason": reason}, [f"[pr_branch_guard] BLOCKED: {reason}"])


@worker_task(task_definition_name="pr_commit_checks")
def pr_commit_checks(task):
    """Return the aggregate state of every reported GitHub check for one SHA."""
    i = task.input_data or {}
    try:
        if "pushed" in i and not _bool(i.get("pushed")):
            state = str(i.get("publicationState") or "publication_blocked")
            return ok(task, {"sha": str(i.get("sha") or ""), "checks": [], "links": [],
                             "checkCount": 0, "verificationState": state,
                             "reason": str(i.get("publicationReason") or
                                           "candidate branch was not published")},
                      [f"[pr_commit_checks] skipped: publication state={state}"])
        out = github.commit_checks(i["repo"], str(i["sha"]))
        # A blank or "empty" state means CI has not reported yet, which is a
        # pending poll rather than a verdict. Naming it here removes the jq task
        # that used to normalize it before the poll loop could read it.
        reported = str(out.get("verificationState") or "").strip()
        out["ciState"] = "pending" if not reported or reported == "empty" else reported
        return ok(task, out, [f"[pr_commit_checks] sha={out['sha'][:12]} checks={out['checkCount']} state={out['verificationState']}"])
    except Exception as e:  # noqa: BLE001
        # CI observability is evidence gathering.  An exhausted GitHub retry is
        # not proof that a candidate passed, but it also must not bypass the
        # workflow's CI-blocked checkpoint by failing the worker outright.
        sha = str(i.get("sha") or "")
        reason = f"GitHub exact-SHA check lookup failed: {e}"
        return ok(task, {"sha": sha, "checks": [], "links": [], "checkCount": 0,
                         "verificationState": "unknown", "reason": reason},
                  [f"[pr_commit_checks] BLOCKED: {reason}"])


@worker_task(task_definition_name="pr_comment")
def pr_comment(task):
    """Post a comment on a PR. Input: repoPath, number, body, repo?."""
    i = task.input_data or {}
    try:
        out = github.pr_comment(i["repoPath"], _int(i["number"]), i.get("body") or "",
                                repo_ref=i.get("repo") or i.get("repoUrl") or None)
        return ok(task, out, [f"[pr_comment] #{out['number']} {out['url']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "pr_comment", e)


@worker_task(task_definition_name="pr_merge")
def pr_merge(task):
    """Merge a PR. Input: repoPath, number, method?(squash|rebase|merge),
    deleteBranch?, auto?. Destructive — opt-in, no retry."""
    i = task.input_data or {}
    try:
        out = github.pr_merge(i["repoPath"], _int(i["number"]),
                              method=i.get("method") or "squash",
                              delete_branch=_bool(i.get("deleteBranch"), True),
                              auto=_bool(i.get("auto")))
        return ok(task, out, [f"[pr_merge] #{out['number']} method={out['method']} auto={out['auto']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "pr_merge", e)


@worker_task(task_definition_name="json_text")
def json_text(task):
    """Serialize named inputs to JSON text so a prompt can interpolate them.

    Workflows used to do this with a `JSON_JQ_TRANSFORM` whose whole expression
    was a row of `tojson` calls. That carried a throwing-expression failure
    surface and three fixture cases for no logic at all. Every input arrives
    back as `<name>Json`; a value that cannot be serialized degrades to its
    string form rather than failing the task.
    """
    i = dict(task.input_data or {})
    out: dict[str, str] = {}
    for name, value in i.items():
        try:
            out[f"{name}Json"] = _json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            out[f"{name}Json"] = _json.dumps(str(value), ensure_ascii=False)
    return ok(task, out, [f"[json_text] serialized {len(out)} value(s)"])


@worker_task(task_definition_name="review_gate_policy")
def review_gate_policy(task):
    """Decide what a human may do at a review checkpoint.

    Three one-line jq tasks used to compute this: a clamp on the investigation
    budget, the remaining-passes check, and the approve/human routing. Keeping
    them together makes the policy readable in one place and testable directly.
    """
    i = task.input_data or {}
    requested = i.get("requested")
    limit = 5
    if isinstance(requested, (int, float)) and not isinstance(requested, bool):
        limit = max(0, min(5, int(requested)))
    count = i.get("count")
    used = int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else 0
    can_investigate = used < limit
    actions = ["approve", "investigate", "revise", "stop", "later"] if can_investigate \
        else ["approve", "revise", "stop", "later"]
    # A human-approval deployment always opens the gate; an automated one opens
    # it only when the reviewer actually approved.
    gate = "true" if (_bool(i.get("approve")) or str(i.get("approvalMode") or "") == "human") \
        else "false"
    return ok(task, {"limit": limit, "canInvestigate": can_investigate,
                     "actions": actions, "gate": gate},
              [f"[review_gate_policy] limit={limit} used={used} gate={gate}"])


# --- issue_to_pr -------------------------------------------------------------

@worker_task(task_definition_name="issue_to_pr_delivery")
def issue_to_pr_delivery(task):
    i = task.input_data or {}
    out = issue_to_pr.normalize_issue_delivery(
        child=i.get("child"), publication_commit=i.get("publicationCommit"),
        workspace_state=i.get("workspaceState"), fallback_commit=i.get("fallbackCommit"))
    return ok(task, out, [f"[issue_to_pr_delivery] state={out['state']} commit={str(out['commit'])[:12]}"])


@worker_task(task_definition_name="issue_to_pr_verified_delivery")
def issue_to_pr_verified_delivery(task):
    i = task.input_data or {}
    out = issue_to_pr.normalize_verified_delivery(delivery=i.get("delivery"),
                                                  verification=i.get("verification"))
    return ok(task, out, [f"[issue_to_pr_verified_delivery] state={out['state']}"])


@worker_task(task_definition_name="issue_to_pr_publication_plan")
def issue_to_pr_publication_plan(task):
    i = task.input_data or {}
    out = issue_to_pr.resolve_publication_plan(
        i.get("deliveryOutcome"), agent_authored_test=i.get("agentAuthoredTest"))
    return ok(task, out, [f"[issue_to_pr_publication_plan] draft={out['draft']}"])


@worker_task(task_definition_name="issue_to_pr_publication_result")
def issue_to_pr_publication_result(task):
    i = task.input_data or {}
    out = issue_to_pr.normalize_publication_result(push=i.get("push"), pr=i.get("pr"))
    return ok(task, out, [f"[issue_to_pr_publication_result] state={out['publicationState']}"])


# --- Shared human/agent gate decisions (issue_to_pr, address_pr, pr_review) --

@worker_task(task_definition_name="pr_decision")
def pr_decision(task):
    """issue_to_pr's PR-approval gate: approve/revise/stop, with a title+body fallback."""
    i = task.input_data or {}
    decision = gate_decision.resolve_gate_decision(i.get("gate"))
    gate = decision["gate"]
    artifact = gate.get("artifact") if isinstance(gate.get("artifact"), dict) else {}
    title = gate.get("title") or artifact.get("title") or i.get("fallbackTitle")
    body = gate.get("body") or artifact.get("body") or i.get("fallbackBody")
    out = {"action": decision["action"], "feedback": decision["feedback"], "title": title, "body": body}
    return ok(task, out, [f"[pr_decision] action={out['action']}"])


@worker_task(task_definition_name="address_decision")
def address_decision(task):
    """address_pr's reply-approval gate: approve/revise/stop, with a body fallback."""
    i = task.input_data or {}
    decision = gate_decision.resolve_gate_decision(i.get("gate"))
    gate = decision["gate"]
    artifact = gate.get("artifact") if isinstance(gate.get("artifact"), dict) else {}
    body = gate.get("body") or artifact.get("body") or i.get("fallbackBody")
    out = {"action": decision["action"], "feedback": decision["feedback"], "body": body}
    return ok(task, out, [f"[address_decision] action={out['action']}"])


@worker_task(task_definition_name="review_decision")
def review_decision(task):
    """pr_review's review gate: approve/investigate/revise/stop, with a review fallback."""
    i = task.input_data or {}
    decision = gate_decision.resolve_gate_decision(i.get("gate"), can_investigate=i.get("canInvestigate") is True)
    gate = decision["gate"]
    review = gate.get("review") or gate.get("artifact") or i.get("fallbackReview")
    out = {"action": decision["action"], "feedback": decision["feedback"], "review": review}
    return ok(task, out, [f"[review_decision] action={out['action']}"])


# --- pr_review ---------------------------------------------------------------

@worker_task(task_definition_name="pr_review_summary")
def pr_review_summary(task):
    i = task.input_data or {}
    out = gate_decision.summarize_review(i.get("review"))
    return ok(task, out, [f"[pr_review_summary] verdict={out['verdict']} comments={len(out['comments'])}"])


@worker_task(task_definition_name="pr_review_investigation")
def pr_review_investigation(task):
    i = task.input_data or {}
    out = pr_review.normalize_investigation(
        structured=i.get("structured"), status=i.get("status"), error=i.get("error"),
        session_id=i.get("sessionId"), prior_session_id=i.get("priorSessionId"),
        question=i.get("question"), prior_review=i.get("priorReview"), history=i.get("history"),
        prior_tokens=i.get("priorTokens"), tokens=i.get("tokens"),
        prior_cost=i.get("priorCost"), cost=i.get("cost"))
    return ok(task, out, [f"[pr_review_investigation] count={out['count']}"])


# --- address_pr ---------------------------------------------------------------

@worker_task(task_definition_name="address_pr_reply")
def address_pr_reply(task):
    i = task.input_data or {}
    text = pr_reply.compose_parallel_reply(
        pr_number=i.get("prNumber"), head=i.get("head"), proposal_text=i.get("proposalText"),
        subtasks=i.get("subtasks"), findings=i.get("findings"), verified=i.get("verified"))
    return ok(task, {"reply": text}, ["[address_pr_reply] composed"])


@worker_task(task_definition_name="address_pr_revision_reply")
def address_pr_revision_reply(task):
    i = task.input_data or {}
    text = pr_reply.compose_revision_reply(
        proposal_text=i.get("proposalText"), subtasks=i.get("subtasks"), findings=i.get("findings"))
    return ok(task, {"reply": text}, ["[address_pr_revision_reply] composed"])


# --- publish_salvage -----------------------------------------------------------

@worker_task(task_definition_name="build_salvage_plan")
def build_salvage_plan(task):
    i = task.input_data or {}
    out = publish_salvage.build_salvage_plan(i.get("all"))
    return ok(task, out, [f"[build_salvage_plan] canPublish={out['canPublish']}"])


@worker_task(task_definition_name="compose_salvage_body")
def compose_salvage_body(task):
    i = task.input_data or {}
    body = publish_salvage.compose_salvage_body(i.get("plan"))
    return ok(task, {"body": body}, ["[compose_salvage_body] composed"])


@worker_task(task_definition_name="salvage_outcome")
def salvage_outcome(task):
    i = task.input_data or {}
    out = publish_salvage.resolve_salvage_outcome(i.get("number"))
    return ok(task, out, [f"[salvage_outcome] state={out['state']}"])


# --- github_demo ---------------------------------------------------------------

@worker_task(task_definition_name="github_demo_publication_plan")
def github_demo_publication_plan(task):
    i = task.input_data or {}
    state = i.get("verificationState")
    out = {"draft": state != "passed", "outcome": state if isinstance(state, str) and state else "blocked"}
    return ok(task, out, [f"[github_demo_publication_plan] draft={out['draft']}"])


# --- code_subtask ---------------------------------------------------------------

@worker_task(task_definition_name="code_subtask_delivery_outcome")
def code_subtask_delivery_outcome(task):
    i = task.input_data or {}
    out = code_subtask.resolve_delivery_outcome(
        planned_paths=i.get("plannedPaths"), commit=i.get("commit"), audit=i.get("audit"),
        agent=i.get("agent"), tested=i.get("tested"), test_state=i.get("testState"),
        tests_passed=i.get("testsPassed"))
    return ok(task, out, [f"[code_subtask_delivery_outcome] state={out['state']} passed={out['passed']}"])
