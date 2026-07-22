"""Unit tests for the OpenSpec-CLI-backed worker tasks and the tasks.md parser.

The subprocess boundary (``common.openspec_cli.run``) is mocked — no real ``openspec``
binary required — following the same pattern as ``test_github.py``'s ``RecordingRun``.
``common/tasks_md.py`` is pure Python and tested directly against sample markdown.
"""

from __future__ import annotations

import json

import pytest

from common import openspec_cli
from common.tasks_md import TasksMdError, parse_tasks_md
from openspecops.tasks import (
    openspec_instructions,
    openspec_new_change,
    openspec_status,
    openspec_tasks_to_subtasks,
)


class RecordingRun:
    """Drop-in for ``common.openspec_cli.run``: records each argv and returns
    queued RunResult-shaped stdout in order."""

    def __init__(self, *stdouts):
        self.calls: list[list[str]] = []
        self._stdouts = list(stdouts)

    def __call__(self, cmd, cwd=None, check=True):
        self.calls.append(cmd)
        stdout = self._stdouts.pop(0) if self._stdouts else "{}"

        class _Result:
            pass

        r = _Result()
        r.stdout = stdout
        return r


def _completed(result) -> bool:
    return result.status.value == "COMPLETED"


def _failed(result) -> bool:
    return result.status.value == "FAILED"


# --- openspec_new_change ------------------------------------------------------

def test_openspec_new_change_runs_cli_and_seeds_rule(monkeypatch, fake_task_input, tmp_path):
    (tmp_path / "openspec").mkdir()
    (tmp_path / "openspec" / "config.yaml").write_text("schema: spec-driven\n")
    rec = RecordingRun(json.dumps({
        "change": {"id": "add-x", "path": str(tmp_path / "openspec/changes/add-x"), "schema": "spec-driven"}
    }))
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), name="add-x", description="Add X")
    result = openspec_new_change(task)
    assert _completed(result)
    assert result.output_data["changeName"] == "add-x"
    assert result.output_data["tasksRuleSeeded"] is True
    assert rec.calls[0][:3] == ["openspec", "new", "change"]
    import yaml
    cfg = yaml.safe_load((tmp_path / "openspec" / "config.yaml").read_text())
    assert cfg["rules"]["tasks"] == [openspec_cli.TASKS_RULE]


def test_openspec_new_change_rerun_does_not_reseed_rule(monkeypatch, fake_task_input, tmp_path):
    (tmp_path / "openspec").mkdir()
    (tmp_path / "openspec" / "config.yaml").write_text("schema: spec-driven\n")
    rec = RecordingRun(
        json.dumps({"change": {"id": "add-x", "path": "p", "schema": "spec-driven"}}),
        json.dumps({"change": {"id": "add-x", "path": "p", "schema": "spec-driven"}}),
    )
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), name="add-x")
    openspec_new_change(task)
    result = openspec_new_change(task)
    assert _completed(result)
    assert result.output_data["tasksRuleSeeded"] is False


def test_openspec_new_change_fails_closed_on_bad_json(monkeypatch, fake_task_input, tmp_path):
    rec = RecordingRun("not json")
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), name="add-x")
    result = openspec_new_change(task)
    assert _failed(result)


# --- openspec_status / openspec_instructions ---------------------------------

def test_openspec_status_returns_parsed_json(monkeypatch, fake_task_input, tmp_path):
    payload = {"changeName": "add-x", "applyRequires": ["tasks"],
               "artifacts": [{"id": "proposal", "status": "ready"}]}
    rec = RecordingRun(json.dumps(payload))
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), changeName="add-x")
    result = openspec_status(task)
    assert _completed(result)
    assert result.output_data == payload
    assert rec.calls[0] == ["openspec", "status", "--change", "add-x", "--json"]


def test_openspec_instructions_returns_parsed_json(monkeypatch, fake_task_input, tmp_path):
    payload = {"artifactId": "proposal", "instruction": "Create it.", "template": "## Why",
               "resolvedOutputPath": "/tmp/x/proposal.md", "rules": ["keep it short"]}
    rec = RecordingRun(json.dumps(payload))
    monkeypatch.setattr(openspec_cli, "run", rec)
    task = fake_task_input(repoPath=str(tmp_path), changeName="add-x", artifact="proposal")
    result = openspec_instructions(task)
    assert _completed(result)
    assert result.output_data == payload
    assert rec.calls[0] == ["openspec", "instructions", "proposal", "--change", "add-x", "--json"]


# --- openspec_tasks_to_subtasks (worker wrapping common/tasks_md.py) --------

def test_openspec_tasks_to_subtasks_parses_file(fake_task_input, tmp_path):
    tasks_md = tmp_path / "tasks.md"
    tasks_md.write_text(
        "## 1. Setup\n\nFiles: a.py\nTest: pytest tests/test_a.py\n\n- [ ] 1.1 Do it\n"
    )
    task = fake_task_input(tasksPath=str(tasks_md))
    result = openspec_tasks_to_subtasks(task)
    assert _completed(result)
    assert result.output_data["subtasks"] == [
        {"id": "setup", "description": "1.1 Do it", "files": ["a.py"], "testCmd": "pytest tests/test_a.py"}
    ]


def test_openspec_tasks_to_subtasks_requires_path(fake_task_input):
    task = fake_task_input(tasksPath="")
    result = openspec_tasks_to_subtasks(task)
    assert _failed(result)


def test_openspec_tasks_to_subtasks_fails_closed_on_missing_file(fake_task_input, tmp_path):
    task = fake_task_input(tasksPath=str(tmp_path / "nope.md"))
    result = openspec_tasks_to_subtasks(task)
    assert _failed(result)


# --- parse_tasks_md (pure function) ------------------------------------------

def test_parse_tasks_md_splits_independent_groups():
    text = (
        "## 1. Setup\n\nFiles: a.py, b.py\nTest: pytest tests/test_setup.py\n\n"
        "- [ ] 1.1 Create module\n- [ ] 1.2 Add deps\n\n"
        "## 2. Core\n\nFiles: c.py\nTest: pytest tests/test_core.py\n\n"
        "- [ ] 2.1 Implement thing\n"
    )
    subtasks = parse_tasks_md(text)
    assert subtasks == [
        {"id": "setup", "description": "1.1 Create module\n1.2 Add deps",
         "files": ["a.py", "b.py"], "testCmd": "pytest tests/test_setup.py"},
        {"id": "core", "description": "2.1 Implement thing",
         "files": ["c.py"], "testCmd": "pytest tests/test_core.py"},
    ]


def test_parse_tasks_md_fails_closed_on_overlapping_files():
    text = (
        "## 1. Setup\n\nFiles: a.py\nTest: pytest tests/test_setup.py\n\n- [ ] 1.1 X\n\n"
        "## 2. Overlap\n\nFiles: a.py\nTest: pytest tests/test_overlap.py\n\n- [ ] 2.1 Y\n"
    )
    with pytest.raises(TasksMdError, match="file-disjoint"):
        parse_tasks_md(text)


def test_parse_tasks_md_fails_closed_on_missing_files_line():
    text = "## 1. Setup\n\nTest: pytest tests/test_setup.py\n\n- [ ] 1.1 X\n"
    with pytest.raises(TasksMdError, match="Files:"):
        parse_tasks_md(text)


def test_parse_tasks_md_fails_closed_on_missing_test_line():
    text = "## 1. Setup\n\nFiles: a.py\n\n- [ ] 1.1 X\n"
    with pytest.raises(TasksMdError, match="Test:"):
        parse_tasks_md(text)


def test_parse_tasks_md_dedupes_slug_collisions():
    text = (
        "## 1. Setup\n\nFiles: a.py\nTest: pytest a\n\n- [ ] 1.1 X\n\n"
        "## 2. Setup\n\nFiles: b.py\nTest: pytest b\n\n- [ ] 2.1 Y\n"
    )
    subtasks = parse_tasks_md(text)
    assert [s["id"] for s in subtasks] == ["setup", "setup-2"]
