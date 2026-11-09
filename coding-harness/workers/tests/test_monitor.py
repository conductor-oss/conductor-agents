"""Phase 3 monitor: bounded-authority detection over the board, and the team_monitor worker.

The monitor NUDGES/re-prioritizes autonomously and ESCALATES anything bigger to the exception queue
for a human. These assert that classification (pure ``common.monitor.detect``) and that the worker
applies the autonomous actions and posts de-duplicated exceptions.
"""
import common  # noqa: F401 -- sys.path shim

from team.tasks import team_monitor
from common import monitor, team_board as tb


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "bb"))


def _board(items, *, reviews=None, owners=None, exceptions=None):
    return {"items": {it["id"]: {"state": tb.BACKLOG, "attempt": 0, "files": [], **it} for it in items},
            "reviews": reviews or [], "owners": owners or {}, "exceptions": exceptions or [],
            "lessons": [], "directives": []}


# ─────────────────────────── detect: autonomous vs escalate ────────────────────
def test_stall_is_autonomous_nudge_and_reprioritize():
    b = _board([{"id": "a", "state": tb.REOPENED, "attempt": 2}])
    out = monitor.detect(b, stall_attempts=2)
    ops = {x["op"] for x in out["autonomousActions"]}
    assert ops == {"nudge", "reprioritize"} and out["exceptions"] == []
    assert any(s["kind"] == "stall" for s in out["signals"])


def test_quality_regression_is_autonomous_directive():
    b = _board([{"id": "a", "state": tb.REVIEW}],
               reviews=[{"item": "a", "verdict": "changes"}, {"item": "a", "verdict": "changes"}])
    out = monitor.detect(b, quality_changes=2)
    assert any(x["op"] == "nudge" and x["item"] == "a" for x in out["autonomousActions"])
    assert out["exceptions"] == []


def test_blocked_item_escalates():
    b = _board([{"id": "a", "state": tb.BLOCKED, "reason": "rebase conflict"}])
    out = monitor.detect(b)
    assert any(e["kind"] == "blocked" and e["item"] == "a" for e in out["exceptions"])


def test_duplication_escalates():
    b = _board([{"id": "a", "state": tb.IN_PROGRESS, "files": ["x.py", "a.py"]},
                {"id": "b", "state": tb.IN_PROGRESS, "files": ["x.py", "b.py"]}])
    out = monitor.detect(b)
    dup = [e for e in out["exceptions"] if e["kind"] == "duplication"]
    assert dup and "x.py" in dup[0]["detail"]


def test_budget_warns_then_escalates():
    b = _board([{"id": "a"}])
    assert any(s["kind"] == "budget_warn" for s in monitor.detect(b, spend_usd=8.5, ceiling_usd=10)["signals"])
    assert any(e["kind"] == "budget" for e in monitor.detect(b, spend_usd=10.0, ceiling_usd=10)["exceptions"])


# ─────────────────────────── worker: apply + escalate + dedup ───────────────────
def test_team_monitor_corrects_autonomously_and_escalates(monkeypatch, tmp_path, fake_task_input):
    _isolate(monkeypatch, tmp_path)
    cid = "run-mon"
    tb.post_item(cid, "lead", {"id": "stuck", "files": ["x.py"]})
    tb.post_item(cid, "lead", {"id": "dead", "files": ["y.py"]})
    tb.set_state(cid, "lead", "stuck", tb.REOPENED, attempt=2)   # thrash -> autonomous
    tb.set_state(cid, "lead", "dead", tb.BLOCKED, reason="rebase conflict")  # -> escalate

    out = team_monitor(fake_task_input(correlationId=cid, ceilingUsd=10, stallAttempts=2)).output_data
    assert out["actionsApplied"] >= 2 and out["openExceptions"] == 1 and "needsHuman" not in out
    board = tb.fold_board(cid)
    # Autonomous: a monitor directive + a priority bump for the thrashing item.
    assert any(d["target"] == "stuck" and d["authority"] == "monitor" for d in board["directives"])
    assert board["items"]["stuck"]["priority"] >= 100
    # Escalation on the board.
    exc = tb.open_exceptions(board["exceptions"])
    assert exc[0]["item"] == "dead" and exc[0]["kind"] == "blocked"

    # Running again does NOT duplicate the open exception.
    out2 = team_monitor(fake_task_input(correlationId=cid, ceilingUsd=10, stallAttempts=2)).output_data
    assert out2["newExceptions"] == 0 and out2["openExceptions"] == 1


def test_team_monitor_exception_resolves(monkeypatch, tmp_path, fake_task_input):
    _isolate(monkeypatch, tmp_path)
    cid = "run-res"
    tb.post_item(cid, "lead", {"id": "dead", "files": ["y.py"]})
    tb.set_state(cid, "lead", "dead", tb.BLOCKED, reason="conflict")
    team_monitor(fake_task_input(correlationId=cid, ceilingUsd=10))
    assert len(tb.open_exceptions(tb.fold_board(cid)["exceptions"])) == 1
    tb.resolve_exception(cid, "human", "dead", "blocked", "re-planned by human")
    assert tb.open_exceptions(tb.fold_board(cid)["exceptions"]) == []
