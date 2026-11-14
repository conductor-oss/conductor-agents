"""Git operations on the target repo. Each is exposed as its own discrete worker
task (gitops/tasks.py) for visibility and distribution-readiness. Worktrees live
on a shared filesystem now; ``push``/``pull`` are stubs with real signatures so
moving to multi-host later is a worker-body change only.

Ported from ``git_ops.ts`` + ``integrate.ts``.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .exec import RunError, run

WORKTREES = ".cc-worktrees"
GROUP_BRANCH = "cc-group-{scope}-{name}"


def group_branch(repo: str, name: str) -> str:
    """The fork branch for item ``name`` forked from the shared checkout ``repo``.

    Git branch names are shared by every worktree of one repository, so a bare ``cc-group-<item>``
    collides across runs on the same repo — concurrently, or with a run left behind by a crash or a
    termination (measured live: run 6d8af3af's ``design`` fork failed twice with "'cc-group-design' is
    already used by worktree" held by terminated run 2e888ce3). Each run forks from its own shared
    checkout, so scoping the name by that checkout's real path makes it unique per run and stable for
    every caller that holds the same ``repo``."""
    import hashlib
    scope = hashlib.sha1(os.path.realpath(repo).encode()).hexdigest()[:8]
    return GROUP_BRANCH.format(scope=scope, name=name)
_NON_PRODUCT_PATH_PARTS = {
    ".gradle", ".gradle-local", "build", "target", "out", "dist",
    ".pytest_cache", ".mypy_cache", "coverage", "node_modules",
    # Running the tests writes bytecode next to the sources. Committing it
    # makes the next changed-scope discovery see .pyc paths it cannot map to a
    # test, which blocks verification on an artifact nobody edited.
    "__pycache__", ".ruff_cache", ".tox", ".gradle-cache", "_build", "vendor",
}

# git emits these when two processes touch the same repo/refs at once. The
# parallel code_parallel forks all mutate one repo, so worktree_add/commit can
# collide; we serialize (flock on the shared git dir) and retry these.
_GIT_LOCK_HINTS = ("index.lock", "cannot lock ref", "Unable to create",
                   "another git process", "shallow.lock", "packed-refs.lock")


def git(repo: str, *args: str, check: bool = True,
        env: dict[str, str] | None = None, clean_env: bool = False):
    return run(["git", "-C", repo, *args], check=check, env=env, clean_env=clean_env)


def _trim(s: str) -> str:
    return s.strip()


def _common_gitdir(repo: str) -> str:
    """The SHARED git dir for a repo or any of its worktrees, so a lock taken
    from a worktree serializes against the main repo and sibling worktrees."""
    r = git(repo, "rev-parse", "--git-common-dir", check=False)
    out = _trim(r.stdout)
    if r.code == 0 and out:
        return out if os.path.isabs(out) else os.path.join(repo, out)
    gd = os.path.join(repo, ".git")
    return gd if os.path.isdir(gd) else repo


def common_gitdir(repo: str) -> str:
    """Canonical shared Git directory for a checkout or any linked worktree."""
    return os.path.realpath(_common_gitdir(os.path.abspath(repo)))


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return value[:96] or "workspace"


def validate_repo_path(repo: str) -> str:
    """Reject a repository URL accidentally supplied where a local path is required.

    Resolving ``.../https://github.com/org/repo`` otherwise creates a genuine
    local directory. A worker could initialise it and report a misleading no-op
    instead of explaining that the caller passed the wrong input type.
    """
    raw = str(repo or "").strip()
    if not raw:
        raise ValueError("repoPath is required")
    if "://" in raw or any(part.lower() in {"http:", "https:", "ssh:"}
                         for part in Path(raw).parts):
        raise ValueError("repoPath must be a local filesystem path, not a repository URL")
    return str(Path(raw).expanduser().resolve())


@contextmanager
def _repo_lock(repo: str):
    """Cross-process exclusive lock over a repo's shared git dir (flock)."""
    lock_path = os.path.join(_common_gitdir(repo), "cc-worktree.lock")
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


def _is_git_lock_error(e: Exception) -> bool:
    s = (getattr(e, "stderr", "") or "") + (getattr(e, "stdout", "") or "") + str(e)
    return any(h in s for h in _GIT_LOCK_HINTS)


def _git_retry(fn, attempts: int = 5, base: float = 0.3):
    """Run fn(); retry with backoff only on a transient git-lock error (defense
    for a stale lock the flock can't cover, e.g. a crashed peer)."""
    last: Exception | None = None
    for a in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if not _is_git_lock_error(e) or a == attempts - 1:
                raise
            last = e
            time.sleep(base * (a + 1))
    raise last  # unreachable


def ensure_repo(path: str) -> bool:
    """Make an existing directory a git repository with a HEAD, if it is not one yet; True when it did.

    Any step may be the first to touch a fresh project directory — a program's planner, an intake
    classifier, a bug-fix reproducer — and every one of them reads HEAD (run edc1c893 failed four times on
    `git rev-parse HEAD` in an empty directory before anything else ran). ``ensure_ready`` with
    ``preserve_worktree`` does the work: ``git init``, a local identity if none is configured, and an empty
    base commit that leaves the directory's files exactly as they were. A directory already inside a
    repository with a HEAD is left alone, and a path that does not exist is never created here."""
    if not path or not os.path.isdir(path):
        return False
    if git(path, "rev-parse", "--verify", "HEAD", check=False).code == 0:
        return False
    ensure_ready(path, preserve_worktree=True)
    return True


def ensure_ready(repo: str, *, name: str = "conductor-code",
                 email: str = "harness@conductor.local",
                 preserve_worktree: bool = False) -> dict:
    """Make ``repo`` git-ready so worktree_add/commit won't fail: init if needed,
    set a LOCAL identity only if none is configured, and create an initial commit
    if there is no HEAD. Idempotent — safe to run on an already-prepared repo."""
    repo = validate_repo_path(repo)
    os.makedirs(repo, exist_ok=True)
    inside = git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    initialized = False
    if inside.code != 0 or _trim(inside.stdout) != "true":
        git(repo, "init")
        initialized = True
    repo = str(Path(_trim(git(repo, "rev-parse", "--show-toplevel").stdout)).resolve())
    if not _trim(git(repo, "config", "user.email", check=False).stdout):
        git(repo, "config", "user.email", email)
    if not _trim(git(repo, "config", "user.name", check=False).stdout):
        git(repo, "config", "user.name", name)
    committed = False
    if git(repo, "rev-parse", "--verify", "HEAD", check=False).code != 0:
        if preserve_worktree:
            # Establish an empty base without consuming the caller's staged,
            # unstaged, or untracked files. The outcome branch snapshots them later.
            fd, index_path = tempfile.mkstemp(prefix="conductor-empty-index-")
            os.close(fd)
            os.unlink(index_path)
            env = {"GIT_INDEX_FILE": index_path}
            try:
                git(repo, "read-tree", "--empty", env=env)
                tree = _trim(git(repo, "write-tree", env=env).stdout)
                root_commit = _trim(git(repo, "commit-tree", tree,
                                        "-m", "conductor-code: empty initial base",
                                        env=env).stdout)
                git(repo, "update-ref", "HEAD", root_commit)
            finally:
                try:
                    os.unlink(index_path)
                except FileNotFoundError:
                    pass
        else:
            git(repo, "add", "-A", "--", ".")
            git(repo, "commit", "--allow-empty", "-m", "conductor-code: initial commit")
        committed = True
    branch = _trim(git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout)
    return {"repoPath": repo, "initialized": initialized,
            "initialCommitCreated": committed, "branch": branch,
            "head": _trim(git(repo, "rev-parse", "HEAD").stdout)}


def _require_existing_checkout(repo: str) -> str:
    """Validate the stricter contract used by in-place executions.

    Unlike :func:`ensure_ready`, this never initializes a directory or creates an
    initial commit.  The caller explicitly supplied an existing checkout and it
    must stay that checkout for the duration of the run.
    """
    repo = validate_repo_path(repo)
    inside = git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.code != 0 or _trim(inside.stdout) != "true":
        raise ValueError("inPlace requires repoPath to be an existing git checkout")
    repo = str(Path(_trim(git(repo, "rev-parse", "--show-toplevel").stdout)).resolve())
    if git(repo, "rev-parse", "--verify", "HEAD", check=False).code != 0:
        raise ValueError("inPlace requires a checkout with an existing HEAD commit")
    current = _current_branch(repo)
    if not current or current == "HEAD":
        raise ValueError("inPlace requires repoPath to be on a named local branch")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
        if git(repo, "rev-parse", "--verify", "-q", marker, check=False).code == 0:
            raise ValueError(f"inPlace checkout has an active git operation ({marker})")
    return repo


def status_fingerprint(repo: str) -> str:
    """Stable, content-free identity of the checkout's current working state."""
    import hashlib
    status = git(repo, "status", "--porcelain=v1", check=False).stdout
    return hashlib.sha256(status.encode("utf-8")).hexdigest()


def _branch_exists(repo: str, name: str) -> bool:
    return git(repo, "show-ref", "--verify", f"refs/heads/{name}", check=False).code == 0


def run_specific_branch(requested: str, run_id: str) -> str:
    """Return a stable outcome branch name owned by one workflow execution.

    Callers provide a human-readable base such as ``harness/issue-132``.  The
    workflow execution suffix prevents a later or concurrent run from pushing a
    divergent history to the same remote ref.  Names that already end in this
    run's full or short identity are preserved so nested callers do not append
    the suffix twice.
    """
    safe_run = _safe_name(run_id or "manual")
    short_run = safe_run[:8]
    base = str(requested or "").strip() or "conductor/run"
    owned_suffixes = (
        f"-{safe_run}", f"/{safe_run}", f"-{short_run}", f"/{short_run}",
    )
    candidate = base if base.endswith(owned_suffixes) else f"{base}-{short_run}"
    if run(["git", "check-ref-format", "--branch", candidate], check=False).code != 0:
        raise ValueError(f"invalid outcome branch name: {candidate!r}")
    return candidate


def allocate_outcome_branch(repo: str, requested: str, run_id: str, *,
                            source_branch: str = "") -> str:
    """Return a new run-specific branch that cannot overwrite an existing ref."""
    candidate = run_specific_branch(requested, run_id)
    base = candidate
    if candidate == source_branch or _branch_exists(repo, candidate):
        candidate = f"{base}-2"
    counter = 2
    while candidate == source_branch or _branch_exists(repo, candidate):
        counter += 1
        candidate = f"{base}-{counter}"
    return candidate


def _stage_all_visible_changes(repo: str) -> list[str]:
    """Stage every Git-visible change, including non-ignored generated paths."""
    git(repo, "add", "-A", "--", ":/")
    return sorted(line.strip() for line in
                  git(repo, "diff", "--cached", "--name-only", check=False).stdout.splitlines()
                  if line.strip())


def snapshot_worktree(repo: str, *, run_id: str, original_branch: str,
                      original_head: str) -> dict:
    """Create an unreferenced baseline commit without touching source HEAD/index/files."""
    before_status = status_fingerprint(repo)
    included = sorted(status_changes(repo, untracked_files_all=True))
    marker = f"conductor-workspace:{_safe_name(run_id or 'manual')}:baseline"
    fd, index_path = tempfile.mkstemp(prefix="conductor-workspace-index-")
    os.close(fd)
    os.unlink(index_path)
    env = {"GIT_INDEX_FILE": index_path}
    try:
        git(repo, "read-tree", original_head, env=env)
        git(repo, "add", "-A", "--", ":/", env=env)
        tree = _trim(git(repo, "write-tree", env=env).stdout)
        message = (f"conductor workspace baseline [{marker}]\n\n"
                   f"Conductor-Original-Branch: {original_branch}\n"
                   f"Conductor-Original-Head: {original_head}")
        baseline = _trim(git(repo, "commit-tree", tree, "-p", original_head,
                             "-m", message, env=env).stdout)
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass
    if (_current_branch(repo) != original_branch or head(repo) != original_head
            or status_fingerprint(repo) != before_status):
        raise ValueError("isolated workspace snapshot changed the supplied checkout")
    return {"baselineCommit": baseline, "baselineCreated": True,
            "includedPaths": included, "marker": marker}


def prepare_inplace(repo: str, *, run_id: str = "", branch_name: str = "",
                    phase: str = "baseline") -> dict:
    """Switch to a new run-owned branch, then capture every Git-visible change."""
    repo = _require_existing_checkout(repo)
    run = _safe_name(run_id or "manual")
    current_branch, original_head = _current_branch(repo), head(repo)
    before = status_fingerprint(repo)
    marker = f"conductor-workspace:{run}:{phase}"
    # A restart of the same workflow must not manufacture another baseline.
    # The marker is local-only and bounded to recent history so unrelated old
    # harness runs cannot be mistaken for this execution.
    prior = git(repo, "log", "-n", "200", "--format=%H", "--grep", marker,
                check=False).stdout.strip().splitlines()
    if prior:
        if status_files(repo):
            raise ValueError("inPlace run already has a baseline but the checkout is now dirty; "
                             "refuse to create a second baseline")
        baseline = prior[0].strip()
        parent = git(repo, "rev-parse", f"{baseline}^", check=False).stdout.strip() or baseline
        body = git(repo, "show", "-s", "--format=%B", baseline, check=False).stdout
        original_branch = next((line.split(":", 1)[1].strip() for line in body.splitlines()
                                if line.startswith("Conductor-Original-Branch:")), "")
        return {
            "repoPath": repo,
            "branch": current_branch,
            "originalBranch": original_branch,
            "originalHead": parent,
            "checkpointCommit": baseline,
            "baselineCommit": baseline,
            "baselineCreated": False,
            "marker": marker,
            "statusFingerprintBefore": before,
            "statusFingerprintAfter": before,
            "includedPaths": [],
            "resumed": True,
        }
    outcome_branch = allocate_outcome_branch(
        repo, branch_name, run, source_branch=current_branch)
    original_ref = git(repo, "rev-parse", current_branch).stdout.strip()
    with _repo_lock(repo):
        git(repo, "switch", "-c", outcome_branch)
        included = _stage_all_visible_changes(repo)
        message = (f"conductor workspace {phase} [{marker}]\n\n"
                   f"Conductor-Original-Branch: {current_branch}\n"
                   f"Conductor-Original-Head: {original_head}")
        result = git(repo, "commit", "--allow-empty", "-m", message, check=False)
        if result.code != 0:
            raise RunError("git commit workspace baseline", result.code,
                           result.stdout, result.stderr)
        checkpoint = head(repo)
    if git(repo, "rev-parse", current_branch).stdout.strip() != original_ref:
        raise ValueError(f"original branch {current_branch!r} moved during workspace preparation")
    return {
        "repoPath": repo,
        "branch": outcome_branch,
        "originalBranch": current_branch,
        "originalHead": original_head,
        "checkpointCommit": checkpoint,
        "baselineCommit": checkpoint,
        "baselineCreated": True,
        "marker": marker,
        "statusFingerprintBefore": before,
        "statusFingerprintAfter": status_fingerprint(repo),
        "includedPaths": included,
    }


def assert_inplace_state(repo: str, *, branch_name: str, expected_head: str,
                         expected_status: str = "") -> dict:
    """Fail closed when the user checkout drifted before an integration step."""
    repo = _require_existing_checkout(repo)
    actual_branch, actual_head = _current_branch(repo), head(repo)
    actual_status = status_fingerprint(repo)
    canonical_expected = canonical_commit(repo, expected_head, field="expected commit")
    if actual_branch != branch_name or actual_head != canonical_expected:
        raise ValueError("inPlace checkout drifted: expected "
                         f"{branch_name}@{canonical_expected[:12]}, "
                         f"got {actual_branch}@{actual_head[:12]}")
    if expected_status and actual_status != expected_status:
        raise ValueError("inPlace checkout has uncommitted changes since its checkpoint")
    return {"repoPath": repo, "branch": actual_branch, "head": actual_head,
            "statusFingerprint": actual_status, "matched": True}


def branch(repo: str, name: str, *, run_id: str = "", force_new: bool = False) -> dict:
    source = _current_branch(repo)
    requested = run_specific_branch(name, run_id)
    if source == requested and not force_new:
        return {"branch": requested, "resumed": True}
    actual = allocate_outcome_branch(repo, name, run_id, source_branch=source)
    git(repo, "switch", "-c", actual)
    return {"branch": actual, "resumed": False}


def _validated_relative_paths(repo: str, paths: list[str] | tuple[str, ...] | None) -> list[str]:
    root = Path(repo).resolve()
    valid: list[str] = []
    for raw in paths or []:
        rel = str(raw or "").strip()
        if not rel or os.path.isabs(rel):
            raise ValueError("force-add paths must be non-empty repository-relative paths")
        target = (root / rel).resolve()
        if target == root or root not in target.parents or ".git" in target.relative_to(root).parts:
            raise ValueError(f"force-add path escapes repository safety boundary: {rel}")
        valid.append(target.relative_to(root).as_posix())
    return sorted(set(valid))


def is_vetted_change_path(path: str) -> bool:
    """Whether a changed path is eligible for an automated source commit.

    This is intentionally path-based and conservative: generated build/cache
    directories are never a repair, while normal source, test, config,
    migration, lockfile, and documentation paths remain eligible.
    """
    normalized = Path(path).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return bool(normalized) and not any(part in _NON_PRODUCT_PATH_PARTS
                                        for part in Path(normalized).parts)


def vetted_changes(repo: str) -> dict[str, list[str]]:
    """Classify the current worktree without staging it."""
    changed = sorted(status_files(repo))
    allowed = [path for path in changed if is_vetted_change_path(path)]
    rejected = [path for path in changed if not is_vetted_change_path(path)]
    if rejected:
        # A `git mv source.py build/source.py` can present as a source deletion
        # plus an untracked generated-path file after we deliberately clear the
        # index.  Never convert that ambiguous mixed state into an automated
        # source deletion.  A human can review/clean it, while ordinary source
        # edits in the same checkout may still be staged.
        deleted = {
            line.strip() for line in git(repo, "diff", "--name-only", "--diff-filter=D", "HEAD",
                                         check=False).stdout.splitlines()
            if line.strip()
        }
        protected = sorted(path for path in allowed if path in deleted)
        if protected:
            allowed = [path for path in allowed if path not in deleted]
            rejected.extend(f"protected deletion: {path}" for path in protected)
    return {"changed": changed, "allowed": allowed, "rejected": rejected}


def within_write_roots(path: str, write_roots) -> bool:
    """Whether ``path`` is one of ``write_roots`` or under one of them (a root is a file or a directory
    prefix). Empty roots confine nothing."""
    given = [os.path.normpath(str(r).strip()).replace(os.sep, "/") for r in (write_roots or []) if str(r).strip()]
    if not given:
        return True
    roots = [r for r in given if r not in ("", "..") and not r.startswith("../")]  # a root escaping the repo confines to nothing
    if not roots:
        return False
    path = os.path.normpath(str(path).strip()).replace(os.sep, "/")
    return any(r == "." or path == r or path.startswith(r + "/") for r in roots)


def stage_vetted_changes(repo: str, *, force_add_paths: list[str] | tuple[str, ...] | None = None,
                         allow_noop: bool = False, write_roots=None, _locked: bool = False) -> dict:
    """Replace automated staging with the vetted changed-path set only.

    A hard reset is never used; ``git reset`` here only clears the index so a
    pre-existing staged cache file cannot hitch a ride on the repair commit.

    ``write_roots``, when given, confines staging to the item's write roots: the agent runner's guard
    refuses Write/Edit outside them, but Bash can write any file of the worktree (sv-cmd-decr removed a
    sibling's declaration through Bash after the Edit tool refused it, run ba2491ff). A change outside
    the roots is left unstaged and reported as ``outsideScope`` — the same refusal the guard records,
    so the Lead widens the item's scope from it instead of integrating an edit nobody owns.
    """
    classified = vetted_changes(repo)
    forced = _validated_relative_paths(repo, force_add_paths)
    forced = [path for path in forced if is_vetted_change_path(path)]
    allowed = sorted(set(classified["allowed"] + forced))
    outside = sorted(path for path in allowed if not within_write_roots(path, write_roots))
    allowed = [path for path in allowed if path not in outside]
    if not allowed:
        rejected_all = classified["rejected"]
        detail = (", ".join(path_sample(rejected_all))
                  + (f" and {len(rejected_all) - PATH_SAMPLE} more" if len(rejected_all) > PATH_SAMPLE else "")
                  ) or "no changed files"
        if outside:
            detail = f"every change is outside the item's write roots: {', '.join(outside)}"
        if allow_noop:
            # A coding task may correctly find nothing to change.  Do not turn
            # that result into a failed workflow, and never stage generated
            # artifacts merely to manufacture a commit.
            with (_nolock() if _locked else _repo_lock(repo)):
                _git_retry(lambda: git(repo, "reset", check=False))
            return {"staged": [], "rejected": classified["rejected"], "outsideScope": outside,
                    "noOp": True, "reason": f"no meaningful source changes: {detail}"}
        raise ValueError(f"no meaningful change to commit; rejected generated/cache-only paths: {detail}")
    with (_nolock() if _locked else _repo_lock(repo)):
        _git_retry(lambda: git(repo, "reset", check=False))
        normal = [path for path in allowed if path not in forced]
        if normal:
            _git_retry(lambda: git(repo, "add", "--", *normal))
        if forced:
            _git_retry(lambda: git(repo, "add", "-f", "--", *forced))
        staged = git(repo, "diff", "--cached", "--name-only", check=False).stdout.splitlines()
    if not staged:
        if allow_noop:
            return {"staged": [], "rejected": classified["rejected"], "outsideScope": outside,
                    "noOp": True, "reason": "no meaningful source changes were staged"}
        raise ValueError("no meaningful change was staged for commit")
    return {"staged": sorted(staged), "rejected": classified["rejected"], "outsideScope": outside, "noOp": False}


@contextmanager
def _nolock():
    yield


def commit(repo: str, message: str = "conductor-code change", *,
           force_add_paths: list[str] | tuple[str, ...] | None = None, write_roots=None,
           atomic: bool = False) -> dict:
    """Stage the vetted changes (within ``write_roots``) and commit. ``atomic`` holds the repo lock across
    staging AND the commit: several agents of one team commit their own files from ONE shared tree, and
    a teammate's ``git reset`` between this agent's staging and its commit would unstage its files."""
    def _run():
        staged = stage_vetted_changes(repo, force_add_paths=force_add_paths, allow_noop=True,
                                      write_roots=write_roots, _locked=atomic)
        if staged["noOp"]:
            return {"commit": head(repo), "stagedPaths": [],
                    "rejectedPaths": path_sample(staged["rejected"]), "rejectedCount": len(staged["rejected"]),
                    "outsideScope": staged.get("outsideScope", []),
                    "noOp": True, "reason": staged["reason"]}
        with (_nolock() if atomic else _repo_lock(repo)):
            result = git(repo, "commit", "-m", message or "conductor-code change", check=False)
            if result.code != 0:
                raise RunError("git commit", result.code, result.stdout, result.stderr)
            sha = head(repo)
        return {"commit": sha, "stagedPaths": staged["staged"],
                "rejectedPaths": path_sample(staged["rejected"]), "rejectedCount": len(staged["rejected"]),
                "outsideScope": staged.get("outsideScope", []), "noOp": False}
    if atomic:
        with _repo_lock(repo):
            return _run()
    return _run()


# The tool cache a sandboxed session may write (common.coding_agent.TOOL_CACHE_DIR, architect.CACHE_ROOT):
# npm and pip caches, Go's build cache, a scout's Playwright browsers. Git-excluded, so a fresh worktree
# starts without one and every session re-downloaded its dependencies (a React implementer spent part
# of its 4.6 minutes on `npm install` and a Chromium download its scout had already made, run 0f829d58).
CACHE_DIR = ".cc-cache"


def exclude_path(repo: str, pattern: str) -> None:
    """Add ``pattern`` to the repository's ``info/exclude`` once (a worktree shares its main
    repository's; a clone has its own), so a seeded cache never shows as an untracked change."""
    rel = git(repo, "rev-parse", "--git-path", "info/exclude", check=False).stdout.strip()
    if not rel:
        return
    path = rel if os.path.isabs(rel) else os.path.join(repo, rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = open(path).read() if os.path.exists(path) else ""
        if pattern in text.split("\n"):
            return
        with open(path, "a") as fh:
            fh.write(("" if not text or text.endswith("\n") else "\n") + pattern + "\n")
    except OSError:
        return


# A task output is a message, not a dump: a path list read off the filesystem goes into it as its
# count plus this many examples. A failed scaffold carried its 15,000 rejected node_modules paths
# (1 MB) in every attempt's output, joins copied them, and the team workflow passed Conductor's 10 MB
# payload limit and died (run a87e9ddb). The full list is the tree's own `git status`.
PATH_SAMPLE = 20


def path_sample(paths) -> list[str]:
    return list(paths or [])[:PATH_SAMPLE]


def exclude_generated(repo: str) -> None:
    """Keep generated directories (dependency installs, build output, caches) out of git's view in
    ``repo`` and every worktree that shares its ``info/exclude``, whatever the repository's own ignore
    files say. ``is_vetted_change_path`` already never commits them; excluding them at the source keeps
    `git status` the size of the work. A frontend scaffold with an empty .gitignore and a team's
    `npm install` put 15,000 untracked node_modules files in a shared tree, and its `git status` grew to
    2.8 MB (run a87e9ddb). Tracked files under such a directory stay tracked: exclude rules apply to
    untracked paths only."""
    for part in sorted(_NON_PRODUCT_PATH_PARTS):
        exclude_path(repo, f"{part}/")


def seed_cache(src: str, dst: str) -> int:
    """Warm the tool cache at ``dst`` from the one at ``src``: every file not yet there is hard-linked
    (the caches are content-addressed and immutable) or copied where linking is refused. Returns the
    number of files seeded; nothing to seed is 0."""
    if not src or not os.path.isdir(src) or os.path.realpath(src) == os.path.realpath(dst):
        return 0
    if os.path.basename(dst.rstrip(os.sep)) == CACHE_DIR:
        exclude_path(os.path.dirname(dst.rstrip(os.sep)), f"/{CACHE_DIR}/")
    seeded = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for d in dirs:
            s = os.path.join(root, d)
            if os.path.islink(s) and not os.path.lexists(os.path.join(target, d)):
                os.symlink(os.readlink(s), os.path.join(target, d))
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for f in files:
            s, t = os.path.join(root, f), os.path.join(target, f)
            if os.path.lexists(t):
                continue
            try:
                if os.path.islink(s):
                    os.symlink(os.readlink(s), t)
                else:
                    os.link(s, t)
            except OSError:
                try:
                    shutil.copy2(s, t, follow_symlinks=False)
                except OSError:
                    continue
            seeded += 1
    return seeded


def worktree_shared(repo: str) -> dict:
    """ONE working tree per team: the item builds in the team's shared checkout itself (no fork), so
    every agent sees every teammate's files as they land and verifies against the real tree. Three
    implementers in separate forks each finished "verification could not run" because their fork lacked
    the others' files (run b93cd3e2)."""
    return {"worktreePath": repo, "branch": _trim(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout),
            "initialCommit": head(repo), "resumed": False, "shared": True}


def worktree_add(repo: str, name: str, *, preserve_existing: bool = False) -> dict:
    exclude_generated(repo)
    return _worktree_add(repo, name, preserve_existing=preserve_existing)


def _worktree_add(repo: str, name: str, *, preserve_existing: bool = False) -> dict:
    wt = os.path.join(repo, WORKTREES, name)
    br = group_branch(repo, name)
    if preserve_existing and os.path.isdir(wt):
        inside = git(wt, "rev-parse", "--is-inside-work-tree", check=False)
        if inside.code == 0 and _trim(inside.stdout) == "true":
            return {"worktreePath": wt,
                    "branch": _trim(git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout),
                    "initialCommit": _trim(git(wt, "rev-parse", "HEAD").stdout),
                    "resumed": True}
    # Serialize the whole create section across the parallel forks (they all
    # mutate this one repo's .git); retry the load-bearing add as extra defense.
    with _repo_lock(repo):
        # Prune dead refs + remove any stale worktree/branch so re-runs never block.
        git(repo, "worktree", "prune", check=False)
        git(repo, "worktree", "remove", "--force", wt, check=False)
        git(repo, "branch", "-D", br, check=False)
        _git_retry(lambda: git(repo, "worktree", "add", "-B", br, wt))
    # Copy test/ + package.json into the worktree so test runs find them
    # (worktrees only contain branch-tracked files; tests live in the main repo).
    for rel in ("test", "package.json"):
        src = os.path.join(repo, rel)
        if os.path.exists(src):
            dst = os.path.join(wt, rel)
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            except OSError:
                pass
    # A fork starts with the shared worktree's tool cache: no second `npm install` download, no second
    # Chromium, for every implementer, author, reviewer and scout.
    seed_cache(os.path.join(repo, CACHE_DIR), os.path.join(wt, CACHE_DIR))
    initial = _trim(git(wt, "rev-parse", "HEAD").stdout)
    return {"worktreePath": wt, "branch": br, "initialCommit": initial, "resumed": False}


def worktree_add_detached(repo: str, name: str, commit: str) -> dict:
    """A worktree pinned at ``commit`` with no branch: the tree a read-only session (a peer reviewer)
    looks at. A session must never run in the shared integration worktree: the agent runner resets the
    worktree it was given to the HEAD it started from, so a sibling team's merge landing meanwhile was
    undone and its files left untracked, aborting that team's next fast-forward (run a062184f)."""
    wt = os.path.join(repo, WORKTREES, name)
    with _repo_lock(repo):
        git(repo, "worktree", "prune", check=False)
        git(repo, "worktree", "remove", "--force", wt, check=False)
        _git_retry(lambda: git(repo, "worktree", "add", "--detach", wt, commit))
    return {"worktreePath": wt, "commit": _trim(git(wt, "rev-parse", "HEAD").stdout)}


def exclude_worktrees(repo: str) -> str:
    """Ignore harness worktrees without modifying the repository's tracked .gitignore."""
    common = os.path.abspath(_common_gitdir(repo))
    path = os.path.join(common, "info", "exclude")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    marker = ".cc-worktrees/"
    current = ""
    try:
        current = Path(path).read_text(encoding="utf-8")
    except OSError:
        pass
    if marker not in {line.strip() for line in current.splitlines()}:
        with open(path, "a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(marker + "\n")
    return path


def remote_urls(repo: str) -> dict[str, str]:
    names = [line.strip() for line in git(repo, "remote", check=False).stdout.splitlines()
             if line.strip()]
    return {
        name: git(repo, "remote", "get-url", name, check=False).stdout.strip()
        for name in names
    }


def fetch_source(repo: str, source: str, refspec: str) -> dict:
    """Fetch a ref directly from a URL/slug/remote without changing source repo config."""
    with _repo_lock(repo):
        _git_retry(lambda: git(repo, "fetch", source, refspec))
    return {"source": source, "refspec": refspec}


def workspace_add(repo: str, workflow_id: str, *, branch_name: str | None = None,
                  start_point: str = "HEAD", preserve_existing: bool = True) -> dict:
    """Create one persistent run-level worktree below the supplied source checkout."""
    name = f"run-{_safe_name(workflow_id)}"
    wt = os.path.join(os.path.abspath(repo), WORKTREES, name)
    if preserve_existing and os.path.isdir(wt):
        inside = git(wt, "rev-parse", "--is-inside-work-tree", check=False)
        if inside.code == 0 and _trim(inside.stdout) == "true":
            return {
                "worktreePath": wt,
                "branch": _current_branch(wt),
                "initialCommit": head(wt),
                "resumed": True,
            }
    with _repo_lock(repo):
        git(repo, "worktree", "prune", check=False)
        git(repo, "worktree", "remove", "--force", wt, check=False)
        br = allocate_outcome_branch(
            repo, branch_name or "conductor/run", workflow_id,
            source_branch=_current_branch(repo))
        _git_retry(lambda: git(repo, "worktree", "add", "-b", br, wt, start_point))
    return {
        "worktreePath": wt,
        "branch": br,
        "initialCommit": head(wt),
        "resumed": False,
    }


def worktree_remove_path(repo: str, worktree_path: str, *,
                         remove_nested: bool = True) -> dict:
    """Remove an owned worktree, deepest nested worktrees first, preserving branches."""
    target = os.path.abspath(worktree_path)
    listing = git(repo, "worktree", "list", "--porcelain", check=False).stdout
    paths = [line.split(" ", 1)[1].strip() for line in listing.splitlines()
             if line.startswith("worktree ")]
    selected = []
    for path in paths:
        absolute = os.path.abspath(path)
        if absolute == target or (remove_nested and absolute.startswith(target + os.sep)):
            selected.append(absolute)
    for path in sorted(selected, key=len, reverse=True):
        git(repo, "worktree", "remove", "--force", path, check=False)
    git(repo, "worktree", "prune", check=False)
    return {"removed": selected, "worktreePath": target}


def worktree_remove(repo: str, name: str) -> dict:
    wt = os.path.join(repo, WORKTREES, name)
    git(repo, "worktree", "remove", "--force", wt, check=False)
    git(repo, "worktree", "prune", check=False)
    return {"removed": name}


def run_worktrees(repo: str) -> dict[str, str]:
    """The run-level worktrees registered on ``repo`` (``.cc-worktrees/run-<id>``), by run id."""
    base = os.path.join(os.path.abspath(repo), WORKTREES)
    listing = git(repo, "worktree", "list", "--porcelain", check=False).stdout
    out: dict[str, str] = {}
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = os.path.abspath(line.split(" ", 1)[1].strip())
        parent, name = os.path.split(path)
        if parent == base and name.startswith("run-"):
            out[name[len("run-"):]] = path
    return out


def prune_finished_runs(repo: str, *, keep_run_id: str, prunable) -> dict:
    """Remove the run worktrees (with their nested fork and review worktrees) of runs that are over for
    good. ``prunable(run_id)`` answers True for a run that completed, False for one still running or
    one that failed or was stopped (its retry resumes in this checkout), or None when it cannot tell —
    an unknown run is kept. Only the checkout goes: the run's branch, and everything it delivered, stays.

    Registered worktrees are not free. Claude Code's Bash sandbox adds filesystem deny paths for every
    worktree registered on the repository, and at 171 such paths the sandbox profile exceeded the OS
    exec argument limit: every Bash command of every implementer failed at spawn (E2BIG) — 591 failed
    calls in one run — and no agent could build or test (run ec0fb6dc; 51 of the 70 registered
    worktrees belonged to runs that had finished hours earlier)."""
    removed, kept = [], []
    keep = _safe_name(keep_run_id) if keep_run_id else ""
    for run_id, path in sorted(run_worktrees(repo).items()):
        if run_id == keep:
            continue
        if prunable(run_id) is True:
            worktree_remove_path(repo, path, remove_nested=True)
            removed.append(run_id)
        else:
            kept.append(run_id)
    return {"removed": removed, "kept": kept}


def tracked_at_head(repo: str, paths) -> list[str]:
    """The given repository paths that exist in HEAD's tree: committed before the current session."""
    return [str(p) for p in paths
            if git(repo, "cat-file", "-e", f"HEAD:{p}", check=False).code == 0]


def status_files(repo: str) -> set[str]:
    """Set of paths with uncommitted changes (porcelain), every untracked file listed by itself. Used
    to report filesChanged after an agent edits a worktree and to audit a delivery: with git's default
    collapsing, the first files of a NEW directory showed up as one entry ``resp/`` and the audit
    reported every planned file of the item missing (three no-op repair agents per such fork, run
    ec0fb6dc)."""
    return set(_status_porcelain(repo, untracked_files_all=True))


def _status_porcelain(repo: str, *, untracked_files_all: bool = False) -> dict[str, str]:
    """Return exact paths and XY codes from porcelain v1's NUL format.

    Newline-delimited porcelain is ambiguous for quoted names, embedded newlines,
    and renames.  With ``-z`` paths are literal and a rename/copy's destination is
    the first path followed by a second, source-path field.
    """
    args = ["status", "--porcelain=v1", "-z"]
    if untracked_files_all:
        args.append("--untracked-files=all")
    fields = git(repo, *args, check=False).stdout.split("\0")
    changes: dict[str, str] = {}
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            continue
        code, path = record[:2], record[3:]
        changes[path] = code
        if "R" in code or "C" in code:
            # Consume the source path.  Scope/reporting follows the destination,
            # which is the path whose resulting contents an agent controls.
            index += 1
    return changes


def status_changes(repo: str, *, untracked_files_all: bool = False) -> dict[str, str]:
    """Uncommitted changes with a normalized one-letter status per path:
    A = created (untracked/added), M = updated, D = deleted, R = renamed.
    Complements status_files (which strips the codes)."""
    changes: dict[str, str] = {}
    for path, code in _status_porcelain(
            repo, untracked_files_all=untracked_files_all).items():
        if code == "??" or "A" in code:
            status = "A"
        elif "D" in code:
            status = "D"
        elif "R" in code:
            status = "R"
        else:
            status = "M"
        changes[path] = status
    return changes


def index_snapshot(repo: str) -> tuple[str, bytes | None]:
    """Capture the worktree's exact index so an agent cannot leave staged edits."""
    path = _trim(git(repo, "rev-parse", "--git-path", "index").stdout)
    if not os.path.isabs(path):
        path = os.path.join(repo, path)
    path = os.path.abspath(path)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        data = None
    return path, data


def restore_index(snapshot: tuple[str, bytes | None]) -> None:
    """Restore a snapshot made by :func:`index_snapshot` atomically."""
    path, data = snapshot
    if data is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.harness-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def local_diff_against_remote(repo: str, *, remote: str = "origin",
                              branch: str = "main", max_chars: int = 200_000) -> dict:
    """Return the checkout's complete working-tree diff against a fresh remote branch.

    This deliberately does *not* check out, reset, stage, commit, or push anything.
    Fetching refreshes only the remote-tracking ref so review sees the actual remote
    baseline.  ``git diff <base>`` includes both local commits and tracked staged or
    unstaged edits; untracked files are appended with ``--no-index`` so a pre-commit
    review does not silently miss new files.
    """
    path = str(Path(repo).expanduser().resolve())
    inside = git(path, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.code != 0 or _trim(inside.stdout) != "true":
        raise ValueError(f"repoPath is not a git worktree: {path}")

    remote = str(remote or "origin").strip()
    branch = str(branch or "main").strip()
    if not remote or any(c.isspace() for c in remote) or remote.startswith("-"):
        raise ValueError("baseRemote must name a configured git remote")
    if not branch or branch.startswith("-") or \
            git(path, "check-ref-format", "--branch", branch, check=False).code != 0:
        raise ValueError("baseBranch must be a valid git branch name")
    remotes = {line.strip() for line in git(path, "remote", check=False).stdout.splitlines()}
    if remote not in remotes:
        raise ValueError(f"baseRemote {remote!r} is not configured; found {sorted(remotes)}")

    base_ref = f"refs/remotes/{remote}/{branch}"
    # The shared ref store can be touched by other harness runs, so use the existing
    # repo lock. This is metadata-only and leaves the caller's checkout untouched.
    with _repo_lock(path):
        _git_retry(lambda: git(path, "fetch", "--quiet", remote,
                               f"+refs/heads/{branch}:{base_ref}"))
    base_commit = _trim(git(path, "rev-parse", "--verify", base_ref).stdout)
    head_commit = _trim(git(path, "rev-parse", "HEAD").stdout)

    tracked = git(path, "diff", "--binary", "--find-renames", base_ref, check=False).stdout
    names = [line for line in git(path, "diff", "--name-only", "-z", base_ref,
                                  check=False).stdout.split("\0") if line]
    untracked = [line for line in git(path, "ls-files", "--others", "--exclude-standard", "-z",
                                      check=False).stdout.split("\0") if line]
    chunks = [tracked]
    for rel in untracked:
        # --no-index exits 1 when a difference is found; that is the expected result.
        added = git(path, "diff", "--binary", "--no-index", "--", "/dev/null", rel,
                    check=False)
        if added.stdout:
            chunks.append(added.stdout)
    changed_files = list(dict.fromkeys([*names, *untracked]))
    full_diff = "".join(chunks)
    limit = max(int(max_chars), 1)
    return {
        "repoPath": path,
        "baseRemote": remote,
        "baseBranch": branch,
        "baseRef": f"{remote}/{branch}",
        "baseCommit": base_commit,
        "headCommit": head_commit,
        "changedFiles": changed_files,
        "untrackedFiles": untracked,
        "hasChanges": bool(changed_files),
        "diff": full_diff[:limit],
        "diffChars": len(full_diff),
        "truncated": len(full_diff) > limit,
    }


def reset_hard(repo: str, commit: str) -> None:
    git(repo, "reset", "--hard", commit, check=False)


def restore_path(wt: str, path: str) -> None:
    """Undo a change to a single path: revert a tracked file to HEAD, or remove
    an untracked new file. Used to enforce file-scope/protected guardrails."""
    tracked = git(wt, "ls-files", "--error-unmatch", path, check=False).code == 0
    if tracked:
        git(wt, "checkout", "--", path, check=False)
    else:
        try:
            target = os.path.join(wt, path)
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
        except OSError:
            pass


def head(repo: str) -> str:
    return _trim(git(repo, "rev-parse", "HEAD").stdout)


def canonical_commit(repo: str, revision: object, *, field: str = "commit") -> str:
    """Resolve an abbreviated or full object ID to one unambiguous commit ID."""
    value = str(revision or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        raise ValueError(f"invalid {field}: {value!r}")
    resolved = git(repo, "rev-parse", "--verify", f"{value}^{{commit}}", check=False)
    if resolved.code != 0:
        raise ValueError(f"{field} does not resolve uniquely: {value}")
    return _trim(resolved.stdout)


def has_conflicts(repo: str) -> list[str]:
    out = git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout
    return [f for f in out.split("\n") if f.strip()]


def _current_branch(repo: str) -> str:
    return _trim(git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout)


def clone(repo_url: str, dest: str | None = None, *, branch: str | None = None,
          depth: int | None = None, no_local: bool = False,
          env: dict[str, str] | None = None, clean_env: bool = False) -> dict:
    """Clone a remote repo. ``dest`` defaults to the repo name under cwd; a shallow
    clone when ``depth`` is set; a specific ``branch`` when given. Assumes git auth is
    already configured (the worker runs github.ensure_git_auth first)."""
    args = ["clone"]
    if no_local:
        args.append("--no-local")
    if branch:
        args += ["--branch", branch]
    if depth:
        args += ["--depth", str(depth)]
    args += [repo_url]
    if dest:
        args += [dest]
    # `git clone` is run from a neutral cwd; -C would need an existing dir.
    run_res = run(["git", *args], check=True, env=env, clean_env=clean_env)  # noqa: F841 — raises on failure
    if not dest:
        # Derive the directory git created (repo name minus .git).
        tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
        dest = tail[:-4] if tail.endswith(".git") else tail
    return {"repoPath": dest, "branch": _current_branch(dest), "head": head(dest)}


def fetch(repo: str, *, remote: str = "origin", refspec: str | None = None,
          prune: bool = False) -> dict:
    """Fetch from a remote (optionally a refspec, e.g. a PR ref; optionally --prune)."""
    args = ["fetch", remote]
    if prune:
        args.insert(1, "--prune")
    if refspec:
        args.append(refspec)
    with _repo_lock(repo):
        _git_retry(lambda: git(repo, *args))
    return {"fetched": True, "remote": remote, "refspec": refspec or "", "head": head(repo)}


def pull(repo: str, *, remote: str = "origin", branch_name: str | None = None,
         rebase: bool = True) -> dict:
    """Fetch + integrate the remote branch into the current one. Fail-soft on
    conflict: aborts the merge/rebase and returns the conflicted paths rather than
    leaving the tree wedged."""
    br = branch_name or _current_branch(repo)
    args = ["pull", "--rebase" if rebase else "--no-rebase", remote, br]
    with _repo_lock(repo):
        r = _git_retry(lambda: git(repo, *args, check=False))
    conflicts = has_conflicts(repo)
    if conflicts:
        # Abort so the worktree is left clean; caller decides what to do next.
        git(repo, "rebase" if rebase else "merge", "--abort", check=False)
        return {"pulled": False, "remote": remote, "branch": br,
                "conflicts": conflicts, "head": head(repo)}
    if r.code != 0:
        raise RunError(f"git pull {remote} {br}", r.code, r.stdout, r.stderr)
    return {"pulled": True, "remote": remote, "branch": br, "conflicts": [],
            "head": head(repo)}


def push(repo: str, *, branch_name: str | None = None, remote: str = "origin",
         destination_branch: str | None = None, set_upstream: bool = True,
         force_with_lease: bool = False, expected_head: str | None = None) -> dict:
    """Push a branch to a remote. Uses --force-with-lease ONLY when asked (never a
    bare --force); sets upstream tracking by default.

    When ``expected_head`` is supplied, the push is candidate-bound: the named
    local branch must still resolve to that commit and the exact commit SHA is
    used as the source side of the refspec.  This prevents a later local commit
    from being published under an earlier verification result.
    """
    br = branch_name or _current_branch(repo)
    destination = destination_branch or br
    source = br
    expected = str(expected_head or "").strip()
    if expected:
        source = canonical_commit(repo, expected, field="expected candidate commit")
    args = ["push"]
    if set_upstream and "://" not in remote and not remote.startswith("git@"):
        args.append("--set-upstream")
    if force_with_lease:
        args.append("--force-with-lease")
    # Git cannot infer a remote destination from a raw SHA source. Use an
    # explicit heads ref for the candidate-bound form; ordinary branch pushes
    # retain their existing upstream-friendly short refspec.
    destination_ref = destination if destination.startswith("refs/") else f"refs/heads/{destination}"
    refspec = (f"{source}:{destination_ref}" if expected
               else (f"{source}:{destination}" if destination != br else br))
    args += [remote, refspec]
    with _repo_lock(repo):
        if expected:
            actual = _trim(git(repo, "rev-parse", "--verify", f"{br}^{{commit}}", check=False).stdout)
            if actual != source:
                raise ValueError("local branch moved after candidate verification: "
                                 f"expected {source[:12]}, got {actual[:12]}")
        _git_retry(lambda: git(repo, *args))
        # Git accepts --set-upstream with a raw-SHA refspec but does not
        # actually record tracking for the named local branch.  Keep the
        # candidate-bound source refspec and configure tracking explicitly.
        if expected and set_upstream and "://" not in remote and not remote.startswith("git@"):
            tracking_destination = destination.removeprefix("refs/heads/")
            git(repo, "branch", "--set-upstream-to", f"{remote}/{tracking_destination}", br)
    return {"pushed": True, "branch": br, "destinationBranch": destination,
            "remote": remote, "head": source if expected else head(repo),
            "candidateBound": bool(expected)}


def remote_set(repo: str, url: str, *, name: str = "origin") -> dict:
    """Idempotently point a remote at ``url``: set-url if it exists, else add it."""
    exists = git(repo, "remote", "get-url", name, check=False).code == 0
    if exists:
        git(repo, "remote", "set-url", name, url)
    else:
        git(repo, "remote", "add", name, url)
    return {"remote": name, "url": url, "existed": exists}


# ─────────────────────────────── after a run: continue, summarize, ship ───────────────────────────────
def branch_slug(text: str, *, prefix: str = "swarm", words: int = 5) -> str:
    """A readable outcome-branch prefix from what was asked: ``swarm/postgres-compatible-database``.
    The run id is appended by ``run_specific_branch``, so two runs never share a branch."""
    tokens = re.findall(r"[a-z0-9]+", str(text or "").lower())
    stop = {"a", "an", "the", "and", "or", "of", "to", "in", "for", "with", "on", "create", "build", "make",
            "add", "write", "written", "implement", "please", "that", "this", "it", "is", "be"}
    picked = [t for t in tokens if t not in stop][:words] or ["run"]
    return f"{prefix}/{'-'.join(picked)[:48].strip('-')}"


def _worktree_branches(repo: str) -> dict[str, str]:
    """``{branch: worktree path}`` for every worktree registered on the repository."""
    out, path = {}, None
    for line in git(repo, "worktree", "list", "--porcelain", check=False).stdout.splitlines():
        if line.startswith("worktree "):
            path = os.path.abspath(line.split(" ", 1)[1].strip())
        elif line.startswith("branch ") and path:
            out[line.split(" ", 1)[1].strip().removeprefix("refs/heads/")] = path
    return out


def continuation_checkout(repo: str, branch: str) -> str:
    """A checkout of ``branch`` that a follow-up run can use as its repoPath, so it plans against and
    builds on that branch's work. The checkout already holding the branch is reused (the main checkout,
    or an earlier continuation); a finished run's own worktree that still holds it is removed first
    (its branch keeps the work, and a branch can be checked out in one place only). Otherwise a new
    worktree ``.cc-worktrees/continue-<branch>`` is created next to the run worktrees. The user's own
    checkout is never switched."""
    root = os.path.abspath(_trim(git(repo, "rev-parse", "--show-toplevel").stdout) or repo)
    common = os.path.abspath(os.path.join(root, _trim(git(root, "rev-parse", "--git-common-dir").stdout)))
    main = os.path.dirname(common) if os.path.basename(common) == ".git" else root
    if not _branch_exists(main, branch):
        raise ValueError(f"branch {branch} does not exist in {main}")
    holders = _worktree_branches(main)
    held = holders.get(branch)
    runs = os.path.join(main, WORKTREES)
    if held and os.path.dirname(held) == runs and os.path.basename(held).startswith("run-"):
        if git(held, "status", "--porcelain", check=False).stdout.strip():
            raise ValueError(f"{held} holds {branch} with uncommitted changes; commit or discard them first")
        worktree_remove_path(main, held, remove_nested=True)
        held = None
    if held:
        return held
    path = os.path.join(runs, "continue-" + _safe_name(branch.replace("/", "-")))
    if os.path.isdir(path):
        worktree_remove_path(main, path, remove_nested=True)
    os.makedirs(runs, exist_ok=True)
    exclude_worktrees(main)
    git(main, "worktree", "add", path, branch)
    return path


def branch_summary(repo: str, branch: str, base: str = "") -> dict:
    """What ``branch`` adds over ``base`` (the branch the run started from): commits, files, lines."""
    if not branch or not _branch_exists(repo, branch):
        return {"branch": branch, "exists": False}
    head = _trim(git(repo, "rev-parse", branch).stdout)
    out = {"branch": branch, "exists": True, "head": head, "base": base}
    if base and git(repo, "rev-parse", "--verify", base, check=False).code == 0:
        out["commits"] = int(_trim(git(repo, "rev-list", "--count", f"{base}..{branch}").stdout) or 0)
        stat = _trim(git(repo, "diff", "--shortstat", f"{base}...{branch}", check=False).stdout)
        nums = {k: int(v) for v, k in re.findall(r"(\d+) (file|insertion|deletion)", stat)}
        out.update({"files": nums.get("file", 0), "insertions": nums.get("insertion", 0),
                    "deletions": nums.get("deletion", 0),
                    "merged": git(repo, "merge-base", "--is-ancestor", branch, base, check=False).code == 0})
    return out


def remote_info(repo: str, remote: str = "origin") -> dict | None:
    """The remote a branch can be shipped to: its URL and default branch (from the local tracking
    ref; no network). None when the repository has no such remote."""
    url = remote_urls(repo).get(remote)
    if not url:
        return None
    head = _trim(git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD", check=False).stdout)
    default = head.split("/", 1)[1] if "/" in head else ""
    if not default:
        default = next((b for b in ("main", "master") if git(repo, "rev-parse", "--verify",
                                                               f"refs/remotes/{remote}/{b}", check=False).code == 0), "")
    github = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return {"name": remote, "url": url, "defaultBranch": default, "github": github.group(1) if github else ""}


def merge_branch(repo: str, branch: str, into: str) -> dict:
    """Merge a run's branch into ``into`` locally, only when it is safe: where ``into`` is checked out
    the checkout must be clean (fast-forward when possible, else a merge commit; a conflict is aborted
    and reported, never left behind); where it is not checked out it can only be fast-forwarded."""
    if not _branch_exists(repo, branch) or not _branch_exists(repo, into):
        raise ValueError(f"both {branch} and {into} must be local branches")
    holder = _worktree_branches(repo).get(into)
    if git(repo, "merge-base", "--is-ancestor", branch, into, check=False).code == 0:
        return {"merged": True, "already": True, "into": into, "head": _trim(git(repo, "rev-parse", into).stdout)}
    ff = git(repo, "merge-base", "--is-ancestor", into, branch, check=False).code == 0
    if not holder:
        if not ff:
            raise ValueError(f"{into} has moved since the run started; check it out and merge {branch} there")
        with _repo_lock(repo):
            git(repo, "update-ref", f"refs/heads/{into}", _trim(git(repo, "rev-parse", branch).stdout))
        return {"merged": True, "fastForward": True, "into": into, "head": _trim(git(repo, "rev-parse", into).stdout)}
    if git(holder, "status", "--porcelain", "--untracked-files=no", check=False).stdout.strip():
        raise ValueError(f"{holder} ({into}) has uncommitted changes; commit or stash them first")
    with _repo_lock(repo):
        res = git(holder, "merge", "--ff" if ff else "--no-ff", "--no-edit", branch, check=False)
        if res.code != 0:
            git(holder, "merge", "--abort", check=False)
            raise ValueError(f"merging {branch} into {into} conflicts; nothing was changed: "
                             f"{(res.stdout + res.stderr).strip()[:400]}")
    return {"merged": True, "fastForward": ff, "into": into, "checkout": holder,
            "head": _trim(git(holder, "rev-parse", "HEAD").stdout)}
