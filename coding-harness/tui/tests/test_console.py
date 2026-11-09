"""Phase 3 web Swarm Console: a view + editor over the boards (§4.8), stdlib only.

Exercises the pure request logic (handle_get/handle_post) and the steering writes without a socket:
the console reads the same fold the Lead/monitor use and its POST steer is an authored board write.
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(ROOT / "workers"))

import console  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}
from common import team_board as tb  # noqa: E402


def _seed(monkeypatch, tmp_path):
    bb = str(tmp_path / "bb")
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", bb)
    cid = "run-con"
    tb.post_item(cid, "lead", {"id": "email", "title": "Email channel", "files": ["registry.py"]})
    tb.set_state(cid, "lead", "email", tb.IN_PROGRESS)
    tb.post_lesson(cid, "x", "registry is append-only", confidence="high")
    tb.post_exception(cid, "monitor", "dead", "blocked", "terminal failure needs a human", severity="high")
    return bb, cid


def test_runs_and_board_and_page(monkeypatch, tmp_path):
    bb, cid = _seed(monkeypatch, tmp_path)
    status, ctype, body = console.handle_get("/api/runs", {}, bb)
    assert status == 200 and cid in json.loads(body)["runs"]

    status, ctype, body = console.handle_get("/api/board", {"run": [cid], "ceiling": ["10"]}, bb)
    v = json.loads(body)
    assert status == 200 and v["rollup"]["total"] == 1
    assert len(v["exceptions"]) == 1 and v["exceptions"][0]["kind"] == "blocked"
    assert v["burn"]["ceilingUsd"] == 10.0

    status, ctype, body = console.handle_get("/", {}, bb)
    assert status == 200 and b"Swarm Console" in body and ctype.startswith("text/html")

    assert console.handle_get("/nope", {}, bb)[0] == 404
    assert console.handle_get("/api/board", {}, bb)[0] == 400  # run required


def test_steer_nudge_is_a_board_write_and_approvals_do_not_exist(monkeypatch, tmp_path):
    bb, cid = _seed(monkeypatch, tmp_path)
    # Nudge the email item from the console.
    s, _, b = console.handle_post("/api/steer",
                                  json.dumps({"run": cid, "action": "nudge", "item": "email",
                                              "text": "prefer stdlib"}).encode(), bb, headers=AUTH)
    assert s == 200 and json.loads(b)["ok"] is True
    board = tb.fold_board(cid)
    # Authored as the registered actor resolved from the token — never a caller-supplied name (H16).
    assert any(d["target"] == "email" and d["by"] == "human:ops@example.com" for d in board["directives"])

    # Humans are passive observers: approval actions do not exist (the team decides).
    for action in ("resolve", "accept", "approve_contract", "reject_contract", "release"):
        s, _, _ = console.handle_post("/api/steer", json.dumps(
            {"run": cid, "action": action, "item": "dead", "kind": "blocked"}).encode(), bb, headers=AUTH)
        assert s == 400, action

    # Unknown action / bad json are rejected, not applied.
    assert console.handle_post("/api/steer", b'{"run":"x","action":"frobnicate"}', bb, headers=AUTH)[0] == 400
    assert console.handle_post("/api/steer", b'not json', bb, headers=AUTH)[0] == 400
    assert console.handle_post("/other", b'{}', bb, headers=AUTH)[0] == 404


def test_main_entrypoint_is_last_so_page_is_defined_before_serve_blocks():
    """Regression: the ``if __name__ == '__main__': _main()`` guard calls ``serve_forever()``,
    which blocks. If it sits above any module global ``handle_get`` needs (e.g. ``PAGE``), then
    under ``python -m tui.console`` execution blocks before that global is bound and ``GET /``
    raises ``NameError: name 'PAGE' is not defined`` at request time — while ``import console``
    (which runs the whole file) hides it. So the blocking entrypoint must be the LAST top-level
    statement, after ``PAGE``. ``handle_get('/')`` returning 200 on an imported module does NOT
    prove this; module execution order does.
    """
    src = (Path(console.__file__)).read_text()
    tree = ast.parse(src)
    main_guard_line = None
    page_assign_line = None
    for node in tree.body:  # top-level statements only
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"):
            main_guard_line = node.lineno
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PAGE" for t in node.targets):
            page_assign_line = node.lineno
    assert page_assign_line is not None, "PAGE must be a module-level global"
    assert main_guard_line is not None, "module must have an `if __name__ == '__main__'` guard"
    assert page_assign_line < main_guard_line, (
        "PAGE is defined after the blocking __main__ guard; under `python -m tui.console` "
        "serve_forever() blocks before PAGE binds and GET / raises NameError")
    assert main_guard_line == tree.body[-1].lineno, (
        "the blocking __main__ entrypoint must be the last top-level statement")


def test_terminate_ends_the_run_and_starts_its_retrospective():
    """The human's hard stop: terminate the run (and with it every agent), then start its retrospective
    (Conductor never runs a failureWorkflow for an API termination)."""
    calls = []

    class _Resp:
        def __init__(self, body=b""):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def opener(req):
        calls.append((req.get_method(), req.full_url, req.data))
        if req.get_method() == "GET":
            return _Resp(json.dumps({"input": {"repoPath": "/repo"}}).encode())
        return _Resp(b"retro-1" if req.get_method() == "POST" else b"")

    from board import terminate_run
    out = terminate_run("run-1", "wrong direction", author="human:ops@example.com", server_url="http://c/api",
                        opener=opener)
    assert out["terminated"] is True and out["retrospective"] == "retro-1"
    assert [c[0] for c in calls] == ["GET", "DELETE", "POST"]
    assert calls[1][1].startswith("http://c/api/workflow/run-1?reason=") and "wrong%20direction" in calls[1][1]
    assert calls[2][1] == "http://c/api/workflow/run_retrospective"
    body = json.loads(calls[2][2])
    assert body["workflowId"] == "run-1" and body["repoPath"] == "/repo" and body["verdict"] == "TERMINATED"

def test_program_run_shows_child_team_tree(monkeypatch, tmp_path):
    bb_dir, cid = _seed(monkeypatch, tmp_path)
    from common import blackboard as sbb
    tb.post_item("child-1", "lead", {"id": "x", "files": ["x.py"]})
    sbb.post_fact(cid, "child-1", "child", {"subsystem": "store", "childRun": "child-1"})
    sbb.post_fact(cid, "program", "rollup", {"subsystem": "store", "childRun": "child-1", "verdict": "DELIVERED",
                                             "delivered": True})
    v = json.loads(console.handle_get("/api/board", {"run": [cid]}, bb_dir)[2])
    assert v["children"][0]["subsystem"] == "store" and v["children"][0]["run"] == "child-1"
    assert v["children"][0]["rollup"]["total"] == 1 and v["children"][0]["verdict"] == "DELIVERED"
    # The page draws a program's teams as its rank strip (each child team a tile, drill-down by click).
    page = console.handle_get("/", {}, bb_dir)[2]
    assert b"function renderStrip" in page and b"Teams" in page


def test_steering_requires_a_registered_actor_and_supports_lifecycle(monkeypatch, tmp_path):
    bb, cid = _seed(monkeypatch, tmp_path)
    body = json.dumps({"run": cid, "action": "nudge", "item": "email", "text": "x"}).encode()
    assert console.handle_post("/api/steer", body, bb)[0] == 401
    assert console.handle_post("/api/steer", body, bb, headers={"Authorization": "Bearer nope"})[0] == 401
    s, _, b = console.handle_post("/api/steer", json.dumps(
        {"run": cid, "action": "nudge", "item": "email", "text": "do not use library X"}).encode(), bb, headers=AUTH)
    assert s == 200 and json.loads(b)["author"] == "human:ops@example.com"
    view = json.loads(console.handle_get("/api/board", {"run": [cid]}, bb)[2])
    con = [c for c in view["constraints"] if c["text"] == "do not use library X"][0]
    assert con["authority"] == "human" and con["id"]
    s, _, _ = console.handle_post("/api/steer", json.dumps(
        {"run": cid, "action": "replace", "item": "email", "directive": con["id"],
         "text": "library X is fine for parsing only"}).encode(), bb, headers=AUTH)
    view = json.loads(console.handle_get("/api/board", {"run": [cid]}, bb)[2])
    texts = [c["text"] for c in view["constraints"]]
    assert "do not use library X" not in texts and "library X is fine for parsing only" in texts
    new = [c for c in view["constraints"] if c["text"].startswith("library X")][0]
    console.handle_post("/api/steer", json.dumps({"run": cid, "action": "lift", "directive": new["id"]}).encode(),
                        bb, headers=AUTH)
    view = json.loads(console.handle_get("/api/board", {"run": [cid]}, bb)[2])
    assert not [c for c in view["constraints"] if c["text"].startswith("library X")]


def test_item_trace_links_dispatch_inputs_to_outcomes(monkeypatch, tmp_path):
    bb_dir, cid = _seed(monkeypatch, tmp_path)
    from common import blackboard as sbb
    sbb.post_fact(cid, "lead", "dispatch", {"item": "email", "candidate": "email", "attempt": 0,
                                            "waveBase": "abc123", "contractRevision": 1, "promptSha256": "f00",
                                            "lessons": [{"text": "registry is append-only"}],
                                            "directives": [{"id": "d1", "text": "prefer stdlib"}]})
    sbb.post_fact(cid, "integrator", "session", {"item": "email", "candidate": "email", "delivered": True,
                                                 "testsPassed": True, "costUsd": 0.12,
                                                 "feedbackInjections": [{"text": "x"}], "checkpoints": []})
    tb.post_review(cid, "reviewer_email", "email", "lgtm")
    s, _, b = console.handle_get("/api/trace", {"run": [cid], "item": ["email"]}, bb_dir)
    kinds = [e["kind"] for e in json.loads(b)["timeline"]]
    assert s == 200 and kinds.index("dispatch") < kinds.index("session") < kinds.index("review")
    text = " ".join(e["summary"] for e in json.loads(b)["timeline"])
    assert "1 lessons, 1 directives" in text and "1 feedback injection(s)" in text and "contract rev 1" in text
    assert console.handle_get("/api/trace", {"run": [cid]}, bb_dir)[0] == 400


def test_learning_board_shows_every_retrospective_learning(monkeypatch, tmp_path):
    """Selecting the cross-run learnings run showed nothing: the board folded team ``lesson`` facts while
    retrospectives record ``learning`` facts (27 invisible learnings after run fc6467b7)."""
    bb, cid = _seed(monkeypatch, tmp_path)
    from common import retrospective
    retrospective.record(
        [{"scope": "process", "text": "slice a work item's write root to its criteria", "evidence": "fc6467b7"},
         {"scope": "repo", "text": "Stats is declared once, in store/stats.go", "evidence": "fc6467b7"}],
        repo="/r", languages=["go"], host_name="h", run="fc6467b7")
    status, ctype, body = console.handle_get("/api/board", {"run": [retrospective.LEARNINGS_RUN]}, bb)
    v = json.loads(body)
    assert status == 200 and v["rollup"]["lessons"] == 2
    texts = [l["text"] for l in v["lessons"]]
    assert any(t.startswith("[repo:/r] Stats") for t in texts) and any(t.startswith("[process] slice") for t in texts)
    assert all(l["run"] == "fc6467b7" for l in v["lessons"])
    # A team run's board is unchanged: its own lessons, not the learnings.
    status, ctype, body = console.handle_get("/api/board", {"run": [cid]}, bb)
    assert json.loads(body)["rollup"]["lessons"] == 1


# ───────── launch, list and drill into runs (the console as the operating surface) ─────────
class _FakeConductor:
    """A Conductor REST stand-in: records calls, answers search / get / start."""

    def __init__(self, runs=None, start_id="new-run-1"):
        self.calls, self.runs, self.start_id = [], runs or {}, start_id

    def __call__(self, req):
        method, url = req.get_method(), req.full_url
        self.calls.append((method, url, json.loads(req.data) if req.data else None))
        if method == "GET" and "/workflow/search" in url:
            return _Resp({"results": list(self.runs.values())})
        if method == "GET" and "/workflow/" in url:
            wid = url.split("/workflow/")[1].split("?")[0]
            return _Resp(self.runs.get(wid, {}))
        if method == "POST":
            return _Resp(self.start_id)
        return _Resp({})


class _Resp:
    status = 200

    def __init__(self, obj):
        self.raw = (obj if isinstance(obj, str) else json.dumps(obj)).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.raw


def _program(cid="prog-1"):
    return {"workflowId": cid, "workflowName": "cc_program", "workflowType": "cc_program", "status": "RUNNING",
            "startTime": 1_700_000_000_000, "input": {"repoPath": "/work/resp-cache", "ceilingUsd": 150,
                                                       "description": "Build resp-cache: a Redis-style cache\nmore"},
            "output": {}, "tasks": [
                {"referenceTaskName": "rank_init", "outputData": {
                    "ranks": [["design"], ["resp", "store"]],
                    "subsystems": [{"id": "design", "deliverable": "docs", "items": [{"id": "d1"}, {"id": "d2"}]},
                                   {"id": "resp", "deliverable": "code"}, {"id": "store", "deliverable": "code"}]}},
                {"referenceTaskName": "rank_breakdown__1", "outputData": {"structured": {"subsystems": [
                    {"id": "resp", "items": [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]}]}}}]}


def test_swarm_run_list_titles_nests_children_and_falls_back_to_the_board(monkeypatch, tmp_path):
    bb, cid = _seed(monkeypatch, tmp_path)
    child = {"workflowId": "team-1", "workflowType": "team_build", "status": "COMPLETED", "startTime": 1,
             "endTime": 2, "input": {"parentCorrelationId": "prog-1", "subsystemId": "store"},
             "output": {"verdict": "DELIVERED", "spendUsd": 2.5}}
    fake = _FakeConductor({"prog-1": _program(), "team-1": child})
    listed = console.list_swarm_runs(opener=fake, server_url="http://c/api")
    rows = {r["id"]: r for r in listed["items"]}
    assert rows["prog-1"]["title"] == "Build resp-cache: a Redis-style cache"  # first line of the ask
    assert rows["prog-1"]["repoName"] == "resp-cache" and rows["prog-1"]["ceilingUsd"] == 150
    assert rows["team-1"]["parent"] == "prog-1" and rows["team-1"]["subsystem"] == "store"
    assert rows["team-1"]["verdict"] == "DELIVERED" and rows["team-1"]["spendUsd"] == 2.5
    assert "workflowType IN (cc_program,team_build,sdlc_intake)" in __import__("urllib.parse").parse.unquote(
        fake.calls[0][1])
    # Conductor unreachable: the endpoint still lists the runs on the board, and says why.
    monkeypatch.setenv("CONDUCTOR_SERVER_URL", "http://127.0.0.1:9/api")
    status, _, body = console.handle_get("/api/swarm-runs", {}, bb)
    out = json.loads(body)
    assert status == 200 and cid in [r["id"] for r in out["items"]] and "unreachable" in out["error"]


def test_run_view_reads_the_rank_strip_from_the_program_tasks(monkeypatch, tmp_path):
    bb, cid = _seed(monkeypatch, tmp_path)
    fake = _FakeConductor({cid: {**_program(cid)}})
    v = console.run_view(cid, blackboard_dir=bb, opener=fake, server_url="http://c/api")
    assert v["run"]["title"].startswith("Build resp-cache") and v["run"]["status"] == "RUNNING"
    assert [[s["id"] for s in rank] for rank in v["ranks"]] == [["design"], ["resp", "store"]]
    planned = {s["id"]: (s["deliverable"], s["planned"]) for rank in v["ranks"] for s in rank}
    # The breakdown's re-slice is what gets built: resp shows its three re-sliced items.
    assert planned == {"design": ("docs", 2), "resp": ("code", 3), "store": ("code", 1)}
    assert v["board"]["rollup"]["total"] == 1 and v["snapshot"]["status"] == "RUNNING"
    assert v["board"]["burn"]["ceilingUsd"] == 150 and v["conductor"] == ""
    # A run Conductor does not know still renders from its board.
    v2 = console.run_view(cid, blackboard_dir=bb, opener=_FakeConductor({}), server_url="http://c/api")
    assert v2["ranks"] == [] and v2["board"]["rollup"]["total"] == 1 and "board only" in v2["conductor"]


def test_launchers_come_from_the_catalog_and_launch_is_signed_and_validated(monkeypatch, tmp_path):
    bb, cid = _seed(monkeypatch, tmp_path)
    s, _, body = console.handle_get("/api/launchers", {}, bb)
    forms = {f["workflow"]: f for f in json.loads(body)["launchers"]}
    assert set(forms) == {"cc_program", "sdlc_intake"}
    fields = {f["name"]: f for f in forms["cc_program"]["fields"]}
    assert fields["repoPath"]["required"] is True and fields["ceilingUsd"]["default"] == 60
    assert fields["maxDepth"]["advanced"] is True
    # Unsigned launches are refused like any other write.
    body = json.dumps({"workflow": "cc_program", "values": {"repoPath": str(tmp_path), "description": "x"}}).encode()
    assert console.handle_post("/api/launch", body, bb)[0] == 401
    # Validation: a missing description, a repo that is not a directory, a workflow the console cannot start.
    fake = _FakeConductor()
    out = console.launch({"workflow": "cc_program", "values": {"repoPath": str(tmp_path), "description": ""}},
                         author="human:a@b", opener=fake)
    assert out["ok"] is False and "Program description" in out["error"]
    out = console.launch({"workflow": "cc_program", "values": {"repoPath": str(tmp_path / "nope"),
                                                                "description": "build it"}}, author="h", opener=fake)
    assert out["ok"] is False and "not a directory" in out["error"]
    assert console.launch({"workflow": "team_build", "values": {}}, author="h", opener=fake)["ok"] is False
    assert fake.calls == []  # nothing reached Conductor
    # A valid launch starts the workflow without a version and sends only changed optional fields.
    out = console.launch({"workflow": "cc_program", "values": {
        "repoPath": str(tmp_path), "description": "build it", "ceilingUsd": 60, "maxWidth": 8}},
        author="human:a@b", opener=fake, server_url="http://c/api")
    assert out == {"ok": True, "run": "new-run-1", "workflow": "cc_program", "author": "human:a@b"}
    method, url, payload = fake.calls[-1]
    assert method == "POST" and url == "http://c/api/workflow"  # no version anywhere in the start
    # The result branch reads as what was asked for (the run id is appended by the workspace).
    assert payload == {"name": "cc_program", "input": {"repoPath": str(tmp_path), "description": "build it",
                                                       "maxWidth": 8, "branchName": "swarm/run"}}


def test_whoami_names_the_actor_behind_a_token(monkeypatch, tmp_path):
    bb, cid = _seed(monkeypatch, tmp_path)
    s, _, b = console.handle_get("/api/whoami", {}, bb, headers=AUTH)
    assert s == 200 and json.loads(b)["email"] == "ops@example.com"
    assert console.handle_get("/api/whoami", {}, bb, headers={"Authorization": "Bearer nope"})[0] == 401


def test_add_actor_registers_a_private_token_the_console_accepts(monkeypatch, tmp_path):
    """Signing in to the console needed a hand-written actors.json; a new user had no way in."""
    path = tmp_path / "actors" / "actors.json"
    monkeypatch.setenv("SWARM_ACTORS_FILE", str(path))
    from common import actors
    token = actors.register("new@example.com", "New Person")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    s, _, b = console.handle_get("/api/whoami", {}, None, headers={"Authorization": f"Bearer {token}"})
    assert s == 200 and json.loads(b) == {"name": "New Person", "email": "new@example.com", "authority": "human",
                                         "author": "human:new@example.com"}
    second = actors.register("other@example.com")
    assert second != token and actors.resolve(token) and actors.resolve(second)["name"] == "other@example.com"
    import pytest
    with pytest.raises(ValueError):
        actors.register("not-an-email")


def test_a_failed_runs_snapshot_carries_why_it_failed():
    """Run edc1c893 failed before planning (its directory was not a git repository); the board held
    nothing, so "why did it fail?" was answered "the snapshot does not say". The cause comes from
    Conductor: the run's reason and each failed task's, once per distinct reason."""
    import swarm_state
    err = "coding_agent: git -C /tmp/pg2 rev-parse HEAD exited 128: fatal: not a git repository"
    wf = {"status": "FAILED", "reasonForIncompletion": "Task c181 failed with status: FAILED",
          "tasks": [{"referenceTaskName": "program_plan__1", "taskType": "coding_agent", "status": "FAILED",
                     "reasonForIncompletion": err}] * 4 + [{"referenceTaskName": "memory_read", "status": "COMPLETED"}]}
    f = swarm_state._failure(wf)
    assert f["status"] == "FAILED" and f["failedTasks"] == [
        {"task": "program_plan__1", "type": "coding_agent", "reason": err, "attempts": 4}]
    assert swarm_state._failure({"status": "RUNNING"}) is None
    snap = {"workflow": "cc_program", "run": "edc1c893x", "phase": "finished (failed)", "teams": [],
            "activeAgents": [], "escalations": [], "spend": {"usd": 0, "ceilingUsd": 66}, "failure": f}
    assert "failed: coding_agent: git -C /tmp/pg2 rev-parse HEAD" in swarm_state.summarize(snap)
    assert "lead with it" in swarm_state._ASK_SYSTEM


def test_finished_runs_offer_retry_start_over_and_run_again(monkeypatch, tmp_path):
    """Retry resumes the same run from the failed step; start over is a NEW run with the same input,
    because Conductor's restart reuses the run id and the board is keyed by it (old items, spend twice)."""
    failed = {**_program("prog-f"), "status": "FAILED", "workflowName": "cc_program"}
    assert console.lifecycle_actions(failed) == {"retry": True, "startOver": True, "editAndRun": True}
    assert console.lifecycle_actions({**failed, "status": "TERMINATED"})["retry"] is True
    done = {**failed, "status": "COMPLETED"}
    assert console.lifecycle_actions(done) == {"retry": False, "startOver": True, "editAndRun": True}
    assert console.lifecycle_actions({**failed, "status": "RUNNING"}) == {
        "retry": False, "startOver": False, "editAndRun": False}
    child = {**failed, "workflowName": "team_build", "input": {"parentCorrelationId": "prog-f"}}
    assert console.lifecycle_actions(child) == {"retry": False, "startOver": False, "editAndRun": False}

    repo = tmp_path / "repo"
    repo.mkdir()
    failed["input"] = {**failed["input"], "repoPath": str(repo)}
    fake = _FakeConductor({"prog-f": failed, "prog-r": {**failed, "workflowId": "prog-r", "status": "RUNNING"}},
                          start_id="prog-new")
    assert console.retry_run("prog-f", opener=fake, server_url="http://c/api") == {"ok": True, "run": "prog-f"}
    assert fake.calls[-1][:2] == ("POST", "http://c/api/workflow/prog-f/retry?resumeSubworkflowTasks=false")
    refused = console.retry_run("prog-r", opener=fake, server_url="http://c/api")
    assert refused["ok"] is False and "running" in refused["error"]
    out = console.start_over("prog-f", opener=fake, server_url="http://c/api")
    assert out == {"ok": True, "run": "prog-new", "from": "prog-f", "workflow": "cc_program"}
    method, url, body = fake.calls[-1]
    assert method == "POST" and url == "http://c/api/workflow" and body == {"name": "cc_program",
                                                                            "input": failed["input"]}
    # The run view tells the page which actions apply and hands it the input to prefill a new run.
    bb, cid = _seed(monkeypatch, tmp_path)
    v = console.run_view("prog-f", blackboard_dir=bb, opener=fake, server_url="http://c/api")
    assert v["actions"]["retry"] is True and v["workflowInput"]["description"].startswith("Build resp-cache")
    # Both are signed writes.
    assert console.handle_post("/api/retry", b'{"run":"prog-f"}', bb)[0] == 401
    assert console.handle_post("/api/start-over", b'{"run":"prog-f"}', bb)[0] == 401


def _git(repo, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _delivered_repo(tmp_path):
    """A repository after a run: master untouched, the run's branch one commit ahead, still checked out
    in the run's worktree (as a finished run left it before the release step existed)."""
    repo = tmp_path / "pg"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "master")
    (repo / "README.md").write_text("pg\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    wt = repo / ".cc-worktrees" / "run-prog-done"
    _git(repo, "worktree", "add", "-b", "swarm/pg-database-progdone", str(wt))
    (wt / "db.py").write_text("print('db')\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "the database")
    done = {"workflowId": "prog-done", "workflowName": "cc_program", "workflowType": "cc_program",
            "status": "COMPLETED", "input": {"repoPath": str(repo), "description": "Build a pg database"},
            "output": {"verdict": "DELIVERED", "report": "Built the database.\n\nDetails.", "sourceHandoff": {
                "sourceRepoPath": str(repo), "repoPath": str(wt), "branch": "swarm/pg-database-progdone",
                "originalBranch": "master"}}, "tasks": []}
    return repo, wt, done


def test_a_finished_runs_result_is_its_branch_with_what_comes_next(monkeypatch, tmp_path):
    """The work of a finished run is a branch; the run page shows it and what can be done with it."""
    repo, wt, done = _delivered_repo(tmp_path)
    fake = _FakeConductor({"prog-done": done})
    r = console.run_result(done, opener=fake, server_url="http://c/api")
    assert (r["branch"], r["base"], r["commits"], r["files"], r["merged"]) == (
        "swarm/pg-database-progdone", "master", 1, 1, False)
    # No remote: continue and merge apply, a pull request does not.
    assert r["actions"] == {"continue": True, "pullRequest": False, "merge": True}
    assert r["remote"] is None
    _git(repo, "remote", "add", "origin", "https://github.com/acme/pg.git")
    r = console.run_result(done, opener=fake, server_url="http://c/api")
    assert r["actions"]["pullRequest"] is True and r["remote"]["github"] == "acme/pg"
    assert console.run_result({**done, "status": "RUNNING"}, opener=fake)["actions"] == {
        "continue": False, "pullRequest": False, "merge": False}


def test_continue_builds_a_follow_up_run_on_the_finished_runs_branch(monkeypatch, tmp_path):
    repo, wt, done = _delivered_repo(tmp_path)
    fake = _FakeConductor({"prog-done": done}, start_id="prog-next")
    out = console.launch({"workflow": "cc_program", "continueFrom": "prog-done",
                          "values": {"repoPath": "/ignored", "description": "Add docs for the wire protocol"}},
                         author="human:a@b", opener=fake, server_url="http://c/api")
    follow = repo / ".cc-worktrees" / "continue-swarm-pg-database-progdone"
    assert out["ok"] is True and out["continues"] == "prog-done" and out["repoPath"] == str(follow)
    # The finished run's checkout let go of the branch; the follow-up's checkout holds it.
    assert not wt.exists() and (follow / "db.py").exists()
    assert _git(follow, "branch", "--show-current") == "swarm/pg-database-progdone"
    method, url, start = fake.calls[-1]
    assert url == "http://c/api/workflow" and start["correlationId"] == "continue:prog-done"
    assert start["input"]["repoPath"] == str(follow)
    assert start["input"]["description"].startswith("Follow-up to an earlier swarm run")
    assert start["input"]["description"].endswith("Add docs for the wire protocol")
    assert start["input"]["branchName"] == "swarm/docs-wire-protocol"
    # A running run cannot be continued yet; the reason is said.
    fake.runs["prog-run"] = {**done, "workflowId": "prog-run", "status": "RUNNING"}
    out = console.launch({"workflow": "cc_program", "continueFrom": "prog-run",
                          "values": {"repoPath": str(repo), "description": "more"}}, author="h", opener=fake)
    assert out["ok"] is False and "still working" in out["error"]
    # The follow-up's result names the run it continues and merges into the chain's first base.
    nxt = {**done, "workflowId": "prog-next", "correlationId": "continue:prog-done", "output": {"sourceHandoff": {
        "sourceRepoPath": str(follow), "branch": "swarm/docs-wire-protocol-prognext",
        "originalBranch": "swarm/pg-database-progdone"}}}
    _git(follow, "branch", "swarm/docs-wire-protocol-prognext")
    r = console.run_result(nxt, opener=fake, server_url="http://c/api")
    assert r["base"] == "master" and [x["id"] for x in r["lineage"]] == ["prog-done"]
    assert console._run_row(nxt)["continues"] == "prog-done"


def test_ship_merges_locally_or_opens_a_pull_request(monkeypatch, tmp_path):
    repo, wt, done = _delivered_repo(tmp_path)
    fake = _FakeConductor({"prog-done": done}, start_id="ship-1")
    assert console.ship_run("prog-done", "pr", author="h", opener=fake)["ok"] is False  # no remote
    out = console.ship_run("prog-done", "merge", author="human:a@b", opener=fake, server_url="http://c/api")
    assert out["ok"] is True and out["fastForward"] is True and out["into"] == "master"
    assert (repo / "db.py").exists()  # master's checkout moved forward with the work
    r = console.run_result(done, opener=fake, server_url="http://c/api")
    assert r["merged"] is True and r["actions"]["merge"] is False
    assert console.ship_run("prog-done", "merge", author="h", opener=fake)["ok"] is False

    repo2, _, done2 = _delivered_repo(tmp_path / "two")
    _git(repo2, "remote", "add", "origin", "git@github.com:acme/pg.git")
    fake = _FakeConductor({"prog-done": done2}, start_id="ship-2")
    out = console.ship_run("prog-done", "pr", author="human:a@b", opener=fake, server_url="http://c/api")
    assert out == {"ok": True, "how": "pr", "run": "ship-2", "author": "human:a@b"}
    method, url, start = fake.calls[-1]
    assert url == "http://c/api/workflow" and start["name"] == "ship_run"
    assert start["correlationId"] == "ship:prog-done"
    head = _git(repo2, "rev-parse", "swarm/pg-database-progdone")
    assert start["input"] == {"repoPath": str(repo2), "branch": "swarm/pg-database-progdone", "head": head, "base": "master",
                              "title": "Build a pg database", "summary": "Built the database.", "remote": "origin"}
    # A dirty target checkout is never merged into: nothing changes and the reason is said.
    repo3, _, done3 = _delivered_repo(tmp_path / "three")
    (repo3 / "README.md").write_text("local edit\n")
    out = console.ship_run("prog-done", "merge", author="h", opener=_FakeConductor({"prog-done": done3}))
    assert out["ok"] is False and "uncommitted changes" in out["error"]
    assert console.handle_post("/api/ship", b'{"run":"prog-done","how":"merge"}', None)[0] == 401


def test_settings_edit_the_model_policy_every_launch_uses(monkeypatch, tmp_path):
    """The model policy edited in the console is the TUI's /models file, validated by the workers' own
    rules, and a console launch carries it (it used to start runs with no policy at all)."""
    v = console.settings_view()
    pol = v["models"]
    assert pol["path"].endswith("model-profiles/models.json") and pol["problems"] == []
    assert "code" in v["roles"] and "claude" in v["backends"] and "claude-opus-4-8" in v["knownModels"]
    from tui import model_profiles
    data = model_profiles.set_role(pol["data"], "anthropic-standard", "code", 1, agent="claude", model="claude-opus-5-5")
    # A backend that cannot run the model is refused with the workers' reason; nothing is written.
    bad = model_profiles.set_role(data, "anthropic-standard", "plan", None, agent="codex", model="claude-opus-4-8")
    out = console.save_models({"data": bad, "sha256": pol["sha256"]})
    assert out["ok"] is False and "mismatch" in out["error"]
    assert console.save_models({"data": {**data, "defaultProfile": "nope"}, "sha256": pol["sha256"]})["ok"] is False
    out = console.save_models({"data": data, "sha256": pol["sha256"]})
    assert out["ok"] is True
    # A save over a policy that changed since it was loaded is refused rather than lost.
    stale = console.save_models({"data": data, "sha256": pol["sha256"]})
    assert stale["ok"] is False and "changed since you opened it" in stale["error"]
    s, _, b = console.handle_post("/api/models", json.dumps({"data": data}).encode(), None)
    assert s == 401  # signed writes only
    fake = _FakeConductor()
    out = console.launch({"workflow": "cc_program", "values": {"repoPath": str(tmp_path), "description": "x y"}},
                         author="h", opener=fake, server_url="http://c/api")
    sent = fake.calls[-1][2]["input"]
    assert out["ok"] and sent["modelProfile"] == "anthropic-standard"
    assert sent["modelPolicy"]["profiles"]["anthropic-standard"]["roles"]["code"]["tiers"][1]["model"] == "claude-opus-5-5"
    assert sent["modelPolicySource"].startswith("user:")


def test_settings_create_edit_and_delete_prompt_templates():
    v = console.settings_view()
    assert v["templates"] == [] and v["templateTargets"]["pr_review"]["primary"] == "reviewPromptTemplate"
    seed = json.loads(console.handle_get("/api/template-default", {"workflow": ["pr_review"],
                                                                   "field": ["reviewPromptTemplate"]})[2])["body"]
    assert len(seed) > 100  # the shipped default prompt to start from
    out = console.save_template({"name": "Security review", "description": "authz first", "body": "Look for authz.",
                                 "workflows": ["pr_review"], "fields": ["reviewPromptTemplate"]})
    assert out["ok"] and out["path"].endswith("security-review.md")
    again = console.save_template({"name": "Security review", "body": "x"})
    assert again["ok"] is False and "already exists" in again["error"]
    out = console.save_template({"path": out["path"], "name": "Security review", "body": "Look for authz and secrets.",
                                 "workflows": ["pr_review"], "repos": ["acme/app"]})
    t = console.settings_view()["templates"][0]
    assert (t["body"], t["repos"], t["description"]) == ("Look for authz and secrets.", ["acme/app"], "")
    assert console.save_template({"name": "x", "body": "y", "workflows": ["cc_program"]})["ok"] is False
    assert console.save_template({"action": "delete", "path": "/etc/passwd"})["ok"] is False
    assert console.save_template({"action": "delete", "path": t["path"]})["ok"] is True
    assert console.settings_view()["templates"] == []


def test_settings_list_every_built_in_prompt_and_where_it_runs():
    b = {x["key"]: x for x in console.settings_view()["builtins"]}
    assert "cc_program" in b["program_plan"]["usedBy"] and "team_build" in b["team_review"]["usedBy"]
    assert b["team_review"]["repoOverride"] == ".conductor/team_review.md" and b["team_review"]["copyable"] is False
    assert b["pr_review"]["copyable"] is True and "senior code reviewer" in b["pr_review"]["body"]
    assert all(x["body"].strip() for x in b.values()) and all(x["usedBy"] for x in b.values())
