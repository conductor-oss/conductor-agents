"""Tests for the editor bridge (resolve_editor / open_path) and workspace resolution."""

from __future__ import annotations

import contextlib

import pytest

from tui import api, edit


# --------------------------------------------------------------------------- resolve_editor

def test_editor_precedence_override_first(monkeypatch):
    monkeypatch.setenv("VISUAL", "vim")
    monkeypatch.setenv("EDITOR", "nano")
    cmd, is_gui = edit.resolve_editor("code -n")
    assert cmd == ["code", "-n"] and is_gui is True


def test_editor_visual_over_editor(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_HARNESS_EDITOR", raising=False)
    monkeypatch.setenv("VISUAL", "vim")
    monkeypatch.setenv("EDITOR", "nano")
    cmd, is_gui = edit.resolve_editor(None)
    assert cmd == ["vim"] and is_gui is False   # terminal editor


def test_editor_gui_detected_when_no_env(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(edit.shutil, "which", lambda n: "/usr/bin/cursor" if n == "cursor" else None)
    cmd, is_gui = edit.resolve_editor(None)
    assert cmd == ["cursor"] and is_gui is True


def test_editor_os_open_fallback(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(edit.shutil, "which", lambda n: "/usr/bin/open" if n == "open" else None)
    monkeypatch.setattr(edit.sys, "platform", "darwin")
    cmd, is_gui = edit.resolve_editor(None)
    assert cmd == ["open"] and is_gui is True


# --------------------------------------------------------------------------- open_path

class _FakeApp:
    def __init__(self): self.suspended = False
    @contextlib.contextmanager
    def suspend(self):
        self.suspended = True
        yield


def test_open_path_gui_uses_popen(monkeypatch):
    calls = {}
    monkeypatch.setattr(edit, "resolve_editor", lambda o: (["code"], True))
    monkeypatch.setattr(edit.subprocess, "Popen", lambda argv, **kw: calls.setdefault("popen", argv))
    monkeypatch.setattr(edit.subprocess, "run", lambda *a, **k: calls.setdefault("run", a))
    app = _FakeApp()
    msg = edit.open_path(app, "/tmp/proj")
    assert calls["popen"] == ["code", "/tmp/proj"]
    assert "run" not in calls and app.suspended is False
    assert "code" in msg


def test_open_path_terminal_uses_suspend(monkeypatch):
    calls = {}
    monkeypatch.setattr(edit, "resolve_editor", lambda o: (["vim"], False))
    monkeypatch.setattr(edit.subprocess, "run", lambda argv, **kw: calls.setdefault("run", argv))
    app = _FakeApp()
    edit.open_path(app, "/tmp/proj")
    assert app.suspended is True
    assert calls["run"] == ["vim", "/tmp/proj"]


# --------------------------------------------------------------------------- workspace_path

def test_workspace_from_code_parallel_input():
    run = api.Run(id="w", workflow="code_parallel", status="RUNNING", start_ms=0, end_ms=None,
                  input={"repoPath": "/home/me/proj"})
    assert api.workspace_path(run) == "/home/me/proj"


def test_workspace_from_output():
    run = api.Run(id="w", workflow="issue_to_pr", status="COMPLETED", start_ms=0, end_ms=1,
                  output={"repoPath": "/tmp/issue_3_x"})
    assert api.workspace_path(run) == "/tmp/issue_3_x"


def test_workspace_from_git_clone_task():
    run = api.Run(id="w", workflow="pr_review", status="COMPLETED", start_ms=0, end_ms=1)
    tasks = [api.TaskNode(ref="clone", def_name="git_clone", type="SIMPLE", status="COMPLETED",
                          task_id="t", output={"repoPath": "/tmp/review_7_x"})]
    assert api.workspace_path(run, tasks) == "/tmp/review_7_x"


def test_workspace_none_when_unresolvable():
    run = api.Run(id="w", workflow="pr_review", status="COMPLETED", start_ms=0, end_ms=1)
    assert api.workspace_path(run, []) is None


# --------------------------------------------------------------------------- file_changes

def _agent(output):
    return api.TaskNode(ref="code", def_name="coding_agent", type="SIMPLE",
                        status="COMPLETED", task_id="t", output=output)


def test_file_changes_aggregates_and_dedups():
    run = api.Run(id="w", workflow="code_parallel", status="COMPLETED", start_ms=0, end_ms=1)
    t1 = _agent({"fileChanges": [{"path": "a.py", "status": "A"}, {"path": "b.py", "status": "M"}]})
    t2 = _agent({"fileChanges": [{"path": "b.py", "status": "M"}, {"path": "c.py", "status": "D"}]})
    d = api.RunDetail(run=run, tasks=[t1, t2])
    assert d.file_changes() == [("A", "a.py"), ("M", "b.py"), ("D", "c.py")]  # sorted by path


def test_file_changes_legacy_fallback_and_real_status_wins():
    run = api.Run(id="w", workflow="code_parallel", status="COMPLETED", start_ms=0, end_ms=1)
    legacy = _agent({"filesChanged": ["x.py", "y.py"]})          # no fileChanges → "•"
    modern = _agent({"fileChanges": [{"path": "x.py", "status": "A"}]})
    d = api.RunDetail(run=run, tasks=[legacy, modern])
    changes = dict((p, s) for s, p in d.file_changes())
    assert changes["x.py"] == "A"      # real status beats "•"
    assert changes["y.py"] == "•"


def test_file_changes_pr_review_changedfiles_fallback():
    run = api.Run(id="w", workflow="pr_review", status="COMPLETED", start_ms=0, end_ms=1,
                  output={"changedFiles": ["m.go", "n.go"]})
    d = api.RunDetail(run=run, tasks=[])
    assert d.file_changes() == [("•", "m.go"), ("•", "n.go")]


def test_files_section_renders_and_caps():
    from tui.widgets.result_card import files_section
    changes = [("A", f"f{i}.py") for i in range(20)]
    text = files_section(changes, max_rows=15).plain
    assert "f0.py" in text and "f14.py" in text and "f15.py" not in text
    assert "+5 more" in text
