"""Test runs with an expected duration (common/test_budget.py): a run far past its usual time is stopped with
everything it started and reported; a test runner that would hang on an open handle exits; nothing else
changes (run 221d632a: fifteen hung `node --test` runs, nine agents waiting half an hour each)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from common import check_execution, test_budget


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    test_budget.install(str(root))
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    # A stand-in `go`: `go test` sleeps (a hung suite) unless told how long, everything else answers at once.
    (fake / "go").write_text("#!/bin/sh\nif [ \"$1\" = test ]; then sleep \"${GO_SLEEP:-600}\"; fi\necho go-$1\n")
    (fake / "go").chmod(0o755)
    env = {**os.environ, "PATH": os.pathsep.join([str(root / test_budget.BIN_REL), str(fake), os.environ["PATH"]])}
    return root, env


def _run(argv, root, env, **kw):
    return subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, **kw)


def test_every_runner_has_a_wrapper_and_other_uses_pass_straight_through(tree):
    root, env = tree
    assert sorted(os.listdir(root / test_budget.BIN_REL)) == sorted(test_budget.SHIMS)
    out = _run(["go", "version"], root, env)
    assert out.returncode == 0 and out.stdout.strip() == "go-version"
    assert not (root / test_budget.TIMES_REL).exists()  # not a test run: nothing recorded


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_node_test_that_leaves_a_server_open_still_exits(tree):
    root, env = tree
    (root / "open.test.mjs").write_text("import { test } from 'node:test';\nimport net from 'node:net';\n"
                                         "test('leaves a server listening', () => { net.createServer().listen(0); });\n")
    started = time.monotonic()
    out = _run(["node", "--test", "open.test.mjs"], root, env)
    assert out.returncode == 0 and time.monotonic() - started < 20, out.stderr
    times = json.load(open(root / test_budget.TIMES_REL))
    assert times["node --test open.test.mjs"][0]["done"] is True


def test_a_run_past_its_budget_is_stopped_with_everything_it_started(tree):
    root, env = tree
    started = time.monotonic()
    out = _run(["go", "test", "./..."], root, {**env, "TEST_BUDGET_S": "2"})
    assert out.returncode == 124 and time.monotonic() - started < 15
    assert "[harness] stopped `go test ./...` after 2 s" in out.stderr and "hung, not slow" in out.stderr
    assert subprocess.run(["pgrep", "-f", "sleep 600"], capture_output=True).returncode != 0  # nothing left
    times = json.load(open(root / test_budget.TIMES_REL))
    assert times["go test ./..."][-1]["done"] is False


def test_the_budget_comes_from_the_commands_own_history(tree):
    root, env = tree
    assert _run(["go", "test", "./pkg"], root, {**env, "GO_SLEEP": "0"}).returncode == 0
    times = json.load(open(root / test_budget.TIMES_REL))
    assert times["go test ./pkg"][-1]["done"] is True and times["go test ./pkg"][-1]["s"] < 5
    # One that ran once in 0.1 s gets max(3 x 0.1 + 20, 30) = 30 s, not the tool's 300 s first-run default.
    with open(root / test_budget.TIMES_REL, "w") as fh:
        json.dump({"go test ./slow": [{"s": 0.1, "done": True}]}, fh)
    proc = subprocess.Popen(["go", "test", "./slow"], cwd=root, env=env, stderr=subprocess.PIPE, text=True)
    _, err = proc.communicate()
    assert proc.returncode == 124 and "after 30 s (it has taken at most 0 s in this tree)" in err


def test_killing_the_wrapper_takes_the_test_run_down_with_it(tree):
    """The agent's own command timeout kills the wrapper: the suite must not live on holding its port."""
    root, env = tree
    proc = subprocess.Popen(["go", "test", "./held"], cwd=root, env={**env, "GO_SLEEP": "777"})
    time.sleep(1.0)
    assert subprocess.run(["pgrep", "-f", "sleep 777"], capture_output=True).returncode == 0
    proc.send_signal(signal.SIGTERM)
    proc.wait()
    time.sleep(0.5)
    assert subprocess.run(["pgrep", "-f", "sleep 777"], capture_output=True).returncode != 0


def test_a_check_past_its_expected_duration_is_stopped(tmp_path):
    started = time.monotonic()
    r = check_execution.execute([sys.executable, "-c", "import time; print('working', flush=True); time.sleep(30)"],
                                cwd=str(tmp_path), env=dict(os.environ), expected_s=1.5)
    assert r.overran and not r.stalled and r.exit_code != 0 and time.monotonic() - started < 10
    assert "far past how long this command has taken before" in r.stderr
    fine = check_execution.execute([sys.executable, "-c", "print('ok')"], cwd=str(tmp_path), env=dict(os.environ),
                                   expected_s=30)
    assert fine.passed and not fine.overran


def test_measure_learns_each_checks_duration_and_holds_it_to_that(tmp_path, fake_task_input):
    from common import git
    from swarm.tasks import CHECK_TIMES, _check_budget, swarm_measure
    repo = str(tmp_path / "clone")
    git.ensure_ready(repo)
    os.makedirs(os.path.join(repo, ".cc"), exist_ok=True)
    with open(os.path.join(repo, ".cc", "checks.json"), "w") as fh:
        json.dump([{"id": "quick", "command": f"{sys.executable} -c pass"}], fh)
    first = swarm_measure(fake_task_input(workdir=repo, label="round 1")).output_data
    assert first["allGreen"] is True and first["results"][0]["typicalSeconds"] is None  # no history: no budget
    times = json.load(open(os.path.join(repo, CHECK_TIMES)))
    assert list(times) == [f"quick|{sys.executable} -c pass"]
    assert _check_budget(times, {"id": "quick", "command": f"{sys.executable} -c pass"})[0] < 70
    assert _check_budget({"slow|x": [100.0]}, {"id": "slow", "command": "x"}) == (360.0, 100.0)


def test_the_plan_gate_accepts_documents_a_designer_will_write(tmp_path):
    from common import git
    from swarm import spec
    work = str(tmp_path / "w")
    git.ensure_ready(work)
    plan = {"spec": {"kind": "none", "why": "x"}, "design": {"docs": ["design/lua.md", "design/other.md"], "why": "x"},
            "items": [{"id": "lua-design", "kind": "design", "owns": ["design/lua.md"]}]}
    out = spec.gate(work, plan, "auto", "auto", "")
    assert out["ok"] is False and "design/other.md" in out["feedback"] and "design/lua.md" not in out["feedback"]


def _git_tree(root):
    from common import git
    git.ensure_ready(str(root))
    git.exclude_path(str(root), "/.cc-cache/")
    git.git(str(root), "add", "-A")
    git.git(str(root), "commit", "-q", "-m", "tree", check=False)


def test_the_same_test_on_the_same_tree_is_not_run_again(tree):
    root, env = tree
    _git_tree(root)
    (root / "app.txt").write_text("v1\n")
    first = _run(["go", "test", "./x"], root, {**env, "GO_SLEEP": "0"})
    again = _run(["go", "test", "./x"], root, {**env, "GO_SLEEP": "0"})
    assert first.returncode == 0 and again.returncode == 0
    assert "already ran on exactly this tree" in again.stderr and "go-test" in again.stderr  # the earlier output
    forced = _run(["go", "test", "./x"], root, {**env, "GO_SLEEP": "0", "TEST_RERUN": "1"})
    assert "already ran" not in forced.stderr
    (root / "app.txt").write_text("v2\n")  # anyone changes anything: it runs again
    changed = _run(["go", "test", "./x"], root, {**env, "GO_SLEEP": "0"})
    assert "already ran" not in changed.stderr
    outcomes = [e["outcome"] for e in test_budget.runs(str(root))]
    assert outcomes == ["passed", "reused", "passed", "passed"]
    s = test_budget.summary(test_budget.runs(str(root)))
    assert s["runs"] == 3 and s["reused"] == 1 and s["sameSessionRepeats"] == 2
    assert "1 not run again because the tree had not changed" in test_budget.summary_text(s)


def test_an_agent_reuses_the_harness_check_run_of_the_same_tree_even_after_the_commit(tree):
    root, env = tree
    (root / ".cc" / "checks").mkdir(parents=True)
    (root / ".cc" / "checks" / "c.mjs").write_text("console.log('checked')\n")
    (root / "src.txt").write_text("code\n")
    _git_tree(root)
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    (root / "src.txt").write_text("the round's work, not committed yet\n")
    harness = _run(["node", ".cc/checks/c.mjs"], root, {**env, **test_budget.HARNESS_ENV})
    assert harness.returncode == 0 and "checked" in harness.stdout
    from common import git
    git.git(str(root), "add", "-A")
    git.git(str(root), "commit", "-q", "-m", "the harness commits the round")  # same content, new commit
    agent = _run(["node", ".cc/checks/c.mjs"], root, env)
    assert "already ran on exactly this tree" in agent.stderr and "by the harness" in agent.stderr
    assert [e["outcome"] for e in test_budget.runs(str(root))] == ["passed", "reused"]
    assert test_budget.summary(test_budget.runs(str(root)))["harnessRuns"] == 1
