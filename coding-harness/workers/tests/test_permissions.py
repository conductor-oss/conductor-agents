"""Permission requests (docs/PERMISSIONS.md): agents ask instead of retrying what they are refused; the run's
history, the repository's standing grants and the policy decide what they can; the rest waits for an
approver (a person or an agent) deciding through the API; grants reach the next sessions' sandbox."""
import os
import threading
import time

import pytest

from common import codex, git, permissions


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    monkeypatch.setenv("CONDUCTOR_HARNESS_HOME", str(tmp_path / "home"))
    repo = str(tmp_path / "repo")
    git.ensure_ready(repo)
    return repo


def test_the_policy_allows_the_repositorys_files_never_git_or_outside_and_leaves_the_rest_to_an_approver(board):
    assert permissions.policy("write", "src/a.py", board)[0] == "grant"
    assert permissions.policy("write", ".codex/INSTALL.md", board)[0] == "grant"
    assert permissions.policy("write", ".git/config", board)[0] == "deny"
    assert permissions.policy("write", "../elsewhere.txt", board)[0] == "deny"
    assert permissions.policy("write", ".codex/config.toml", board) is None  # an agent's own sandbox config
    assert permissions.policy("network", "https://pypi.org/simple", board)[0] == "grant"
    assert permissions.policy("network", "api.example.com", board) is None
    assert permissions.policy("decision", "rename the public API", board) is None


def test_requests_are_decided_once_and_a_repository_grant_holds_for_later_runs(board):
    asked = [{"kind": "write", "target": "./src/a.py", "why": "fix"},
             {"kind": "network", "target": "api.example.com", "why": "the client calls it"},
             {"kind": "write", "target": ".git/hooks/x", "why": "a hook"}]
    out = permissions.submit("r1", 1, asked, workdir=board, repo_key="github:o/r")
    assert {d["target"]: d["decision"] for d in out["decided"]} == {"src/a.py": "grant", ".git/hooks/x": "deny"}
    assert [r["target"] for r in out["pending"]] == ["api.example.com"]
    # Asked again next round: nothing new is decided and nothing is asked twice.
    again = permissions.submit("r1", 2, asked, workdir=board, repo_key="github:o/r")
    assert again["decided"] == [] and [r["target"] for r in again["pending"]] == ["api.example.com"]
    assert len(permissions.requests("r1")) == 3
    rid = out["pending"][0]["id"]
    permissions.decide("r1", rid, "grant", by="agent:approver", scope="repository", note="read-only API")
    assert permissions.pending("r1") == []
    with pytest.raises(ValueError, match="already decided"):
        permissions.decide("r1", rid, "deny", by="human:a@b")
    g = permissions.grants("r1")
    assert g["writableRoots"] == ["src/a.py"] and g["allowedDomains"] == ["api.example.com"]
    assert "by agent:approver: read-only API" in g["text"]
    # Another run on the same repository: granted by the standing grant, no approver needed.
    later = permissions.submit("r2", 1, asked[1:2], workdir=board, repo_key="github:o/r")
    assert later["pending"] == [] and later["decided"][0]["by"] == "policy:repository"


def test_the_round_waits_until_an_approver_decides(board, fake_task_input, monkeypatch):
    from swarm import approval
    import common.progress as progress
    monkeypatch.setattr(progress.ProgressReporter, "start", lambda self: self)
    monkeypatch.setattr(progress.ProgressReporter, "stop", lambda self: None)
    monkeypatch.setattr(approval, "POLL_S", 0.05)
    evaluation = {"structured": {"permissions": [{"kind": "write", "target": ".codex/config.toml",
                                                  "why": "the project sets its Codex model"}]}}

    def approve_soon():
        for _ in range(100):
            waiting = permissions.pending("run-w")
            if waiting:
                permissions.decide("run-w", waiting[0]["id"], "grant", by="human:a@b", note="ok")
                return
            time.sleep(0.02)
    threading.Thread(target=approve_soon).start()
    res = approval.swarm_permissions(fake_task_input(runId="run-w", round=1, evaluation=evaluation, workdir=board,
                                                     repoPath=board, delivered=False, stop=False))
    out = res.output_data
    assert res.status == "COMPLETED" and out["pending"] == [] and out["writableRoots"] == [".codex/config.toml"]
    done = approval.swarm_permissions(fake_task_input(runId="run-x", round=1, evaluation=evaluation, workdir=board,
                                                      delivered=True))
    assert done.output_data["pending"] == [] and permissions.requests("run-x") == {}  # a delivered run asks nothing


def test_a_granted_path_opens_the_codex_sandbox_and_git_never_does(board):
    roots = codex.writable_roots(board, [".codex/config.toml", ".git/config", "../out", "docs/x.md"])
    assert roots == [os.path.join(os.path.realpath(board), ".codex"), os.path.join(os.path.realpath(board), "docs/x.md")]


def test_an_agent_approvers_token_writes_as_that_agent_not_as_a_person(tmp_path, monkeypatch):
    from common import actors
    monkeypatch.setenv("SWARM_ACTORS_FILE", str(tmp_path / "actors.json"))
    agent = actors.register("approver@agents.local", "permission-approver", authority="agent")
    person = actors.register("a@b.c", "A")
    assert actors.resolve(agent)["author"] == "agent:permission-approver"
    assert actors.resolve(person)["author"] == "human:a@b.c"
    assert actors.authority_of("agent:permission-approver") == "agent"
