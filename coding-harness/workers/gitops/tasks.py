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
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import git, github
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


@worker_task(task_definition_name="workspace_prepare")
def workspace_prepare(task):
    """Create or resume an isolated run worktree from a local checkout or temp clone.

    Uncommitted source changes are intentionally reported but excluded: git worktrees
    always start from a committed ref. An inherited workspacePath is passed through so
    nested workflows share the parent's run workspace without owning its cleanup.
    """
    i = task.input_data or {}
    try:
        inherited = str(i.get("workspacePath") or "").strip()
        if inherited:
            workspace = str(Path(inherited).expanduser().resolve())
            inside = git.git(workspace, "rev-parse", "--is-inside-work-tree", check=False)
            if inside.code != 0 or inside.stdout.strip() != "true":
                raise ValueError(f"inherited workspacePath is not a git worktree: {workspace}")
            source = str(Path(i.get("repoPath") or workspace).expanduser().resolve())
            return ok(task, {
                "sourceRepoPath": source,
                "worktreePath": workspace,
                "branch": git.git(workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                "baseCommit": git.head(workspace),
                "ignoredSourceChanges": 0,
                "ignoredSourcePaths": [],
                "materializedSourcePaths": [],
                "owned": False,
                "resumed": True,
                "sourceCloned": False,
            }, [f"[workspace_prepare] inherited {workspace}"])

        source_value = str(i.get("repoPath") or "").strip()
        source_cloned = False
        if source_value:
            source = str(Path(source_value).expanduser().resolve())
        else:
            repo_url = str(i.get("repoUrl") or "").strip()
            if not repo_url:
                raise ValueError("workspace_prepare requires repoPath or repoUrl")
            github.ensure_git_auth()
            run_id = str(i.get("workflowId") or getattr(task, "workflow_instance_id", "")
                         or getattr(task, "task_id", "workspace"))
            source = str(Path(i.get("cloneDest") or
                              f"/tmp/conductor-source-{git._safe_name(run_id)}").resolve())
            inside = git.git(source, "rev-parse", "--is-inside-work-tree", check=False)
            if inside.code != 0:
                if os.path.exists(source):
                    raise ValueError(f"clone destination exists but is not a git repository: {source}")
                git.clone(github.clone_url(repo_url), source)
            source_cloned = True

        prepared = git.ensure_ready(source)
        git.exclude_worktrees(source)
        changes = sorted(git.status_files(source))
        materialized_roots = _relative_roots(i.get("materializedSourcePaths"))
        materialized = [path for path in changes if any(
            path == root or path.startswith(root + "/") for root in materialized_roots)]
        ignored = [path for path in changes if path not in materialized]

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

        out = git.workspace_add(
            source,
            str(i.get("workflowId") or getattr(task, "workflow_instance_id", "")
                or getattr(task, "task_id", "workspace")),
            branch_name=str(i.get("branch") or "").strip() or None,
            start_point=str(i.get("startPoint") or "HEAD"),
            preserve_existing=_bool(i.get("preserveExisting"), True),
        )
        output = {
            "sourceRepoPath": source,
            "worktreePath": out["worktreePath"],
            "branch": out["branch"],
            "baseCommit": out["initialCommit"],
            "ignoredSourceChanges": len(ignored),
            "ignoredSourcePaths": ignored,
            "materializedSourcePaths": materialized,
            "owned": True,
            "resumed": out["resumed"],
            "sourceCloned": source_cloned,
            "sourceHead": prepared["head"],
        }
        return ok(task, output, [
            f"[workspace_prepare] {source} -> {out['worktreePath']} branch={out['branch']}",
            f"[workspace_prepare] ignored source changes={len(ignored)} materialized={len(materialized)} resumed={out['resumed']}",
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
        out = git.ensure_ready(
            i["repoPath"],
            name=i.get("identityName") or "conductor-code",
            email=i.get("identityEmail") or "harness@conductor.local",
        )
        return ok(task, out, [f"[prepare_repo] init={out['initialized']} "
                              f"initialCommit={out['initialCommitCreated']} branch={out['branch']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "prepare_repo", e)


@worker_task(task_definition_name="create_branch")
def create_branch(task):
    i = task.input_data or {}
    try:
        out = git.branch(i["repoPath"], i["name"])
        return ok(task, out, [f"[create_branch] {out['branch']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "create_branch", e)


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
    from common.claude import run_agent

    i = task.input_data or {}
    repo = i["repoPath"]
    ids = i.get("groupIds")
    ids = ids.split(",") if isinstance(ids, str) else (ids or [])
    ids = [x.strip() for x in ids if x and x.strip()]
    model = i.get("modelBuilder") or None

    merged, conflicts, resolved = [], [], []
    total_tokens, total_cost = 0, 0.0
    logs = [f"[merge_worktrees] merging: {', '.join(ids)}"]
    try:
        for gid in ids:
            br = git.GROUP_BRANCH.format(name=gid)
            try:
                git.git(repo, "merge", "--no-edit", br)
                merged.append(br)
                logs.append(f"[merge_worktrees] merged {br} cleanly")
            except Exception:  # noqa: BLE001
                conflicted = git.has_conflicts(repo)
                if not conflicted:
                    logs.append(f"[merge_worktrees] merge error on {br} (no conflict markers)")
                    continue
                conflicts.append(br)
                logs.append(f"[merge_worktrees] conflict on {br}: {', '.join(conflicted)}")
                prompt = (
                    f"Resolve ALL git merge conflicts in these files: {', '.join(conflicted)}. "
                    "Keep both sides' changes where possible. Remove every conflict marker "
                    "(<<<<<<<, =======, >>>>>>>). Edit only the conflicted files."
                )
                res = run_agent(prompt, cwd=repo, model=model, write=True,
                                max_budget_usd=float(i.get("maxBudgetUsd") or 50.0))
                total_tokens += res["tokens"]
                total_cost += res["cost_usd"]
                if res["ok"]:
                    git.git(repo, "add", "-A")
                    git.git(repo, "commit", "-m", f"merge_worktrees: resolve conflict from {br}", check=False)
                    resolved.append(br)
                    logs.append(f"[merge_worktrees] resolved {br} (tokens={res['tokens']} cost=${res['cost_usd']:.4f})")
                else:
                    git.git(repo, "merge", "--abort", check=False)
                    logs.append(f"[merge_worktrees] FAILED to resolve {br}: {res.get('error')}")
            git.worktree_remove(repo, gid)
        git.git(repo, "worktree", "prune", check=False)
        logs.append(f"[merge_worktrees] merged={len(merged)} conflicts={len(conflicts)} resolved={len(resolved)}")
        return ok(task, {
            "merged": merged, "conflicts": conflicts, "resolved": resolved,
            "tokenUsed": total_tokens, "costUsd": round(total_cost, 6),
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
                       force_with_lease=_bool(i.get("forceWithLease")))
        return ok(task, out, [f"[git_push] {out['remote']} {out['branch']} HEAD={out['head'][:7]}"])
    except Exception as e:  # noqa: BLE001
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
    ({summary, verdict, comments[]}; dict or JSON string). Never APPROVEs."""
    i = task.input_data or {}
    try:
        repo_ref = i.get("repo") or i.get("repoUrl") or ""
        structured = i.get("structured")
        if isinstance(structured, str):
            structured = _json.loads(structured) if structured.strip() else {}
        structured = structured or {}
        verdict = str(structured.get("verdict") or "comment").lower()
        event = "REQUEST_CHANGES" if verdict == "request_changes" else "COMMENT"
        out = github.submit_review(
            repo_ref, _int(i["number"]),
            summary=structured.get("summary") or "Automated review.",
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
    try:
        out = github.pr_create(i["repoPath"], title=i.get("title") or "",
                               body=i.get("body") or "", base=i.get("base") or None,
                               head_branch=i.get("head") or None,
                               draft=_bool(i.get("draft")), fill=_bool(i.get("fill")))
        return ok(task, out, [f"[pr_create] #{out['number']} {out['url']}"])
    except Exception as e:  # noqa: BLE001
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
