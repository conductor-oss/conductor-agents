"""Phase 2 construction read model: the tracking / learning / review boards folded from the log.

These cover the pure fold + standup/sizing/termination logic in ``common/team_board.py`` — the part
that turns append-only facts into a team's shared kanban. The thin ``@worker_task`` wrappers over
this module (the team cycle) have their own workflow-shape tests in ``test_team_cycle.py``.
"""
import common  # noqa: F401 -- sys.path shim for swarm_substrate

from common import team_board as tb


def _isolate_board(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))


# ─────────────────────────── fold: latest state per item wins ──────────────────
def test_fold_tracks_latest_state_and_durable_fields(monkeypatch, tmp_path):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-fold"
    tb.post_item(cid, "lead", {"id": "a", "title": "Add A", "files": ["x.py"],
                               "dependsOn": [], "priority": 5})
    tb.post_item(cid, "lead", {"id": "b", "title": "Add B", "dependsOn": ["a"]})
    tb.set_state(cid, "lead", "a", tb.READY)
    tb.set_state(cid, "impl-1", "a", tb.IN_PROGRESS, assignee="impl-1", attempt=1)
    tb.set_state(cid, "impl-1", "a", tb.DONE)

    board = tb.fold_board(cid)
    items = board["items"]
    assert items["a"]["state"] == tb.DONE and items["a"]["assignee"] == "impl-1"
    assert items["a"]["attempt"] == 1 and items["a"]["files"] == ["x.py"] and items["a"]["priority"] == 5
    # b never transitioned -> backlog, durable deps preserved.
    assert items["b"]["state"] == tb.BACKLOG and items["b"]["dependsOn"] == ["a"]


# ─────────────────────────── standup: dependency-ready only ────────────────────
def test_ready_items_respects_dependencies_and_priority(monkeypatch, tmp_path):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-ready"
    tb.post_item(cid, "lead", {"id": "a", "priority": 1})
    tb.post_item(cid, "lead", {"id": "b", "priority": 9})
    tb.post_item(cid, "lead", {"id": "c", "dependsOn": ["a"], "priority": 5})
    board = tb.fold_board(cid)
    ready = tb.ready_items(board["items"])
    # a and b are ready (no deps); c is blocked on a. Higher priority first.
    assert [it["id"] for it in ready] == ["b", "a"]

    # Finish a -> c becomes ready.
    tb.set_state(cid, "impl", "a", tb.DONE)
    board = tb.fold_board(cid)
    assert {it["id"] for it in tb.ready_items(board["items"])} == {"b", "c"}


def test_reopened_item_is_dispatchable_again(monkeypatch, tmp_path):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-reopen"
    tb.post_item(cid, "lead", {"id": "a"})
    tb.set_state(cid, "impl", "a", tb.IN_PROGRESS)
    tb.set_state(cid, "reviewer", "a", tb.REOPENED, reason="review requested changes")
    board = tb.fold_board(cid)
    assert [it["id"] for it in tb.ready_items(board["items"])] == ["a"]


# ─────────────────────────── sizing: the concrete formula ──────────────────────
def test_size_wave_fills_slots_and_throttles_on_budget():
    # ok: team size = min(ready, free slots).
    assert tb.size_wave(5, max_width=3, budget_state="ok") == 3
    assert tb.size_wave(2, max_width=3, budget_state="ok") == 2
    # in-flight consumes slots.
    assert tb.size_wave(5, max_width=3, budget_state="ok", in_flight=2) == 1
    # warn halves the free slots but never starves (>=1 while any slot is free).
    assert tb.size_wave(5, max_width=4, budget_state="warn") == 2
    assert tb.size_wave(5, max_width=1, budget_state="warn") == 1
    # halt dispatches nothing; let in-flight drain.
    assert tb.size_wave(5, max_width=3, budget_state="halt") == 0
    # no free slots -> nothing.
    assert tb.size_wave(5, max_width=3, budget_state="ok", in_flight=3) == 0


# ─────────────────────────── termination: done vs drained ──────────────────────
def test_all_done_and_is_drained_distinguish_green_from_blocked(monkeypatch, tmp_path):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-term"
    tb.post_item(cid, "lead", {"id": "a"})
    tb.post_item(cid, "lead", {"id": "b"})
    tb.set_state(cid, "i", "a", tb.DONE)
    tb.set_state(cid, "i", "b", tb.IN_PROGRESS)
    items = tb.fold_board(cid)["items"]
    assert not tb.all_done(items) and not tb.is_drained(items)

    tb.set_state(cid, "i", "b", tb.BLOCKED, reason="dependency missing")
    items = tb.fold_board(cid)["items"]
    # Drained (nothing open) but NOT all done -> an honest stop, not convergence.
    assert tb.is_drained(items) and not tb.all_done(items)

    tb.set_state(cid, "i", "b", tb.DONE)
    items = tb.fold_board(cid)["items"]
    assert tb.all_done(items) and tb.is_drained(items)


# ─────────────────────────── learning + review boards fold ─────────────────────
def test_lessons_reviews_directives_fold_with_provenance(monkeypatch, tmp_path):
    _isolate_board(monkeypatch, tmp_path)
    cid = "run-boards"
    tb.post_item(cid, "lead", {"id": "a"})
    tb.post_lesson(cid, "impl-1", "registry.py uses append-only markers", confidence="high",
                   provenance="observed", refs=["registry.py"])
    tb.post_review(cid, "rev-1", "a", "changes", findings=["missing acceptance criterion 2"])
    tb.post_review(cid, "rev-2", "a", "lgtm")
    tb.post_directive(cid, "human", "a", "do not use library X", authority="human")

    board = tb.fold_board(cid)
    assert board["lessons"][0]["confidence"] == "high" and board["lessons"][0]["refs"] == ["registry.py"]
    assert {r["verdict"] for r in board["reviews"]} == {"changes", "lgtm"}
    assert board["directives"][0]["authority"] == "human" and board["directives"][0]["sticky"] is True

    rollup = tb.board_rollup(board)
    assert rollup["total"] == 1 and rollup["lessons"] == 1 and rollup["openReviews"] == 1
    assert rollup["directives"] == 1 and rollup["counts"].get(tb.BACKLOG) == 1


def test_board_accepts_criteria_written_as_text(monkeypatch, tmp_path):
    """A planner wrote every item's acceptance criteria as one paragraph; the board kept [] for a
    non-list and no team of that run had a frozen acceptance test (run 90e5e3aa). Text is criteria."""
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import team_board as tb
    from common.shapes import lines
    tb.post_item("run-lines", "lead", {"id": "x", "files": ["a.go"],
                                       "acceptanceCriteria": "- Handlers never panic\n- Each validates its own arity\n",
                                       "reviewCriteria": "one paragraph, with commas"})
    it = tb.fold_board("run-lines")["items"]["x"]
    assert it["acceptanceCriteria"] == ["Handlers never panic", "Each validates its own arity"]
    assert it["reviewCriteria"] == ["one paragraph, with commas"]
    assert lines(["a", " ", "b"]) == ["a", "b"]
    assert lines("1. one\n2) two\n• three\n\n* four") == ["one", "two", "three", "four"]
    assert lines(None) == [] and lines("") == []
