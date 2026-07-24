from __future__ import annotations

import json
from pathlib import Path

import pytest

from campaign.checks import ChecksConfigError, load_config, run_profile
from campaign.model import (aggregate_usage, paths_overlap, select_wave,
                            validate_checkpoint, validate_plan)
from campaign.tasks import campaign_schedule
from common import git


def _task(ident, *, deps=None, files=None):
    return {"id": ident, "description": f"implement {ident}", "dependsOn": deps or [],
            "files": files or [f"src/{ident}.py"],
            "acceptanceCriteria": [f"{ident} works"], "checks": ["unit"]}


def test_dag_validation_and_topological_order():
    result = validate_plan({"tasks": [_task("api"), _task("ui", deps=["api"])]})
    assert result["valid"] is True
    assert result["order"] == ["api", "ui"]


def test_dag_rejects_cycles_missing_dependencies_and_escapes():
    result = validate_plan({"tasks": [
        _task("a", deps=["b"], files=["../escape"]),
        _task("b", deps=["a"]),
        _task("c", deps=["missing"]),
    ]})
    assert result["valid"] is False
    text = "\n".join(result["errors"])
    assert "cycle" in text and "missing dependencies" in text and "escapes" in text


def test_file_disjoint_wave_scheduling_and_blocked_tasks():
    tasks = [_task("a", files=["src/core"]), _task("b", files=["src/core/x.py"]),
             _task("c", files=["docs/readme.md"]), _task("d", deps=["a"])]
    first = select_wave(tasks, max_parallelism=6)
    assert first["readyIds"] == ["a", "c"]
    second = select_wave(tasks, completed=["a", "c"], blocked=["b"])
    assert second["readyIds"] == ["d"]
    assert paths_overlap(["src/*"], ["src/app.py"])


def test_mock_campaign_unlocks_dependent_tasks_across_corrective_waves():
    tasks = [_task("schema", files=["db/schema.sql"]),
             _task("api", deps=["schema"], files=["src/api.py"]),
             _task("docs", files=["docs/feature.md"])]
    wave1 = select_wave(tasks, max_parallelism=2)
    assert wave1["readyIds"] == ["schema", "docs"]
    # docs failed its first burst; schema integrated and unlocks api.
    wave2 = select_wave(tasks, completed=["schema"], max_parallelism=2)
    assert wave2["readyIds"] == ["api", "docs"]
    assert select_wave(tasks, completed=["schema", "api", "docs"])["done"] is True


def test_scheduler_forwards_resume_session_feedback_and_limits(fake_task_input):
    plan = {"tasks": [_task("api")]}
    task = fake_task_input(repoPath="/tmp/repo", plan=plan, wave=3,
                           sessions={"api": "session-123"}, feedback="fix retry",
                           maxTurns=900, maxBudgetUsd=75, maxParallelism=6,
                           specContextPath="/tmp/conductor-openspec/wf/context.md")
    result = campaign_schedule(task).output_data
    ref = "wave_3_api"
    assert result["dynamicTasksInput"][ref]["resumeSessionId"] == "session-123"
    assert result["dynamicTasksInput"][ref]["feedback"] == "fix retry"
    assert result["dynamicTasksInput"][ref]["maxTurns"] == 900
    assert result["dynamicTasksInput"][ref]["specContextPath"].endswith("context.md")


def test_campaign_worktree_resume_keeps_same_path_and_uncommitted_edits(tmp_git_repo):
    first = git.worktree_add(str(tmp_git_repo), "campaign-w1-api", preserve_existing=True)
    marker = Path(first["worktreePath"]) / "resume.txt"
    marker.write_text("unfinished session")
    second = git.worktree_add(str(tmp_git_repo), "campaign-w1-api", preserve_existing=True)
    assert second["resumed"] is True
    assert second["worktreePath"] == first["worktreePath"]
    assert marker.read_text() == "unfinished session"


def test_checkpoint_actions_block_continue_and_validate_specific_checks():
    blocked = validate_checkpoint({"action": "continue"}, phase="wave", blocking_passed=False)
    assert blocked["valid"] is False and blocked["action"] == "revise"
    revise = validate_checkpoint({"action": "revise", "feedback": "fix API"}, phase="plan")
    assert revise["valid"] is True
    bad_check = validate_checkpoint({"action": "run_checks", "profile": "wave",
                                     "checks": ["other"]}, allowed_checks=["unit"])
    assert bad_check["valid"] is False
    assert validate_checkpoint({"action": "stop"})["outcome"] == "incomplete"


def test_usage_aggregation_preserves_sessions_without_campaign_cap():
    out = aggregate_usage([{"tokenUsed": 10, "costUsd": 1.2, "sessionId": "a"},
                           {"totalTokens": 5, "totalCostUsd": 0.3, "sessionId": "b"}])
    assert out == {"totalTokens": 15, "totalCostUsd": 1.5, "sessions": ["a", "b"]}


def _write_config(repo: Path, data: dict) -> None:
    path = repo / ".conductor-code" / "checks.json"
    path.parent.mkdir()
    path.write_text(json.dumps(data))


def _config(*, environment=None):
    return {"version": 2, "checks": {
        "unit": {"command": ["sh", "-c", "echo unit"], "blocking": True},
        "lint": {"command": ["sh", "-c", "echo lint; exit 1"], "blocking": False},
    }, "profiles": {"wave": {"checks": ["unit", "lint"],
                                "environment": environment or {"mode": "none"}}},
            "defaults": {"waveProfile": "wave", "finalProfile": "wave"}}


def test_checks_advisory_failure_does_not_block(tmp_path):
    _write_config(tmp_path, _config())
    result = run_profile(str(tmp_path), "wave")
    assert result["passed"] is False
    assert result["blockingPassed"] is True
    assert result["checks"][1]["exitCode"] == 1
    assert Path(result["checks"][0]["logPath"]).is_file()


def test_specific_checks_must_belong_to_profile(tmp_path):
    _write_config(tmp_path, _config())
    with pytest.raises(ChecksConfigError, match="not in profile"):
        run_profile(str(tmp_path), "wave", requested=["missing"])


def test_attached_requires_fresh_confirmation_and_never_tears_down(tmp_path, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_TEST_URL", raising=False)
    cfg = _config(environment={"mode": "attached", "readyCheck": ["sh", "-c", "exit 0"],
                               "requiredEnv": ["CAMPAIGN_TEST_URL"]})
    _write_config(tmp_path, cfg)
    missing = run_profile(str(tmp_path), "wave", attached_confirmed=True)
    assert missing["missingEnvironmentVariables"] == ["CAMPAIGN_TEST_URL"]
    monkeypatch.setenv("CAMPAIGN_TEST_URL", "http://127.0.0.1:9999")
    confirm = run_profile(str(tmp_path), "wave", attached_confirmed=False)
    assert confirm["confirmationRequired"] is True and confirm["teardownRan"] is False
    actual = run_profile(str(tmp_path), "wave", requested=["unit"], attached_confirmed=True)
    assert actual["blockingPassed"] is True and actual["teardownRan"] is False


def test_managed_teardown_runs_even_when_setup_fails(tmp_path):
    down_marker = tmp_path / "down-ran"
    cfg = _config(environment={
        "mode": "managed",
        "up": ["sh", "-c", "exit 7"],
        "readyCheck": ["sh", "-c", "exit 0"],
        "down": ["sh", "-c", f"touch {down_marker}"],
    })
    _write_config(tmp_path, cfg)
    result = run_profile(str(tmp_path), "wave")
    assert result["blockingPassed"] is False
    assert result["teardownRan"] is True and down_marker.exists()


def test_check_retries_are_reported(tmp_path):
    marker = tmp_path / "retry-marker"
    cfg = _config()
    cfg["checks"]["flaky"] = {
        "command": ["sh", "-c", f"if test -f {marker}; then exit 0; else touch {marker}; exit 1; fi"],
        "blocking": True, "retries": 1,
    }
    cfg["profiles"]["wave"]["checks"] = ["flaky"]
    _write_config(tmp_path, cfg)
    result = run_profile(str(tmp_path), "wave")
    assert result["blockingPassed"] is True
    assert len(result["checks"][0]["attempts"]) == 2


def test_config_rejects_attached_teardown(tmp_path):
    cfg = _config(environment={"mode": "attached", "down": "docker compose down"})
    _write_config(tmp_path, cfg)
    with pytest.raises(ChecksConfigError, match="must never define teardown"):
        load_config(str(tmp_path))


def test_config_rejects_inline_environment_secrets(tmp_path):
    cfg = _config()
    cfg["checks"]["unit"]["env"] = {"TOKEN": "plaintext"}
    _write_config(tmp_path, cfg)
    with pytest.raises(ChecksConfigError, match="credential values"):
        load_config(str(tmp_path))
