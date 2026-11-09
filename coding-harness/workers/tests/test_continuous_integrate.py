"""Phase 1 group 8: continuous-rebase integrator. Pure ordering/classification plus a real-git proof
that overlapping short-lived worktrees converge onto a moving shared head with no big-bang merge, and
that a conflict re-opens its item with a named mechanism instead of being dropped."""
import os

import common  # noqa: F401  -- sys.path shim for swarm_substrate

from common import continuous_integrate as ci
from common import git
from swarm_substrate import blackboard as bb


# ─────────────────────────── pure logic ──────────────────────────────────────
def test_integration_order_is_deterministic():
    items = [{"id": "c", "readyAt": 2}, {"id": "a", "readyAt": 1}, {"id": "b", "readyAt": 1}]
    assert [it["id"] for it in ci.integration_order(items)] == ["a", "b", "c"]


def test_classify_rebase_result():
    assert ci.classify_rebase_result(0, [])["status"] == "integrated"
    assert ci.classify_rebase_result(0, ["x.py"])["status"] == "conflict"
    assert ci.classify_rebase_result(1, [])["status"] == "conflict"


def test_reopen_record_names_mechanism():
    rec = ci.reopen_record("item-7", "main", ["schema.py"])
    assert rec["reason"] == "rebase_conflict" and "schema.py" in rec["mechanism"]
    assert rec["item"] == "item-7" and rec["conflicts"] == ["schema.py"]


# ─────────────────────────── real git ────────────────────────────────────────
def _write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def _commit_all(repo, msg):
    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-m", msg)


def _setup(repo, schema_text):
    git.ensure_ready(repo)
    _write(os.path.join(repo, "schema.py"), schema_text)
    _commit_all(repo, "base schema")
    return git._current_branch(repo)


def _worktree(repo, tmp, name, shared_branch):
    wt = os.path.join(tmp, f"wt-{name}")
    git.git(repo, "worktree", "add", wt, "-b", f"feat-{name}", shared_branch)
    return wt


def test_overlapping_worktrees_converge_without_big_bang_merge(tmp_path):
    repo = str(tmp_path / "repo")
    # One shared file, three edit sites separated by filler so each edit's diff context is disjoint
    # (adjacent-line edits legitimately conflict on rebase — a real property, exercised separately).
    sep = "\n".join(["# ---"] * 4)
    shared = _setup(repo, f"FIELD_A = None\n{sep}\nFIELD_B = None\n{sep}\nFIELD_C = None\n")
    items = []
    for idx, name in enumerate(["a", "b", "c"]):
        wt = _worktree(repo, str(tmp_path), name, shared)
        anchor = f"FIELD_{name.upper()}"
        _write(os.path.join(wt, "schema.py"),
               open(os.path.join(wt, "schema.py")).read().replace(f"{anchor} = None", f'{anchor} = "{name}"'))
        _commit_all(wt, f"set {anchor}")
        items.append({"id": name, "worktree": wt, "branch": f"feat-{name}", "readyAt": idx})

    out = ci.integrate_ready(repo, shared, items)

    assert out["big_bang_merge"] is False
    assert [i["item"] for i in out["integrated"]] == ["a", "b", "c"]
    assert out["reopened"] == []
    # All three overlapping edits landed on the shared file via continuous re-sync.
    final = open(os.path.join(repo, "schema.py")).read()
    assert 'FIELD_A = "a"' in final and 'FIELD_B = "b"' in final and 'FIELD_C = "c"' in final
    # The shared branch advanced by fast-forward only — no merge commit was created.
    graph = git.git(repo, "log", "--oneline", "--merges", shared, check=False).stdout
    assert graph.strip() == ""


def test_conflict_reopens_item_and_leaves_shared_branch_untouched(tmp_path):
    repo = str(tmp_path / "repo")
    shared = _setup(repo, "SHARED = None\n")
    items = []
    for idx, (name, val) in enumerate([("x", "1"), ("y", "2")]):
        wt = _worktree(repo, str(tmp_path), name, shared)
        _write(os.path.join(wt, "schema.py"), f"SHARED = {val}\n")  # both edit the SAME line
        _commit_all(wt, f"set SHARED={val}")
        items.append({"id": name, "worktree": wt, "branch": f"feat-{name}", "readyAt": idx})

    out = ci.integrate_ready(repo, shared, items)

    assert [i["item"] for i in out["integrated"]] == ["x"]          # x integrates
    assert len(out["reopened"]) == 1                                 # y conflicts -> re-opened
    reop = out["reopened"][0]
    assert reop["reason"] == "rebase_conflict" and "schema.py" in reop["conflicts"]
    # The mechanism names the other side of the collision — the shared-branch commit that last changed
    # the file (a sibling fork's test-cycle repair had written another item's file, run a33d7061).
    assert reop["touched"]["schema.py"].endswith("set SHARED=1")
    assert "schema.py (last changed on" in reop["mechanism"] and "set SHARED=1" in reop["mechanism"]
    head_after = out["final_head"]
    # shared branch sits at x's result (SHARED = 1), the conflict did not corrupt or advance it
    assert open(os.path.join(repo, "schema.py")).read().strip() == "SHARED = 1"
    assert head_after == git.head(repo)
    # the conflicted worktree is clean (rebase was aborted, not left mid-conflict)
    assert git.has_conflicts(os.path.join(str(tmp_path), "wt-y")) == []


def test_conflict_posts_reopen_fact_to_board(tmp_path):
    repo = str(tmp_path / "repo")
    shared = _setup(repo, "SHARED = None\n")
    be = bb._LocalBackend(str(tmp_path / "board"))
    items = []
    for idx, (name, val) in enumerate([("x", "1"), ("y", "2")]):
        wt = _worktree(repo, str(tmp_path), name, shared)
        _write(os.path.join(wt, "schema.py"), f"SHARED = {val}\n")
        _commit_all(wt, f"set SHARED={val}")
        items.append({"id": name, "worktree": wt, "branch": f"feat-{name}", "readyAt": idx})

    ci.integrate_ready(repo, shared, items, correlation_id="run-ci", backend=be)
    reopens = bb.read_facts("run-ci", kind="reopen", backend=be)
    assert len(reopens) == 1 and reopens[0]["data"]["item"] == "y"
