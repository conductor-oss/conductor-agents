from __future__ import annotations

from pathlib import Path

from common import git
from revision.tasks import revision_checkpoint


def _completed(result) -> bool:
    return result.status.value == "COMPLETED"


def test_revision_checkpoint_accepts_an_abbreviated_expected_head(
        fake_task_input, tmp_git_repo: Path):
    repo = str(tmp_git_repo)
    worktree = tmp_git_repo / ".cc-worktrees" / "revision-short-head"
    git.git(repo, "worktree", "add", "-b", "revision-short-head", str(worktree))
    (worktree / "revision.py").write_text("revision = 1\n")
    abbreviated = git.git(str(worktree), "rev-parse", "--short", "HEAD").stdout.strip()

    result = revision_checkpoint(fake_task_input(
        action="save",
        worktreePath=str(worktree),
        workflowId="workflow-1",
        loopId="loop-1",
        candidateId="candidate-1",
        expectedHead=abbreviated,
    ))

    assert _completed(result)
    assert result.output_data["commit"] == git.head(str(worktree))
    assert len(result.output_data["commit"]) == 40
