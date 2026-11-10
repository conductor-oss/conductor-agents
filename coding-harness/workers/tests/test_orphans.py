"""A restart must not strand a task the previous worker generation was running (run 373cac75)."""
import pathlib

from common import orphans

HOST, BEFORE = "worker-1", 1000
ME = orphans.generation_identity(HOST, BEFORE)
LEFT = {"taskId": "t1", "taskDefName": "test_plan_resolve", "status": "IN_PROGRESS", "workerId": "worker-1/400",
        "startTime": 500, "referenceTaskName": "initial_plan"}


def test_only_a_task_an_earlier_generation_of_this_host_claimed_is_orphaned():
    pick = lambda task: orphans.orphaned({"tasks": [task]}, ["test_plan_resolve"], identity=ME, before_ms=BEFORE)  # noqa: E731
    assert pick(LEFT) == [LEFT]
    assert pick({**LEFT, "workerId": ME}) == []                       # claimed by this generation
    assert pick({**LEFT, "workerId": ME, "startTime": 1}) == []       # ...whatever the clocks say
    assert pick({**LEFT, "workerId": "worker-2/400"}) == []           # another host may still be running it
    assert pick({**LEFT, "workerId": "worker-10/400"}) == []          # a prefix is not this host
    assert pick({**LEFT, "taskDefName": "verify"}) == []              # polled by another fleet
    assert pick({**LEFT, "status": "COMPLETED"}) == []
    # A worker older than generation identities: judged by its start time.
    legacy = {**LEFT, "workerId": HOST}
    assert pick(legacy) == [legacy]
    assert pick({**legacy, "startTime": 1500}) == [] and pick({**legacy, "startTime": 0}) == []


def test_reclaim_requeues_without_spending_a_retry_and_survives_a_bad_workflow(monkeypatch):
    calls = []

    def fake(method, path, body=None, session=None):
        calls.append((method, path, body))
        if "search" in path:
            return {"results": [{"workflowId": "bad"}, {"workflowId": "w1"}]}
        if path.startswith("/workflow/bad"):
            raise ValueError("unreadable")
        if path.startswith("/workflow/w1"):
            return {"tasks": [LEFT, {**LEFT, "taskId": "t2", "workerId": ME}]}
        return "t1"

    monkeypatch.setattr(orphans, "_call", fake)
    monkeypatch.setattr(orphans, "_session", lambda: ("http://c/api", {}))
    assert orphans.reclaim(["test_plan_resolve"], identity=ME, before_ms=BEFORE) == ["t1"]
    assert calls[-1] == ("POST", "/tasks", {"workflowInstanceId": "w1", "taskId": "t1", "status": "IN_PROGRESS",
                                            "callbackAfterSeconds": 0, "workerId": ME})


def test_search_is_paged(monkeypatch):
    pages = [[{"workflowId": str(i)} for i in range(orphans._PAGE)], [{"workflowId": "last"}]]
    seen = []

    def fake(method, path, body=None, session=None):
        seen.append(path)
        return {"results": pages.pop(0) if pages else []}
    monkeypatch.setattr(orphans, "_call", fake)
    monkeypatch.setattr(orphans, "_session", lambda: ("http://c/api", {}))
    assert len(orphans._running_workflow_ids()) == orphans._PAGE + 1
    assert any("PAUSED" in p for p in seen)  # a paused run's tasks were claimed too


def test_every_generation_polls_under_its_identity_and_reclaims_after_its_pollers_start():
    source = (pathlib.Path(__file__).parent.parent / "main.py").read_text()
    ident, tag, start, sweep = (source.index("identity = orphans.generation_identity("),
                                source.index('record["worker_id"] = identity'),
                                source.index("handler.start_processes()"), source.index("orphans.reclaim_in_background"))
    assert ident < tag < start < sweep
