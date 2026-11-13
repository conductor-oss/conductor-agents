"""coding_agent — an UNATTENDED Claude Agent SDK coding worker.

This is the doc §17 reference configuration wired to Conductor: given a worktree
and a task prompt, it runs one locked-down autonomous session
(``dontAsk`` + explicit allowlist + a worktree-escape guard hook) and reports
what changed, cost, and the session id for retry-as-resume.

Inputs (task.input_data):
  prompt            (required) the task instruction for the agent
  worktreePath      (required) working dir AND the write boundary
  modelRole         (optional) policy role: design|plan|code|review|judge (default code)
  modelResolution   (optional) output from model_profile_resolve; direct internal runs
                    resolve the supplied policy envelope themselves when it is absent
  agent / model     (optional) explicit backend/model override for this one task role
  fallbackModel     (optional) model to fall back to when the primary is overloaded
  effort            (optional) low|medium|high|xhigh|max
  maxTurns          (optional) tool-use round-trip cap (default 50)
  maxBudgetUsd      (optional) spend cap (default 50.0)
  resumeSessionId   (optional) resume a prior session — MUST use the same worktree
  allowedWriteRoots (optional) repository-relative paths that tighten the normal
                    worktree write boundary (campaign tasks use their plan's files)
  contextFiles     (optional) internal OpenSpec snapshot files to append read-only;
                   every path must be below OPENSPEC_SNAPSHOT_DIR and the combined
                   content is capped at 512 KiB
  contextPaths     (optional) live, absolute files/directories supplied by the user.
                   Their contents are never read by this worker or appended to the
                   prompt; agents receive only canonical path references.
  failSoft          (optional) report agent exhaustion/errors in output while completing
                    the worker task, so an interactive campaign can pause and resume it
  schema            (optional) JSON Schema (dict or JSON string) forcing structured output
  allowedDomains    (optional) network domains the OS sandbox may reach (list or
                    comma-separated), e.g. "registry.npmjs.org" for npm install.
                    Default: none — sandboxed commands have NO network access.
  promptTemplate    (optional) full-override prompt text, OR "@repo/rel/path" to read the
                    prompt from a file in the worktree. When set it REPLACES the built-in
                    `prompt`; {{key}} placeholders are filled from promptContext.
  promptTemplateSource (optional) caller-supplied provenance label. It is reported as
                    requestedSource but never used to resolve or trust prompt content.
  includeRepoGuide  (optional, default true) prepend the repo guide (AGENTS.md/AGENT.md/
                    CLAUDE.md, if present in the worktree) to the prompt. Env override:
                    CODING_AGENT_REPO_GUIDE=0 disables it fleet-wide.
  templateKey       (optional) when promptTemplate is empty, look for a repo-resident
                    override at <worktree>/.conductor/<templateKey>.md (see resolve_prompt).
  promptContext     (optional) map of named runtime values ({diff, feedback, instruction,
                    files, …}) used to fill {{key}} placeholders in the chosen template;
                    unused non-empty entries are appended under a "## Context" trailer.
                    Precedence: promptTemplate > repo .conductor/<key>.md > built-in prompt.

Output: filesChanged, result/structured, sessionId, turns, tokenUsed, costUsd,
        denials, status, and promptTemplate provenance (requested/resolved source,
        template key, SHA-256). Never raises for an agent that merely failed — the
        status field carries the SDK result subtype so the workflow can branch.
"""

from __future__ import annotations

import asyncio
import collections
import fcntl
import hashlib
import json as _json
import os
import posixpath
import re
import shutil
import stat
import tempfile
import threading

from conductor.client.worker.worker_task import worker_task

from common import git
from common.coding_agent import _infer_backend, run_coding_agent
from common.model_policy import ModelPolicyError, select_role_tier
from common.progress import ProgressReporter
from common.results import cap, fail, ok
from common.session_store import store_from_env
from common.templating import resolve_prompt_details
from common.tool_policy import denied_without_changes
from common.verification import is_generated_path

# Operator knob (doc §10 item 5): which filesystem settings load. Default "project"
# loads the repo's CLAUDE.md conventions but also its .claude/settings.json
# hooks/allow-rules. For untrusted repos set CODING_AGENT_SETTING_SOURCES= (empty)
# or "none" so nothing repo-controlled reaches the agent. Per-task `settingSources`
# input overrides this.
_ENV_SETTING_SOURCES = os.environ.get("CODING_AGENT_SETTING_SOURCES")

# Shared-volume session store for cross-host resume (doc §10 item 6). Constructed
# once from CODING_AGENT_SESSION_STORE_DIR; None (and host-local sessions) if unset.
_SESSION_STORE = store_from_env()

# Conductor may redeliver a still-SCHEDULED task before the worker's final result
# is posted.  A planning/coding session is expensive and not safe to run twice in
# the same checkout, so coalesce duplicate deliveries by task ID.  Completed
# entries are kept briefly to answer a late redelivery with the identical result.
_TASK_FLIGHT_LOCK = threading.Lock()
_TASK_FLIGHTS: dict[str, threading.Event] = {}
_TASK_RESULTS: collections.OrderedDict[str, object] = collections.OrderedDict()
_TASK_RESULT_CACHE_LIMIT = 256


def _claim_task_flight(task_id: str) -> tuple[bool, threading.Event, object | None]:
    """Claim a task's sole agent session, or join its in-flight/cached result."""
    with _TASK_FLIGHT_LOCK:
        if task_id in _TASK_RESULTS:
            return False, threading.Event(), _TASK_RESULTS[task_id]
        event = _TASK_FLIGHTS.get(task_id)
        if event is not None:
            return False, event, None
        event = threading.Event()
        _TASK_FLIGHTS[task_id] = event
        return True, event, None


def _finish_task_flight(task_id: str, event: threading.Event, result: object) -> None:
    with _TASK_FLIGHT_LOCK:
        _TASK_FLIGHTS.pop(task_id, None)
        _TASK_RESULTS[task_id] = result
        _TASK_RESULTS.move_to_end(task_id)
        while len(_TASK_RESULTS) > _TASK_RESULT_CACHE_LIMIT:
            _TASK_RESULTS.popitem(last=False)
        event.set()


def _task_flight_result(task_id: str) -> object | None:
    with _TASK_FLIGHT_LOCK:
        return _TASK_RESULTS.get(task_id)


def _remove_context_snapshot(path: str | None) -> None:
    """Remove a read-only context snapshot without leaving worktree debris."""
    if not path or not os.path.lexists(path):
        return
    for parent, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for filename in files:
            try:
                os.chmod(os.path.join(parent, filename), 0o600, follow_symlinks=False)
            except OSError:
                pass
        for dirname in dirs:
            try:
                os.chmod(os.path.join(parent, dirname), 0o700, follow_symlinks=False)
            except OSError:
                pass
        try:
            os.chmod(parent, 0o700, follow_symlinks=False)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)


def _parse_schema(schema):
    if isinstance(schema, str) and schema.strip():
        return _json.loads(schema)
    return schema or None


_CAMPAIGN_PLAN_REQUIRED_FIELDS = {
    "id", "description", "dependsOn", "files", "acceptanceCriteria", "checks",
}
_CAMPAIGN_PLAN_CONTRACT = (
    "Campaign plan contract: every task id MUST be a lowercase slug matching "
    "^[a-z0-9][a-z0-9_-]*$; dependency ids MUST match task ids exactly; every task "
    "MUST list at least one concrete repository-relative file/write root (no globs); acceptanceCriteria "
    "MUST be non-empty. Do not emit standalone verification tasks with files: []; "
    "attach verification commands to a file-scoped task or list the files being verified."
)


def _constrain_campaign_plan(prompt: str, schema):
    """Add the explicit campaign DAG contract to matching planner requests.

    Detection is structural, so other ``templateKey=plan`` workflows with a
    different output contract are unchanged. The returned schema deliberately
    remains provider-portable; the backend-independent hard gate is
    ``_normalize_campaign_plan_output`` below.
    """
    parsed = _parse_schema(schema)
    if not isinstance(parsed, dict):
        return prompt, parsed, False
    tasks = (parsed.get("properties") or {}).get("tasks")
    item = (tasks or {}).get("items") if isinstance(tasks, dict) else None
    properties = (item or {}).get("properties") if isinstance(item, dict) else None
    required = set((item or {}).get("required") or []) if isinstance(item, dict) else set()
    if (not isinstance(properties, dict)
            or not _CAMPAIGN_PLAN_REQUIRED_FIELDS.issubset(required)
            or not _CAMPAIGN_PLAN_REQUIRED_FIELDS.issubset(properties)):
        return prompt, parsed, False

    return prompt.rstrip() + "\n\n" + _CAMPAIGN_PLAN_CONTRACT, parsed, True


def _normalize_campaign_plan_output(structured):
    """Apply the authoritative campaign contract after any model backend returns."""
    from campaign.model import validate_plan

    validation = validate_plan(structured)
    return {"tasks": validation["tasks"]}, validation


def _as_list(val):
    """Accept a list or comma-separated string; return a list or None if empty.
    Used for the tool-restriction inputs (tools/allowedTools/disallowedTools)."""
    if val is None:
        return None
    if isinstance(val, list):
        return val or None
    items = [x.strip() for x in str(val).split(",") if x.strip()]
    return items or None


def _bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _normalize_repo_path(value: object) -> str | None:
    raw = str(value or "").replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        return None
    norm = posixpath.normpath(raw)
    if norm in {"", ".", ".."} or norm.startswith("../"):
        return None
    return norm[2:] if norm.startswith("./") else norm


def _within_write_roots(path: str, roots: list[str] | None) -> bool:
    if not roots:
        return True
    norm = _normalize_repo_path(path)
    if norm is None:
        return False
    for root in roots:
        base = _normalize_repo_path(root)
        if base is None:
            continue
        if norm == base or norm.startswith(base + "/"):
            return True
    return False


_LOCKFILES = frozenset({"go.sum", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "poetry.lock",
                        "Pipfile.lock", "Gemfile.lock", "composer.lock", "uv.lock"})


def denied_write_paths(paths: list[str], worktree: str, write_roots: list[str] | None) -> list[str]:
    """Repo-relative paths the worktree guard refused because they lie outside this task's write roots.

    This is the Lead's evidence that an item's scope was sliced too narrowly: st-lru's acceptance
    criteria needed eviction hooks in ``store/store.go`` (owned by a sibling item), every attempt was
    denied there, and the item exhausted its review rounds re-wiring eviction elsewhere (run fc6467b7).
    ``team_standup`` widens the item's write roots from this record once the owning item has landed.
    Paths that escape the worktree are a different denial (never widened) and are left out, as are
    generated files (bytecode, build output) and lockfiles a tool rewrites as a side effect (go.sum after
    `go mod tidy`), which no item owns; with no write roots the guard denies nothing on scope, so the
    list is empty.
    """
    if not write_roots:
        return []
    root = os.path.realpath(worktree)
    out: list[str] = []
    for raw in paths or []:
        raw = str(raw)
        target = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(root, raw))
        if target == root or not target.startswith(root + os.sep):
            continue
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        if (not _within_write_roots(rel, write_roots) and not is_generated_path(rel)
                and os.path.basename(rel) not in _LOCKFILES and rel not in out):
            out.append(rel)
    return sorted(out)


def _worktree_target(worktree: str, path: str) -> str:
    """Resolve a Git-reported path lexically without allowing worktree escape."""
    norm = _normalize_repo_path(path)
    if norm is None:
        raise ValueError(f"unsafe repository path: {path!r}")
    root = os.path.abspath(worktree)
    target = os.path.abspath(os.path.join(root, *norm.split("/")))
    if os.path.commonpath([root, target]) != root:
        raise ValueError(f"repository path escapes worktree: {path!r}")
    return target


def _path_state(worktree: str, path: str) -> tuple[str, object, int]:
    """Capture exact working-tree state for one dirty Git path."""
    target = _worktree_target(worktree, path)
    if not os.path.lexists(target):
        return ("absent", None, 0)
    info = os.lstat(target)
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return ("symlink", os.readlink(target), mode)
    if stat.S_ISREG(info.st_mode):
        with open(target, "rb") as handle:
            return ("file", handle.read(), mode)
    if stat.S_ISDIR(info.st_mode):
        # Git reports untracked files individually with --untracked-files=all;
        # a directory marker therefore has no contents of its own to preserve.
        return ("directory", None, mode)
    return ("other", (stat.S_IFMT(info.st_mode), info.st_size), mode)


def _restore_path_state(worktree: str, path: str,
                        state: tuple[str, object, int]) -> None:
    """Restore dirty content exactly instead of destructively reverting to HEAD."""
    target = _worktree_target(worktree, path)
    kind, payload, mode = state
    if os.path.lexists(target):
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
    if kind == "absent":
        return
    parent = os.path.dirname(target)
    root = os.path.realpath(worktree)
    os.makedirs(parent, exist_ok=True)
    if os.path.commonpath([root, os.path.realpath(parent)]) != root:
        raise ValueError(f"repository path parent escapes worktree: {path!r}")
    if kind == "file":
        with open(target, "wb") as handle:
            handle.write(payload)
        os.chmod(target, mode)
    elif kind == "symlink":
        os.symlink(str(payload), target)
    elif kind == "directory":
        os.makedirs(target, exist_ok=True)
        os.chmod(target, mode)
    else:
        raise ValueError(f"cannot restore unsupported dirty path type: {path!r}")


def _reconcile_agent_changes(
    worktree: str,
    write_roots: list[str] | None,
    before_states: dict[str, tuple[str, object, int]],
    index_before: tuple[str, bytes | None],
    branch_before: str,
    head_before: str,
    can_commit: bool = True,
    shared: bool = False,
) -> dict:
    """Fail closed after every agent exit, including exceptions and cancellation.

    ``can_commit`` is False for a session with no Bash tool (a planner with Read/Grep/Glob): it
    cannot have moved HEAD, so a moved HEAD is another actor's commit on the shared worktree and
    is left alone (run 57f8d275, where resetting it erased the contract commits)."""
    branch_after = git.git(
        worktree, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    head_after = git.head(worktree)
    branch_changed = branch_after != branch_before
    head_changed = head_after != head_before
    if shared:
        # ONE tree per team: teammates commit (HEAD moves), stage (the index changes) and write their own
        # files beside this session the whole time. None of that is this agent's doing and none of it
        # may be reset, restored or reverted; its own delivery is whatever it touched within its roots.
        after_codes = git.status_changes(worktree, untracked_files_all=True)
        after = set(after_codes)
        after_states = {path: _path_state(worktree, path) for path in set(before_states) | after}
        touched = (after - set(before_states)) | {p for p in before_states if after_states[p] != before_states[p]}
        changed = sorted(p for p in touched if _within_write_roots(p, write_roots))
        return {"changed": changed, "afterCodes": after_codes, "unauthorized": [], "indexChanged": False,
                "branchChanged": False, "headChanged": False}
    if branch_changed:
        # A coding session has no authority to change branches. Return to the
        # starting branch and restore caller-owned dirty files exactly. Any
        # authorized work on the illicit branch is discarded and the caller
        # receives a terminal policy denial.
        switch_args = (["switch", "--discard-changes", "--detach", head_before]
                       if branch_before == "HEAD"
                       else ["switch", "--discard-changes", branch_before])
        switched = git.git(worktree, *switch_args, check=False)
        if switched.code != 0:
            raise ValueError(
                f"agent changed branch from {branch_before!r} to {branch_after!r} "
                "and the harness could not restore it"
            )
        if git.head(worktree) != head_before:
            git.git(worktree, "reset", "--mixed", head_before)
        for path, state in before_states.items():
            _restore_path_state(worktree, path, state)
    elif head_changed and can_commit:
        # Agents hand uncommitted files to the dedicated commit worker. Moving
        # HEAD would otherwise hide paths from status-based scope enforcement.
        git.git(worktree, "reset", "--mixed", head_before)
    elif head_changed:
        head_changed = False

    index_after = git.index_snapshot(worktree)
    index_changed = index_after[1] != index_before[1]
    if index_changed:
        git.restore_index(index_before)

    before = set(before_states)
    after_codes = git.status_changes(worktree, untracked_files_all=True)
    after = set(after_codes)
    after_states = {path: _path_state(worktree, path) for path in before | after}
    agent_touched = (after - before) | {
        path for path in before if after_states[path] != before_states[path]
    }
    unauthorized = sorted(
        path for path in agent_touched
        if not _within_write_roots(path, write_roots)
    )
    for path in unauthorized:
        if path in before_states:
            _restore_path_state(worktree, path, before_states[path])
        else:
            git.restore_path(worktree, path)
    if unauthorized:
        after_codes = git.status_changes(worktree, untracked_files_all=True)
        after = set(after_codes)
    changed = sorted(agent_touched - set(unauthorized)) or sorted(after)
    return {
        "changed": changed,
        "afterCodes": after_codes,
        "unauthorized": unauthorized,
        "indexChanged": index_changed,
        "branchChanged": branch_changed,
        "headChanged": head_changed,
    }


# Paths a session's report never lists one by one: an installed dependency tree, a build cache, a
# virtualenv. An implementer's `npm install` put 2,960 node_modules paths in filesChanged, the
# sub-workflow stored that report in a variable, and Conductor refused the 731 KB payload (its
# limit is 256 KB): the item failed with its work done (run a8167484, team 4d5b18ce).
_REPORT_SKIP = ("node_modules/", ".venv/", "venv/", "__pycache__/", ".cc-cache/", ".gradle/", ".pytest_cache/",
                ".mypy_cache/", ".tox/", ".next/", ".turbo/", "target/debug/", "target/release/")
_REPORT_CAP = 400


def report_paths(changed: list[str]) -> list[str]:
    """The changed paths a report carries: no dependency or cache tree, at most ``_REPORT_CAP``."""
    kept = [p for p in changed if not any(f"/{skip}" in f"/{p}" for skip in _REPORT_SKIP)]
    return kept[:_REPORT_CAP]


def _resolve_setting_sources(task_val):
    """Precedence: per-task input > env > default ["project"]. An explicit empty
    value ("", "none", "[]", []) means load nothing (untrusted-repo lockdown)."""
    raw = task_val if task_val is not None else _ENV_SETTING_SOURCES
    if raw is None:
        return ["project"]
    if isinstance(raw, list):
        return raw
    s = str(raw).strip().lower()
    if s in ("", "none", "[]"):
        return []
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def _append_context_files(prompt: str, value) -> str:
    files = [str(item).strip() for item in (_as_list(value) or []) if str(item).strip()]
    if not files:
        return prompt
    root = os.path.realpath(os.environ.get("OPENSPEC_SNAPSHOT_DIR", "/tmp/conductor-openspec"))
    chunks: list[str] = []
    total = 0
    for raw in files:
        path = os.path.realpath(str(raw))
        if path != root and not path.startswith(root + os.sep):
            raise ValueError(f"context file is outside OPENSPEC_SNAPSHOT_DIR: {raw}")
        if not os.path.isfile(path):
            raise ValueError(f"context file does not exist: {raw}")
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        total += len(content.encode())
        if total > 512 * 1024:
            raise ValueError("context files exceed 512 KiB")
        chunks.append(f"## Read-only external context: {os.path.basename(path)}\n{content}")
    return prompt.rstrip() + "\n\n# Authoritative external context\n\n" + "\n\n".join(chunks)


def _live_context_paths(value) -> list[dict]:
    """Validate user-authorized live references without opening their contents."""
    result: list[dict] = []
    for raw in (_as_list(value) or []):
        if not os.path.isabs(str(raw)):
            raise ValueError(f"contextPaths must contain absolute paths: {raw}")
        # lstat first: resolving a symlink would silently expand the caller's grant.
        info = os.lstat(raw)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"context path must not be a symlink: {raw}")
        canonical = os.path.realpath(raw)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError(f"context path must be a regular file or directory: {raw}")
        if not os.access(canonical, os.R_OK):
            raise ValueError(f"context path is not readable: {raw}")
        if stat.S_ISDIR(info.st_mode):
            scanned = 0
            for parent, dirs, files in os.walk(canonical, followlinks=False):
                scanned += len(dirs) + len(files)
                if scanned > 10_000:
                    raise ValueError(f"context directory is too large to safely authorize: {raw}")
                for child in [*dirs, *files]:
                    child_path = os.path.join(parent, child)
                    if os.path.islink(child_path):
                        raise ValueError(f"context directory contains a symlink: {child_path}")
        result.append({"path": canonical, "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                       "device": info.st_dev, "inode": info.st_ino,
                       "mtimeNs": info.st_mtime_ns, "size": info.st_size})
    seen: set[str] = set()
    return [entry for entry in result if not (entry["path"] in seen or seen.add(entry["path"]))]


def _snapshot_external_context_paths(entries: list[dict], worktree: str) -> tuple[list[dict], str | None]:
    """Make externally granted context readable without widening a sandbox.

    Claude can consume external roots directly.  Codex and Gemini cannot prove an
    equivalent additional read-root policy, especially when a parallel child
    worktree receives a design file from its parent checkout.  Snapshot only the
    already-validated user grants into a transient, non-git directory inside the
    task worktree.  The snapshot is removed before change detection/commit.
    """
    root = os.path.realpath(worktree)
    external_paths = {entry["path"] for entry in entries
                      if entry["path"] != root and not entry["path"].startswith(root + os.sep)}
    if not external_paths:
        return entries, None
    snapshot_root = os.path.join(root, ".cc-context")
    _remove_context_snapshot(snapshot_root)
    os.makedirs(snapshot_root, mode=0o700)
    effective: list[dict] = []
    try:
        for index, entry in enumerate(entries):
            source = entry["path"]
            if source not in external_paths:
                effective.append(entry)
                continue
            name = os.path.basename(source.rstrip(os.sep)) or "context"
            digest = hashlib.sha256(source.encode()).hexdigest()[:12]
            target = os.path.join(snapshot_root, f"{index:02d}-{digest}-{name}")
            if entry["kind"] == "directory":
                shutil.copytree(source, target, copy_function=shutil.copy2)
                for parent, dirs, files in os.walk(target):
                    os.chmod(parent, 0o555)
                    for filename in files:
                        os.chmod(os.path.join(parent, filename), 0o444)
            else:
                shutil.copy2(source, target)
                os.chmod(target, 0o444)
            snapshot = dict(entry)
            snapshot["path"] = target
            snapshot["sourcePath"] = source
            snapshot["snapshot"] = True
            effective.append(snapshot)
        return effective, snapshot_root
    except Exception:
        _remove_context_snapshot(snapshot_root)
        raise


def _append_context_paths(prompt: str, paths: list[dict]) -> str:
    if not paths:
        return prompt
    refs = "\n".join(
        f"- {entry.get('sourcePath', entry['path'])} "
        f"(available at {entry['path']}; {entry['kind']}; read-only snapshot)"
        if entry.get("snapshot") else f"- {entry['path']} ({entry['kind']}; read-only)"
        for entry in paths
    )
    return prompt.rstrip() + "\n\n## Live context references\n" + refs + (
        "\n\nThese are user-authorized live filesystem references. Inspect them only when useful "
        "to the task. Do not modify them, copy their contents into the repository unless the task "
        "requires it, or treat them as repository files.")


_HOST_FLIGHT_DIR = os.path.join(tempfile.gettempdir(), "conductor-code-task-flights")


def _host_flight_lock(task_id: str):
    """A host-wide, cross-process claim on one Conductor task: the open, exclusively flocked file
    (held until the primary finishes), or None when another process holds it."""
    os.makedirs(_HOST_FLIGHT_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    fh = open(os.path.join(_HOST_FLIGHT_DIR, f"{safe}.lock"), "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return fh, False
    return fh, True


@worker_task(task_definition_name="coding_agent", thread_count=16)  # 16 agent sessions at once per fleet
def coding_agent(task):
    """Run the async agent implementation under the ordinary task runner.

    The SDK's AsyncTaskRunner can re-poll an unacknowledged task on this local
    Conductor release, launching duplicate agent sessions.  The normal runner
    atomically claims the task before this per-task event loop is entered.

    The claim is held across worker GENERATIONS too: a source reload starts a successor fleet
    beside the draining one, the task was re-polled by the successor, and the duplicate planner
    session ran in the same worktree as the primary; when it ended, its scope gate reset HEAD to
    the commit it had started from and erased two contract commits the harness had made meanwhile,
    so the first implementer's fork read as "edited the frozen contract" and the whole correct
    build was discarded (run 57f8d275). The duplicate now waits for the primary's host-wide lock
    and returns without a session; Conductor ignores an update to a task that is already terminal.
    """
    task_id = str(task.task_id)
    owner, event, cached = _claim_task_flight(task_id)
    if cached is not None:
        return cached
    if not owner:
        # Do not start a second LLM session for the same Conductor task.  The
        # primary invocation owns cancellation/error reporting and publishes the
        # exact result that this delivery should return as well.
        event.wait()
        result = _task_flight_result(task_id)
        if result is not None:
            return result
        return fail(task, "coding_agent", "duplicate delivery ended without a primary result")
    host_lock, primary = _host_flight_lock(task_id)
    if not primary:
        try:
            fcntl.flock(host_lock.fileno(), fcntl.LOCK_EX)  # the primary, in another process, finishes first
        finally:
            host_lock.close()
        result = fail(task, "coding_agent",
                      "duplicate delivery: another worker process ran this task to completion; its result stands")
        _finish_task_flight(task_id, event, result)
        return result
    try:
        result = asyncio.run(_coding_agent(task))
    except Exception as exc:  # noqa: BLE001
        result = fail(task, "coding_agent", exc)
    finally:
        try:
            fcntl.flock(host_lock.fileno(), fcntl.LOCK_UN)
        finally:
            host_lock.close()
    _finish_task_flight(task_id, event, result)
    return result


async def _coding_agent(task):
    i = task.input_data or {}
    try:
        wt = i.get("worktreePath") or ""
        if not wt:
            return fail(task, "coding_agent", "worktreePath is required")
        if not os.path.isdir(wt):
            return fail(task, "coding_agent", f"worktreePath {wt} is not a directory on this machine")
        # A planner or classifier may be the first step to touch a fresh project directory.
        initialized_repo = await asyncio.to_thread(git.ensure_repo, wt)
        role = str(i.get("modelRole") or "code").strip().lower()
        try:
            model_tier, model_resolution = select_role_tier(i, role=role, worktree=wt)
        except ModelPolicyError as exc:
            return fail(task, "coding_agent", exc)
        # A dynamically forked task's prompt arrives as a file (common/prompt_files): the inline copy
        # went through Conductor's ${...} substitution and lost every template literal it quoted.
        prompt_in, context_in = (i.get("prompt") or ""), i.get("promptContext")
        if str(i.get("promptPath") or "").strip():
            from common import prompt_files
            stashed_prompt, stashed_context = prompt_files.load(str(i.get("promptPath")))
            if stashed_prompt.strip():
                prompt_in, context_in = stashed_prompt, (stashed_context or context_in)
        # Effective prompt: explicit promptTemplate > repo .conductor/<key>.md > built-in prompt.
        prompt_resolution = resolve_prompt_details(
            str(prompt_in).strip(),
            template=i.get("promptTemplate"),
            template_key=i.get("templateKey"),
            context=context_in,
            worktree=wt,
        )
        prompt = prompt_resolution.prompt
        prompt = _append_context_files(prompt, i.get("contextFiles"))
        if not prompt.strip():
            return fail(task, "coding_agent",
                        "prompt is required (none of prompt, promptTemplate, or a repo template resolved)")
        prompt, output_schema, campaign_plan_contract = _constrain_campaign_plan(
            prompt, i.get("schema")
        )
        context_paths = _live_context_paths(i.get("contextPaths"))
        backend = _infer_backend(model_tier["agent"], model_tier.get("model"))
        write_roots = _as_list(i.get("allowedWriteRoots"))
        before_codes = await asyncio.to_thread(
            git.status_changes, wt, untracked_files_all=True)
        before_states = {
            path: await asyncio.to_thread(_path_state, wt, path)
            for path in before_codes
        }
        unsupported = sorted(
            path for path, state in before_states.items() if state[0] == "other")
        if unsupported:
            return fail(
                task,
                "coding_agent",
                "cannot safely preserve unsupported dirty path types: "
                + ", ".join(unsupported),
            )
        branch_before = (await asyncio.to_thread(
            git.git, wt, "rev-parse", "--abbrev-ref", "HEAD", check=False
        )).stdout.strip()
        head_before = await asyncio.to_thread(git.head, wt)
        index_before = await asyncio.to_thread(git.index_snapshot, wt)
        # For Codex/Gemini, external inputs become transient snapshots inside the
        # worktree.  This is necessary for parallel child worktrees whose parent
        # checkout contains the design document, and avoids an arbitrary host-path
        # read grant.  Claude retains direct additional read roots.
        agent_context_paths, snapshot_root = (
            _snapshot_external_context_paths(context_paths, wt)
            if backend != "claude" else (context_paths, None)
        )
        prompt = _append_context_paths(prompt, agent_context_paths)

        domains = i.get("allowedDomains")
        if isinstance(domains, str):
            domains = [d.strip() for d in domains.split(",") if d.strip()]
        if not domains:
            from common.coding_agent import DEFAULT_ALLOWED_DOMAINS
            domains = list(DEFAULT_ALLOWED_DOMAINS)
        # Campaign runs can stay active for days; a 10-second heartbeat keeps task
        # ownership visible through worker restarts and long model turns.
        reporter = ProgressReporter(task, heartbeat_s=10.0).start()
        scope_result = None
        # In-session team feedback + checkpoints (closure plan WP2, H11/H17): only when the caller names
        # the board run and item (team_build implementers) or asks for checkpoints.
        from common.feedback import FeedbackWatcher
        def _decider(req: dict) -> None:
            from common import architect_decide
            try:
                architect_decide.decide(req, inputs=i, worktree=wt)
            except Exception as e:  # noqa: BLE001
                log.warning("architect decision failed: %s", e)
        watcher = FeedbackWatcher(
            str(i.get("feedbackCorrelationId") or ""), str(i.get("feedbackItem") or ""),
            since_ts=float(i.get("feedbackSince") or 0) or None,
            every=int(i.get("feedbackEveryTurns") if i.get("feedbackEveryTurns") not in (None, "") else 1),
            checkpoint_every=int(i.get("checkpointEveryTurns") or 0), worktree=wt,
            run_id=str(i.get("feedbackRunId") or ""), team=str(i.get("feedbackTeam") or ""), decider=_decider)
        try:
            res = await run_coding_agent(
                prompt,
                worktree=wt,
                model=model_tier.get("model") or None,
                fallback_model=i.get("fallbackModel") or None,
                effort=i.get("effort") or model_tier.get("effort") or None,
                max_turns=int(model_tier.get("maxTurns") or 50),
                max_budget_usd=(float(model_tier["maxBudgetUsd"]) if model_tier.get("maxBudgetUsd") is not None else 50.0),
                resume_session_id=i.get("resumeSessionId") or None,
                output_schema=output_schema,
                allowed_domains=domains or None,
                setting_sources=_resolve_setting_sources(i.get("settingSources")),
                session_store=_SESSION_STORE,
                on_turn=reporter.update,
                # Optional tightening: restrict the tool surface (e.g. a read-only
                # planner passes tools=["Read","Grep","Glob"]). `tools` is an
                # availability gate — it can only remove built-ins, never add.
                tools=_as_list(i.get("tools")),
                allowed_tools=_as_list(i.get("allowedTools")),
                disallowed_tools=_as_list(i.get("disallowedTools")),
                # Prime the prompt with the dir listing (skips the agent's first
                # ls/Glob turn). Default on; set includeFileTree:false to disable.
                include_file_tree=(str(i.get("includeFileTree", "true")).lower()
                                   not in ("false", "0", "no")),
                # Prime the prompt with the repo guide (AGENTS.md/AGENT.md/CLAUDE.md) so the
                # agent knows how to build/test/review. Default on; includeRepoGuide:false or
                # the CODING_AGENT_REPO_GUIDE=0 worker env disables it.
                include_repo_guide=(str(i.get("includeRepoGuide", "true")).lower()
                                    not in ("false", "0", "no")),
                # Backend: "claude" (default) or "codex". If unset, inferred from the
                # model id (gpt-*/o*/codex-* → codex). See docs/CODING_AGENT_WORKER.md.
                backend=model_tier["agent"],
                write_roots=write_roots,
                context_paths=[entry["path"] for entry in agent_context_paths],
                feedback=watcher if watcher.active else None,
            )
        finally:
            try:
                # stop() joins the heartbeat thread (up to ~2s) — off the loop.
                await asyncio.to_thread(reporter.stop)
                if snapshot_root:
                    await asyncio.to_thread(_remove_context_snapshot, snapshot_root)
            finally:
                # The scope gate is cleanup, not a success-path assertion. It must
                # run when a backend raises or the coroutine is cancelled as well.
                scope_result = await asyncio.to_thread(
                    _reconcile_agent_changes,
                    wt,
                    write_roots,
                    before_states,
                    index_before,
                    branch_before,
                    head_before,
                    (not _as_list(i.get("tools"))) or ("Bash" in _as_list(i.get("tools"))),
                    str(i.get("sharedTree") or "").strip().lower() in ("1", "true", "yes"),
                )
        if scope_result["branchChanged"]:
            res["ok"] = False
            res["status"] = "policy_denied"
            res["error"] = "agent changed the worktree branch; original branch restored"
            res.setdefault("denials", []).append(
                "git branch: reverted agent branch change")
        elif scope_result["headChanged"]:
            res.setdefault("denials", []).append(
                "git history: converted agent commit back to uncommitted changes")
        if scope_result["indexChanged"]:
            res.setdefault("denials", []).append(
                "git index: reverted agent staging changes")
        unauthorized = scope_result["unauthorized"]
        if unauthorized:
            res.setdefault("denials", []).append(
                "write roots: reverted out-of-scope changes: " + ", ".join(unauthorized))
        changed = scope_result["changed"]
        after_codes = scope_result["afterCodes"]
        plan_validation = None
        if campaign_plan_contract:
            normalized_plan, plan_validation = _normalize_campaign_plan_output(
                res.get("structured")
            )
            res["structured"] = normalized_plan
            if not plan_validation["valid"]:
                res["ok"] = False
                res["status"] = "invalid_plan"
                res["error"] = "; ".join(plan_validation["errors"])
        # Per-file status (A/M/D/R) for the changed set — additive, for review UIs. Both lists are
        # bounded (no dependency tree, at most _REPORT_CAP paths); filesChangedTotal keeps the count.
        reported = report_paths(changed)
        file_changes = [{"path": p, "status": after_codes.get(p, "M")} for p in reported]

        logs = [
            f"[coding_agent] backend={backend} model={res.get('model') or '(default)'} status={res['status']} "
            f"{'resumed ' + str(i.get('resumeSessionId'))[:8] if i.get('resumeSessionId') else 'cold-start'} "
            f"session={str(res.get('session_id'))[:8]}",
            f"[coding_agent] prompt-template requested={i.get('promptTemplateSource') or '(auto)'} "
            f"resolved={prompt_resolution.source} key={prompt_resolution.template_key or '(none)'} "
            f"sha256={prompt_resolution.sha256[:12]} campaign-plan-contract={campaign_plan_contract}",
            f"[coding_agent] changed={len(changed)} {changed} turns={res['num_turns']} "
            f"tokens={res['tokens']} cost=${res['cost_usd']:.4f}",
        ]
        if initialized_repo:
            logs.append(f"[coding_agent] {wt} was not a git repository: initialized it (empty base commit, "
                        f"files untouched)")
        for entry in res.get("turn_log") or []:
            logs.append(f"[coding_agent] {entry}")
        for d in res.get("denials") or []:
            logs.append(f"[coding_agent] DENIED {cap(d, 200)}")

        out = {
            "status": res["status"],
            "initializedRepo": initialized_repo,
            # Session completion is only evidence that the agent stopped. It is
            # intentionally distinct from independent build/test verification.
            "agentCompleted": bool(res.get("ok")),
            "verificationState": "pending",
            "agent": backend,
            "model": res.get("model") or "",
            "modelResolution": {
                "profile": model_resolution.get("profile", ""),
                "role": role,
                "tier": model_tier,
                "canonicalSha256": model_resolution.get("canonicalSha256", ""),
                "sources": model_resolution.get("sources", []),
                "warnings": model_resolution.get("warnings", []),
            },
            "filesChanged": reported,
            "filesChangedTotal": len(changed),
            "fileChanges": file_changes,
            "result": cap(res.get("result"), 2000),
            "structured": res.get("structured"),
            # The last StructuredOutput payloads the agent submitted, whole — including the ones the
            # validator rejected — so a consumer can salvage a mangled payload (run a062184f's
            # retrospective) instead of recording the placeholder the agent sent to get past it.
            "structuredAttempts": res.get("structured_attempts") or [],
            "structuredSalvaged": bool(res.get("structured_salvaged")),
            "planValidation": plan_validation or {},
            "sessionId": res.get("session_id") or "",
            # `turns` is the per-turn array (turn number + commands run + tokens);
            # `numTurns` is the scalar count for quick reference.
            "turns": res.get("turns") or [],
            "numTurns": res.get("num_turns", 0),
            "tokenUsed": res.get("tokens", 0),
            "costUsd": res.get("cost_usd", 0.0),
            "denials": res.get("denials") or [],
            # Files inside the worktree the guard refused as outside this task's write roots: the
            # Lead's signal to widen the item's scope (team_standup), kept apart from the log lines.
            # Both ways a write is refused count. Codex edits through its shell, past the file-tool guard,
            # so its out-of-scope edits appear only as reverted changes: a correct fix to reader.go was
            # undone repeatedly and the item never widened (run 1177c2d2).
            "deniedWrites": denied_write_paths([*(res.get("denied_writes") or []), *unauthorized], wt, write_roots),
            "feedbackInjections": res.get("feedbackInjections") or [],
            "interruptedBy": res.get("interruptedBy"),
            "checkpoints": res.get("checkpoints") or [],
            "restoredCheckpoint": res.get("restoredCheckpoint"),
            "contextPaths": context_paths,
            "promptTemplate": {
                "requestedSource": str(i.get("promptTemplateSource") or "auto"),
                "resolvedSource": prompt_resolution.source,
                "templateKey": prompt_resolution.template_key,
                "sha256": prompt_resolution.sha256,
            },
        }

        # A model can end its turn normally after reporting that a required command was denied.
        # The SDK marks that as ok even though no work was produced. Fail closed so parent
        # workflows cannot commit/push a partial PR fix and announce that all feedback was
        # addressed (the PR #6 regression).
        # A session asked for a structured answer (a scout, the Architect, a reviewer) is judged by that
        # answer: "found nothing runnable" with no file changed is its outcome, not a failure to retry
        # three times at sixty turns each (run 6cc1a23b).
        # A run that keeps its spend on its board (the swarm) has it recorded the moment each session
        # ends, so the console shows live spend and the next dispatch sizes its caps on the truth.
        spend_run = str(i.get("spendRunId") or "").strip()
        if spend_run:
            try:
                from common import budget_gate
                budget_gate.record_spend(spend_run, str(i.get("spendAuthor") or "agent"),
                                         usd=float(out.get("costUsd") or 0.0),
                                         tokens=int(out.get("tokenUsed") or 0), model=str(out.get("model") or ""))
            except Exception as e:  # noqa: BLE001 - a board write failure must not fail the session
                logs.append(f"[coding_agent] spend not recorded: {e}")
        answered = bool(i.get("schema")) and bool(out.get("structured"))
        if denied_without_changes(changed, out["denials"]) and not answered:
            err = "agent made no changes after one or more tool denials"
            logs.append(f"[coding_agent] error: {err}")
            out["retryable"] = False
            if _bool(i.get("failSoft"), False):
                out["error"] = err
                return ok(task, out, logs)
            return fail(task, "coding_agent", err, logs, output=out)

        if not res["ok"]:
            err = res.get("error") or f"agent ended with status={res['status']}"
            # The agent's final text often carries the real reason the SDK's generic
            # stream error hides — e.g. an invalid/inaccessible model or an auth error.
            detail = (res.get("result") or "").strip()
            if detail:
                err = f"{err} — {detail[:300]}"
            logs.append(f"[coding_agent] error: {err}")
            if res.get("stderr"):
                logs.append(f"[coding_agent] stderr tail: {cap(res['stderr'], 2000)}")
            # error_max_turns / error_max_budget_usd are retryable via resume — surface
            # the session id and let the workflow decide, rather than hard-failing.
            out["retryable"] = res["status"] in ("error_max_turns", "error_max_budget_usd")
            out["stderr"] = cap(res.get("stderr"), 2000)
            if _bool(i.get("failSoft"), False):
                out["error"] = err
                return ok(task, out, logs)
            return fail(task, "coding_agent", err, logs, output=out)

        return ok(task, out, logs)
    except Exception as e:  # noqa: BLE001
        return fail(task, "coding_agent", e)
