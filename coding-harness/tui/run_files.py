"""Browse the files a run produced, wherever they now live.

A finished run reports the exact commit it delivered (``sourceHandoff.commit``). That commit stays in the
repository whether its branch still exists, was merged into master, or is checked out in a worktree, so
reading by commit (``git ls-tree`` / ``git show <commit>:<path>``) works in every case. When the
repository no longer has the commit, the branch is tried, then the run's own checkout. A run still in
progress is read from its working tree, so the files appear as agents write them.
"""
from __future__ import annotations

import os
import subprocess

MAX_BYTES = 512 * 1024
_ARTIFACT_PREFIXES = ("design/", "docs/", ".cc/design.md", ".cc/checks", "adr/", "doc/")
_ARTIFACT_NAMES = ("README", "README.md", "readme.md", "ARCHITECTURE.md", "CHANGELOG.md")


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace",
                          stdin=subprocess.DEVNULL)


def _has(repo: str, ref: str) -> bool:
    return bool(ref) and _git(repo, "cat-file", "-e", f"{ref}^{{commit}}").returncode == 0


def _setup(wf: dict) -> tuple[str, str]:
    """The run's own working tree (the swarm's clone or the team's workspace) and the commit it started
    from: a running run has no result record yet, and without its start the changed list was empty."""
    for t in wf.get("tasks") or []:
        o = t.get("outputData") or {}
        if t.get("referenceTaskName") not in ("setup", "workspace"):
            continue
        for key in ("workdir", "worktreePath"):
            if isinstance(o.get(key), str) and os.path.isdir(o[key]):
                return o[key], str(o.get("base") or o.get("initialCommit") or "")
    return "", ""


def _artifact(path: str) -> bool:
    return path.startswith(_ARTIFACT_PREFIXES) or os.path.basename(path) in _ARTIFACT_NAMES


def locate(wf: dict, handoff: dict) -> dict:
    """Where to read this run's files from: ``{source, repo, ref, root, base, note}``."""
    inp = wf.get("input") or {}
    repo = str(handoff.get("sourceRepoPath") or inp.get("repoPath") or "")
    commit, branch = str(handoff.get("commit") or ""), str(handoff.get("branch") or "")
    checkout, started_at = _setup(wf)
    base = str(handoff.get("originalHead") or started_at or "")
    running = str(wf.get("status") or "") == "RUNNING"
    if running and checkout:
        return {"source": "working tree", "repo": checkout, "ref": "", "root": checkout, "base": base,
                "note": f"the run's working tree, live: {checkout}"}
    if repo and os.path.isdir(repo):
        if _has(repo, commit):
            holders = [b.strip(" *+") for b in _git(repo, "branch", "--contains", commit, "--format=%(refname:short)").stdout.splitlines() if b.strip()]
            where = (f"on {', '.join(holders[:4])}" if holders else "on no branch any more (still in the repository)")
            merged = [b for b in holders if b != branch]
            return {"source": "commit", "repo": repo, "ref": commit, "root": "", "base": base,
                    "note": f"commit {commit[:12]} in {repo}, {where}" + (" (merged)" if merged and branch in holders else ""),
                    "branches": holders}
        if _has(repo, branch):
            return {"source": "branch", "repo": repo, "ref": branch, "root": "", "base": base,
                    "note": f"branch {branch} in {repo}"}
    for path in (checkout, str(handoff.get("repoPath") or "")):
        if path and os.path.isdir(path) and _has(path, commit or "HEAD"):
            return {"source": "checkout", "repo": path, "ref": commit if _has(path, commit) else "HEAD", "root": "",
                    "base": base, "note": f"the run's checkout {path}"}
    return {"source": "", "repo": repo, "ref": "", "root": "", "base": base,
            "note": "this run's files are not reachable: its commit, branch and checkout are all gone"}


_HARNESS_DIRS = {".git", ".cc-cache", ".cc-worktrees"}
_ARTIFACT_ROOTS = ("design", "docs", "doc", "adr", "README.md", "README", "ARCHITECTURE.md", "CHANGELOG.md")


def _hidden(path: str) -> bool:
    """A hidden file or folder anywhere in the path (``.gitignore``, ``.cc/``, ``.env``): not shown."""
    return any(part.startswith(".") for part in str(path).strip("/").split("/") if part)


def _clean(path: str) -> str:
    path = str(path or "").strip().strip("/")
    return "" if ".." in path.split("/") else path


def _changed(loc: dict) -> dict[str, str]:
    """``{path: A|M|D|R}`` against where the run started. An untracked folder of a live tree is ONE entry
    (``node_modules/``): early in a run, before the project ignores it, it held 44,117 files and listing
    each one made the Files tab crawl."""
    repo = loc.get("root") or loc.get("repo") or ""
    base = loc.get("base") or ""
    if not repo or not base or not _has(repo, base):
        return {}
    out: dict[str, str] = {}
    target = [loc["ref"]] if loc.get("ref") else []
    for line in _git(repo, "diff", "--name-status", base, *target).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out[parts[-1]] = parts[0][:1]
    if loc.get("root"):
        for p in _git(repo, "ls-files", "-o", "--exclude-standard", "--directory").stdout.splitlines():
            if p and not p.split("/")[0] in _HARNESS_DIRS:
                out.setdefault(p, "A")
    return out


def _artifacts(loc: dict) -> list[str]:
    """Design documents and the run's other artifacts, found where they live, never by walking the tree."""
    if loc.get("ref"):
        out = _git(loc["repo"], "ls-tree", "-r", "--name-only", loc["ref"], "--", *_ARTIFACT_ROOTS).stdout
        return sorted(p for p in out.splitlines() if p and _artifact(p) and not _hidden(p))[:200]
    root, found = loc.get("root") or "", []
    for name in _ARTIFACT_ROOTS:
        full = os.path.join(root, name)
        if os.path.isfile(full):
            found.append(name)
        elif os.path.isdir(full):
            for d, dirs, files in os.walk(full):
                dirs[:] = [x for x in dirs if x not in _HARNESS_DIRS and x != "node_modules"]
                found += [os.path.relpath(os.path.join(d, f), root) for f in files]
                if len(found) > 200:
                    break
    return sorted(p for p in found if _artifact(p) and not _hidden(p))[:200]


def entries(loc: dict, directory: str = "") -> list[dict]:
    """One folder level: ``[{name, path, type: dir|file}]``, folders first."""
    directory = _clean(directory)
    out: list[dict] = []
    if loc.get("ref"):
        spec = f"{loc['ref']}:{directory}" if directory else loc["ref"]
        for line in _git(loc["repo"], "ls-tree", spec).stdout.splitlines():
            meta, _, name = line.partition("\t")
            kind = meta.split()[1] if len(meta.split()) > 1 else "blob"
            out.append({"name": name, "path": f"{directory}/{name}" if directory else name,
                        "type": "dir" if kind == "tree" else "file"})
    elif loc.get("root"):
        root = os.path.realpath(loc["root"])
        here = os.path.realpath(os.path.join(root, directory))
        if not (here == root or here.startswith(root + os.sep)) or not os.path.isdir(here):
            return []
        names = sorted(e for e in os.listdir(here) if not (directory == "" and e in _HARNESS_DIRS) and e != ".git")
        rel = [f"{directory}/{n}" if directory else n for n in names]
        ignored = set()
        if rel:
            r = subprocess.run(["git", "-C", root, "check-ignore", "--stdin"], input="\n".join(rel), text=True,
                               capture_output=True, stdin=None)
            ignored = {p.strip() for p in r.stdout.splitlines() if p.strip()}
        for n, p in zip(names, rel):
            if p in ignored:
                continue
            out.append({"name": n, "path": p, "type": "dir" if os.path.isdir(os.path.join(here, n)) else "file"})
    return sorted((e for e in out if not e["name"].startswith(".")), key=lambda e: (e["type"] != "dir", e["name"].lower()))


def listing(loc: dict, directory: str = "", *, overview: bool = True) -> dict:
    """A folder of the run (``directory``, the root by default), each entry marked when the run changed it
    (a folder when anything under it changed); with ``overview``, the artifacts and the changed list too."""
    changed = {p: st for p, st in _changed(loc).items() if not _hidden(p)}
    dirs_with_changes: set[str] = set()
    for p in changed:
        parts = p.rstrip("/").split("/")
        for n in range(1, len(parts)):
            dirs_with_changes.add("/".join(parts[:n]))
    items = []
    for e in entries(loc, directory):
        mark = changed.get(e["path"]) or changed.get(e["path"] + "/") or ("•" if e["path"] in dirs_with_changes else "")
        items.append({**e, "changed": mark})
    # A file the run deleted is no longer in the tree: show it in its folder, marked D.
    here = _clean(directory)
    for p, st in sorted(changed.items()):
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        if st == "D" and parent == here:
            items.append({"name": p.rsplit("/", 1)[-1], "path": p, "type": "file", "changed": "D", "deleted": True})
    body = {**{k: loc.get(k) for k in ("source", "ref", "note", "branches")}, "dir": _clean(directory), "entries": items}
    if overview:
        body["artifacts"] = _artifacts(loc)
        # Totals only: each file carries its own mark in the folder view (a changed list of every file
        # could run to thousands of lines).
        totals: dict[str, int] = {}
        for st in changed.values():
            totals[st] = totals.get(st, 0) + 1
        body["changedTotal"] = len(changed)
        body["changedByStatus"] = totals
    return body


def read(loc: dict, path: str) -> dict:
    path = str(path or "").lstrip("/")
    if not path or ".." in path.split("/"):
        return {"path": path, "error": "not a file of this run"}
    if loc.get("root"):
        full = os.path.realpath(os.path.join(loc["root"], path))
        if not full.startswith(os.path.realpath(loc["root"]) + os.sep) or not os.path.isfile(full):
            return {"path": path, "error": "not a file of this run"}
        with open(full, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    elif loc.get("ref"):
        r = subprocess.run(["git", "-C", loc["repo"], "show", f"{loc['ref']}:{path}"], capture_output=True,
                           stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return {"path": path, "error": "not a file of this run"}
        data = r.stdout
    else:
        return {"path": path, "error": loc.get("note") or "no files"}
    if b"\0" in data[:8000]:
        return {"path": path, "binary": True, "bytes": len(data)}
    return {"path": path, "content": data[:MAX_BYTES].decode("utf-8", "replace"), "truncated": len(data) > MAX_BYTES}
