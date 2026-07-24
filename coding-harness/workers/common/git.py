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
import time
from contextlib import contextmanager
from pathlib import Path

from .exec import RunError, run

WORKTREES = ".cc-worktrees"
GROUP_BRANCH = "cc-group-{name}"
RUN_BRANCH = "conductor/run-{name}"

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


def common_gitdir(repo: str) -> str:
    """Canonical shared Git directory for a checkout or any linked worktree."""
    return os.path.realpath(_common_gitdir(os.path.abspath(repo)))


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return value[:96] or "workspace"


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


def ensure_ready(repo: str, *, name: str = "conductor-code",
                 email: str = "harness@conductor.local") -> dict:
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
            "initialCommitCreated": committed, "branch": branch,
            "head": _trim(git(repo, "rev-parse", "HEAD").stdout)}


def branch(repo: str, name: str) -> dict:
    git(repo, "checkout", "-B", name)
    return {"branch": name}


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


def commit(repo: str, message: str = "conductor-code change", *,
           force_add_paths: list[str] | tuple[str, ...] | None = None) -> dict:
    # Serialized on the shared git dir: parallel forks committing to sibling
    # worktrees write shared refs/reflog and can otherwise collide.
    with _repo_lock(repo):
        _git_retry(lambda: git(repo, "add", "-A"))
        paths = _validated_relative_paths(repo, force_add_paths)
        if paths:
            _git_retry(lambda: git(repo, "add", "-f", "--", *paths))
        git(repo, "commit", "-m", message or "conductor-code change", check=False)  # no-op if nothing staged
        sha = _trim(git(repo, "rev-parse", "--short", "HEAD").stdout)
    return {"commit": sha}


def worktree_add(repo: str, name: str, *, preserve_existing: bool = False) -> dict:
    wt = os.path.join(repo, WORKTREES, name)
    br = GROUP_BRANCH.format(name=name)
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
    initial = _trim(git(wt, "rev-parse", "HEAD").stdout)
    return {"worktreePath": wt, "branch": br, "initialCommit": initial, "resumed": False}


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
    br = branch_name or RUN_BRANCH.format(name=_safe_name(workflow_id))
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
        checked_out = git(repo, "worktree", "list", "--porcelain", check=False).stdout
        exists = git(repo, "show-ref", "--verify", f"refs/heads/{br}", check=False).code == 0
        if exists or f"branch refs/heads/{br}\n" in checked_out:
            suffix = _safe_name(workflow_id)[:12]
            candidate = f"{br}/{suffix}"
            counter = 2
            while git(repo, "show-ref", "--verify", f"refs/heads/{candidate}",
                      check=False).code == 0:
                candidate = f"{br}/{suffix}-{counter}"
                counter += 1
            br = candidate
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
         destination_branch: str | None = None, set_upstream: bool = True,
         force_with_lease: bool = False) -> dict:
    """Push a branch to a remote. Uses --force-with-lease ONLY when asked (never a
    bare --force); sets upstream tracking by default."""
    br = branch_name or _current_branch(repo)
    args = ["push"]
    if set_upstream and "://" not in remote and not remote.startswith("git@"):
        args.append("--set-upstream")
    if force_with_lease:
        args.append("--force-with-lease")
    refspec = f"{br}:{destination_branch}" if destination_branch and destination_branch != br else br
    args += [remote, refspec]
    with _repo_lock(repo):
        _git_retry(lambda: git(repo, *args))
    return {"pushed": True, "branch": br, "destinationBranch": destination_branch or br,
            "remote": remote, "head": head(repo)}


def remote_set(repo: str, url: str, *, name: str = "origin") -> dict:
    """Idempotently point a remote at ``url``: set-url if it exists, else add it."""
    exists = git(repo, "remote", "get-url", name, check=False).code == 0
    if exists:
        git(repo, "remote", "set-url", name, url)
    else:
        git(repo, "remote", "add", name, url)
    return {"remote": name, "url": url, "existed": exists}
