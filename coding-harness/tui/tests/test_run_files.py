"""The run page's Files tab reads a run's files wherever they live now (run_files)."""
from __future__ import annotations

import os
import subprocess

import run_files


def _git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    open(os.path.join(repo, "seed.txt"), "w").write("seed\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-qc", "swarm/x-1")
    os.makedirs(os.path.join(repo, "design")); os.makedirs(os.path.join(repo, "src"))
    open(os.path.join(repo, "design", "architecture.md"), "w").write("# Architecture\n")
    open(os.path.join(repo, "src", "app.js"), "w").write("app\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "run")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "master")
    return repo, base, head


def test_files_are_read_by_the_delivered_commit_even_after_merge_and_branch_deletion(tmp_path):
    repo, base, head = _repo(tmp_path)
    wf = {"status": "COMPLETED", "input": {"repoPath": repo}, "tasks": []}
    handoff = {"sourceRepoPath": repo, "branch": "swarm/x-1", "commit": head, "originalHead": base}
    loc = run_files.locate(wf, handoff)
    listed = run_files.listing(loc)
    assert loc["source"] == "commit" and "swarm/x-1" in loc["note"]
    assert listed["artifacts"] == ["design/architecture.md"]
    assert listed["changedByStatus"] == {"A": 2} and "changed" not in listed  # totals only, marks per file
    # One folder level at a time, folders first, each marked when the run changed something under it.
    assert [(e["name"], e["type"], e["changed"]) for e in listed["entries"]] == [
        ("design", "dir", "•"), ("src", "dir", "•"), ("seed.txt", "file", "")]
    assert [(e["name"], e["changed"]) for e in run_files.listing(loc, "src", overview=False)["entries"]] == [("app.js", "A")]
    assert run_files.read(loc, "design/architecture.md")["content"] == "# Architecture\n"
    assert "error" in run_files.read(loc, "../seed.txt")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "swarm/x-1"); _git(repo, "branch", "-qD", "swarm/x-1")
    loc = run_files.locate(wf, handoff)
    assert loc["source"] == "commit" and "on master" in loc["note"]
    assert run_files.read(loc, "src/app.js")["content"] == "app\n"


def test_a_running_run_is_read_live_from_its_working_tree(tmp_path):
    repo, base, head = _repo(tmp_path)
    _git(repo, "switch", "-q", "swarm/x-1")
    open(os.path.join(repo, "src", "new.js"), "w").write("new\n")  # an agent's file, not committed yet
    wf = {"status": "RUNNING", "input": {"repoPath": repo},
          "tasks": [{"referenceTaskName": "setup", "outputData": {"workdir": repo, "base": base}}]}
    loc = run_files.locate(wf, {})  # no result record yet: the start comes from the run's setup
    listed = run_files.listing(loc)
    assert loc["source"] == "working tree"
    assert listed["changedByStatus"].get("A", 0) >= 1
    assert ("src", "•") in [(e["name"], e["changed"]) for e in listed["entries"]]
    assert run_files.read(loc, "src/new.js")["content"] == "new\n"
    assert "error" in run_files.read(loc, "../../etc/hosts")


def test_an_unignored_dependency_folder_is_one_entry_not_thousands(tmp_path):
    """Early in a run, before the project ignores node_modules, a live tree held 44,117 untracked files and
    listing each made the Files tab crawl: an untracked folder is one entry, and folders load one at a time."""
    import time
    repo, base, head = _repo(tmp_path)
    _git(repo, "switch", "-q", "swarm/x-1")
    for n in range(3000):
        d = os.path.join(repo, "node_modules", f"pkg{n % 300}")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, f"f{n}.js"), "w").write("x")
    wf = {"status": "RUNNING", "input": {"repoPath": repo},
          "tasks": [{"referenceTaskName": "setup", "outputData": {"workdir": repo, "base": base}}]}
    loc = run_files.locate(wf, {})
    started = time.monotonic()
    listed = run_files.listing(loc)
    assert time.monotonic() - started < 2
    assert listed["changedTotal"] <= 5  # the untracked folder counts once, not 3000 times
    assert [e["name"] for e in listed["entries"]].count("node_modules") == 1
    assert len(run_files.listing(loc, "node_modules", overview=False)["entries"]) == 300


def test_hidden_files_are_not_shown_and_deleted_files_are_marked_in_their_folder(tmp_path):
    repo, base, head = _repo(tmp_path)
    _git(repo, "switch", "-q", "swarm/x-1")
    open(os.path.join(repo, ".gitignore"), "w").write("*.log\n")
    os.makedirs(os.path.join(repo, ".cc")); open(os.path.join(repo, ".cc", "design.md"), "w").write("brief")
    _git(repo, "rm", "-q", "seed.txt")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "more")
    wf = {"status": "COMPLETED", "input": {"repoPath": repo}, "tasks": []}
    loc = run_files.locate(wf, {"sourceRepoPath": repo, "branch": "swarm/x-1", "commit": _git(repo, "rev-parse", "HEAD"),
                                "originalHead": base})
    listed = run_files.listing(loc)
    names = [(e["name"], e["changed"]) for e in listed["entries"]]
    assert not [n for n, _ in names if n.startswith(".")]
    assert ("seed.txt", "D") in names and listed["changedByStatus"]["D"] == 1
    assert listed["artifacts"] == ["design/architecture.md"]  # .cc/design.md is hidden
