from __future__ import annotations

from common import git
from openspec import tasks as openspec


def test_openspec_finalize_publishes_a_run_specific_archive_branch(
        monkeypatch, tmp_git_repo, fake_task_input):
    change_id = "add-greeting"
    change = tmp_git_repo / "openspec" / "changes" / change_id
    change.mkdir(parents=True)
    (change / "tasks.md").write_text("- [ ] Add greeting\n")

    monkeypatch.setattr(openspec, "_run", lambda *args, **kwargs: {
        "exitCode": 0, "data": {}, "stdout": "{}", "stderr": "",
    })
    result = openspec.openspec_finalize(fake_task_input(
        writebackRepoPath=str(tmp_git_repo),
        writebackProjectPath=str(tmp_git_repo),
        changeId=change_id,
        sameRepo=False,
        publish=True,
        branch="openspec/archive/add-greeting",
        branchRunId="abcdef12-3456-7890-abcd-ef1234567890",
    ))

    assert result.status.value == "COMPLETED"
    assert result.output_data["branch"] == "openspec/archive/add-greeting-abcdef12"
    assert git._current_branch(str(tmp_git_repo)) == result.output_data["branch"]
