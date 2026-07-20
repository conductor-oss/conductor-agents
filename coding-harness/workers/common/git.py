"""Git operations on the target repo. Each is exposed as its own discrete worker
task (gitops/tasks.py) for visibility and distribution-readiness. Worktrees live
on a shared filesystem now; ``push``/``pull`` are stubs with real signatures so
moving to multi-host later is a worker-body change only.

Ported from ``git_ops.ts`` + ``integrate.ts``.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import time
from contextlib import contextmanager

from .exec import RunError, run

WORKTREES = ".cc-worktrees"
GROUP_BRANCH = "cc-group-{name}"

# Git identity used for the local commit when a repo has none configured. Sourced
# from env so deployments can override without code changes (defaults preserved).
GIT_IDENTITY_NAME = os.environ.get("GIT_IDENTITY_NAME") or "conductor-code"
GIT_IDENTITY_EMAIL = os.environ.get("GIT_IDENTITY_EMAIL") or "harness@conductor.local"


def _env_int(key: str, default: int) -> int:
    """Env int with fall-back: a missing/blank/invalid value keeps the default."""
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    """Env float with fall-back: a missing/blank/invalid value keeps the default."""
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Transient-git-lock retry knobs (see _git_retry) — env-configurable, validated.
GIT_LOCK_RETRY_ATTEMPTS = _env_int("GIT_LOCK_RETRY_ATTEMPTS", 5)
GIT_LOCK_RETRY_BASE = _env_float("GIT_LOCK_RETRY_BASE", 0.3)

# Paths copied from the main repo into a fresh worktree so test runs find them
# (worktrees only contain branch-tracked files). Comma-separated env override.
WORKTREE_COPY_PATHS = [
    p.strip() for p in (os.environ.get("WORKTREE_COPY_PATHS") or "test,package.json").split(",")
    if p.strip()
]

# git emits these when two processes touch the same repo/refs at once. The
# parallel code_parallel forks all mutate one repo, so worktree_add/commit can
# collide; we serialize (flock on the shared git dir) and retry these.
_GIT_LOCK_HINTS = ("index.lock", "cannot lock ref", "Unable to create",
                   "another git process", "shallow.lock", "packed-refs.lock")


def git(repo: str, *args: str, check: bool = True):
    return run(["git", "-C", repo, *args], check=check)


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


def _git_retry(fn, attempts: int = GIT_LOCK_RETRY_ATTEMPTS, base: float = GIT_LOCK_RETRY_BASE):
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


def ensure_ready(repo: str, *, name: str = GIT_IDENTITY_NAME,
                 email: str = GIT_IDENTITY_EMAIL) -> dict:
    """Make ``repo`` git-ready so worktree_add/commit won't fail: init if needed,
    set a LOCAL identity only if none is configured, and create an initial commit
    if there is no HEAD. Idempotent — safe to run on an already-prepared repo."""
    os.makedirs(repo, exist_ok=True)
    inside = git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    initialized = False
    if inside.code != 0 or _trim(inside.stdout) != "true":
        git(repo, "init")
        initialized = True
    if not _trim(git(repo, "config", "user.email", check=False).stdout):
        git(repo, "config", "user.email", email)
    if not _trim(git(repo, "config", "user.name", check=False).stdout):
        git(repo, "config", "user.name", name)
    committed = False
    if git(repo, "rev-parse", "--verify", "HEAD", check=False).code != 0:
        git(repo, "add", "-A", check=False)
        r = git(repo, "commit", "-m", "conductor-code: initial commit", check=False)
        if r.code != 0:  # nothing to commit yet — make an empty root commit
            git(repo, "commit", "--allow-empty", "-m", "conductor-code: initial commit", check=False)
        committed = True
    branch = _trim(git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout)
    return {"repoPath": repo, "initialized": initialized,
            "initialCommitCreated": committed, "branch": branch}


def branch(repo: str, name: str) -> dict:
    git(repo, "checkout", "-B", name)
    return {"branch": name}


def commit(repo: str, message: str = "conductor-code change") -> dict:
    # Serialized on the shared git dir: parallel forks committing to sibling
    # worktrees write shared refs/reflog and can otherwise collide.
    with _repo_lock(repo):
        _git_retry(lambda: git(repo, "add", "-A"))
        git(repo, "commit", "-m", message or "conductor-code change", check=False)  # no-op if nothing staged
        sha = _trim(git(repo, "rev-parse", "--short", "HEAD").stdout)
    return {"commit": sha}


def worktree_add(repo: str, name: str) -> dict:
    wt = os.path.join(repo, WORKTREES, name)
    br = GROUP_BRANCH.format(name=name)
    # Serialize the whole create section across the parallel forks (they all
    # mutate this one repo's .git); retry the load-bearing add as extra defense.
    with _repo_lock(repo):
        # Prune dead refs + remove any stale worktree/branch so re-runs never block.
        git(repo, "worktree", "prune", check=False)
        git(repo, "worktree", "remove", "--force", wt, check=False)
        git(repo, "branch", "-D", br, check=False)
        _git_retry(lambda: git(repo, "worktree", "add", "-B", br, wt))
    # Copy configured paths (default test/ + package.json) into the worktree so
    # test runs find them (worktrees only contain branch-tracked files).
    for rel in WORKTREE_COPY_PATHS:
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
    initial = _trim(git(wt, "rev-parse", "HEAD").stdout)
    return {"worktreePath": wt, "branch": br, "initialCommit": initial}


def worktree_remove(repo: str, name: str) -> dict:
    wt = os.path.join(repo, WORKTREES, name)
    git(repo, "worktree", "remove", "--force", wt, check=False)
    git(repo, "worktree", "prune", check=False)
    return {"removed": name}


def status_files(repo: str) -> set[str]:
    """Set of paths with uncommitted changes (porcelain). Used to report
    filesChanged after an agent edits a worktree."""
    out = git(repo, "status", "--porcelain", check=False).stdout
    return {line[3:].strip() for line in out.split("\n") if line.strip()}


def status_changes(repo: str) -> dict[str, str]:
    """Uncommitted changes with a normalized one-letter status per path:
    A = created (untracked/added), M = updated, D = deleted, R = renamed.
    Complements status_files (which strips the codes)."""
    out = git(repo, "status", "--porcelain", check=False).stdout
    changes: dict[str, str] = {}
    for line in out.split("\n"):
        if not line.strip():
            continue
        code, path = line[:2], line[3:].strip()
        if code == "??" or "A" in code:
            status = "A"
        elif "D" in code:
            status = "D"
        elif "R" in code:
            status = "R"
            # porcelain rename lines are "old -> new"; report the new path
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip()
        else:
            status = "M"
        changes[path] = status
    return changes


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
            os.remove(os.path.join(wt, path))
        except OSError:
            pass


def head(repo: str) -> str:
    return _trim(git(repo, "rev-parse", "HEAD").stdout)


def has_conflicts(repo: str) -> list[str]:
    out = git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout
    return [f for f in out.split("\n") if f.strip()]


def _current_branch(repo: str) -> str:
    return _trim(git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout)


def clone(repo_url: str, dest: str | None = None, *, branch: str | None = None,
          depth: int | None = None) -> dict:
    """Clone a remote repo. ``dest`` defaults to the repo name under cwd; a shallow
    clone when ``depth`` is set; a specific ``branch`` when given. Assumes git auth is
    already configured (the worker runs github.ensure_git_auth first)."""
    args = ["clone"]
    if branch:
        args += ["--branch", branch]
    if depth:
        args += ["--depth", str(depth)]
    args += [repo_url]
    if dest:
        args += [dest]
    # `git clone` is run from a neutral cwd; -C would need an existing dir.
    run_res = run(["git", *args], check=True)  # noqa: F841 — raises on failure
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
         set_upstream: bool = True, force_with_lease: bool = False) -> dict:
    """Push a branch to a remote. Uses --force-with-lease ONLY when asked (never a
    bare --force); sets upstream tracking by default."""
    br = branch_name or _current_branch(repo)
    args = ["push"]
    if set_upstream:
        args.append("--set-upstream")
    if force_with_lease:
        args.append("--force-with-lease")
    args += [remote, br]
    with _repo_lock(repo):
        _git_retry(lambda: git(repo, *args))
    return {"pushed": True, "branch": br, "remote": remote, "head": head(repo)}


def remote_set(repo: str, url: str, *, name: str = "origin") -> dict:
    """Idempotently point a remote at ``url``: set-url if it exists, else add it."""
    exists = git(repo, "remote", "get-url", name, check=False).code == 0
    if exists:
        git(repo, "remote", "set-url", name, url)
    else:
        git(repo, "remote", "add", name, url)
    return {"remote": name, "url": url, "existed": exists}
