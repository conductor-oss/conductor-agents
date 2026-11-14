"""Closure plan WP2 (H11/H17): in-session feedback selection, the hook watcher, and checkpoints."""
import os

import common  # noqa: F401
from common import feedback, git


def _fact(author, ts, **data):
    return {"author": author, "ts": ts, "data": data}


def test_select_targets_since_and_urgency():
    facts = [
        _fact("human", 5.0, target="a", text="old", authority="human"),          # before prompt
        _fact("monitor", 11.0, target="a", text="m", authority="monitor", by="monitor"),
        _fact("human:ops@example.com", 12.0, target="team", text="t", authority="human",
              by="human:ops@example.com"),
        _fact("human", 13.0, target="agent:a", text="stop now", authority="human", urgency="blocking"),
        _fact("human", 14.0, target="b", text="not mine", authority="human"),
    ]
    out = feedback.select(facts, "a", since_ts=10.0, seen=set())
    assert [r["text"] for r in out["advisory"]] == ["t", "m"]  # human first
    assert [r["text"] for r in out["blocking"]] == ["stop now"]
    again = feedback.select(facts, "a", since_ts=10.0, seen={r["key"] for r in out["advisory"]})
    assert again["advisory"] == []


def test_watcher_injects_advisory_and_stops_on_blocking():
    facts = []
    w = feedback.FeedbackWatcher("run", "a", since_ts=10.0, every=1, read=lambda cid: facts)
    assert w.on_tool_call() == {}
    facts.append(_fact("human", 11.0, target="a", text="prefer stdlib", authority="human"))
    out = w.on_tool_call()
    assert "prefer stdlib" in out["hookSpecificOutput"]["additionalContext"] and "continue_" not in out
    assert w.on_tool_call() == {}  # delivered once
    facts.append(_fact("human", 12.0, target="a", text="wrong approach, stop", authority="human",
                       urgency="blocking"))
    out = w.on_tool_call()
    assert out["continue_"] is False and "wrong approach" in out["stopReason"]
    rep = w.report()
    assert rep["interruptedBy"]["text"] == "wrong approach, stop"
    assert [i["urgency"] for i in rep["feedbackInjections"]] == ["advisory", "blocking"]
    assert all(i["latencySec"] >= 0 for i in rep["feedbackInjections"])


def _repo(tmp_path):
    repo = str(tmp_path / "r")
    git.ensure_ready(repo)
    with open(os.path.join(repo, "a.py"), "w") as fh:
        fh.write("A = 1\n")
    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-m", "base")
    return repo


def test_checkpoint_snapshots_untracked_without_touching_branch_or_index(tmp_path):
    repo = _repo(tmp_path)
    head = git.head(repo)
    with open(os.path.join(repo, "a.py"), "w") as fh:
        fh.write("A = 2\n")
    with open(os.path.join(repo, "new.py"), "w") as fh:
        fh.write("N = 1\n")
    snap = feedback.checkpoint(repo, label="t")
    assert snap and git.head(repo) == head
    assert git.git(repo, "diff", "--cached", "--name-only").stdout.strip() == ""  # index untouched
    files = git.git(repo, "show", "--name-only", "--format=", snap["sha"]).stdout.split()
    assert sorted(files) == ["a.py", "new.py"]
    assert git.git(repo, "rev-parse", f"{snap['sha']}^1").stdout.strip() == head


def test_restore_brings_back_lost_work_only_onto_the_same_head(tmp_path):
    repo = _repo(tmp_path)
    with open(os.path.join(repo, "new.py"), "w") as fh:
        fh.write("N = 1\n")
    snap = feedback.checkpoint(repo, label="t")
    # Simulate the worker loss: the working tree changes are gone.
    os.remove(os.path.join(repo, "new.py"))
    restored = feedback.restore(repo)
    assert restored["sha"] == snap["sha"] and restored["files"] == ["new.py"]
    assert open(os.path.join(repo, "new.py")).read() == "N = 1\n"
    assert feedback.restore(repo) is None  # dirty tree -> never clobbers live work


def test_watcher_checkpoints_every_n_calls(tmp_path):
    repo = _repo(tmp_path)
    w = feedback.FeedbackWatcher("", "", checkpoint_every=2, worktree=repo)
    assert w.active
    with open(os.path.join(repo, "x.py"), "w") as fh:
        fh.write("X = 1\n")
    w.on_tool_call()
    w.on_tool_call()
    assert len(w.report()["checkpoints"]) == 1 and w.report()["checkpoints"][0]["call"] == 2


def test_stop_hook_catches_feedback_that_arrived_in_the_final_turn():
    facts = []
    w = feedback.FeedbackWatcher("run", "a", since_ts=10.0, every=1, read=lambda cid: facts)
    assert w.on_stop() == {}  # nothing new -> let it finish
    facts.append(_fact("human:ops@example.com", 11.0, target="a", text="also add a docstring",
                       by="human:ops@example.com"))
    out = w.on_stop()
    assert out["decision"] == "block" and "also add a docstring" in out["reason"]
    assert w.report()["feedbackInjections"][0]["at"] == "stop"
    assert w.on_stop() == {}  # delivered once, then it may finish
    for n in range(5):  # bounded: never keeps an agent alive forever
        facts.append(_fact("human:ops@example.com", 20.0 + n, target="a", text=f"more {n}",
                           by="human:ops@example.com"))
        w.on_stop()
    assert w.stop_blocks == feedback.FeedbackWatcher.MAX_STOP_BLOCKS
    facts.append(_fact("human:ops@example.com", 40.0, target="a", text="stop", urgency="blocking",
                       by="human:ops@example.com"))
    assert w.on_stop() == {} and w.report()["interruptedBy"]["text"] == "stop"
