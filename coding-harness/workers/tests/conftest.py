"""Shared scaffolding for coding-harness worker unit tests.

Mirrors ``security-harness/tests/conftest.py``: puts ``workers/`` on ``sys.path`` so
worker modules import bare (``from common import ...``, ``import gitops``,
``import coding_agent``) with no per-test path hacks.

Contract (see CONTEXT.md): pure-logic unit tests — no live Conductor server, no
network, no real LLM, no ``gh``. The only real subprocess allowed is ``git`` against
throwaway repos created under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
import sys

import pytest

# --- import path setup (mirror security-harness/tests/conftest.py) ----------
# conftest lives at coding-harness/workers/tests/ ; parents[1] is coding-harness/workers/.
WORKERS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKERS))

REPO = WORKERS.parent  # coding-harness/
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- git helper -------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real ``git`` command in ``repo``, failing loudly on error."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with a single seed commit, for gitops tests.

    Real ``git``, no network. Local identity is configured and commit signing is
    disabled so the repo is usable regardless of the developer's global git config.
    Returns the repo path (``<repo>/.git`` exists).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # -c init.defaultBranch=main keeps the seed branch deterministic across git versions.
    _git(repo, "-c", "init.defaultBranch=main", "init")
    _git(repo, "config", "user.name", "Conductor Test")
    _git(repo, "config", "user.email", "test@conductor.local")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# tmp git repo\n\nSeed content for gitops tests.\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "seed commit")
    return repo


@pytest.fixture
def fake_task_input():
    """Factory building the ``Task`` object a Conductor ``@worker_task`` receives.

    Worker task functions take a single ``task`` argument and read ``task.input_data``;
    on success/failure they call ``task.to_task_result(...)`` (see common/results.py),
    which needs the id fields set. This returns a real conductor ``Task`` with those
    fields populated, so task functions can be invoked directly in a test::

        def test_commit(fake_task_input, tmp_git_repo):
            task = fake_task_input(repoPath=str(tmp_git_repo), message="wip")
            result = commit(task)            # returns a real TaskResult
            assert result.output_data["commit"]

    Pass the input either as a dict, as keyword args, or both (kwargs win).
    """
    from conductor.client.http.models.task import Task

    def _make(input_data: dict | None = None, **kwargs) -> Task:
        data = dict(input_data or {})
        data.update(kwargs)
        return Task(
            input_data=data,
            task_id="test-task-id",
            workflow_instance_id="test-wf-id",
            worker_id="test-worker",
            task_def_name="test_task",
        )

    return _make


@pytest.fixture
def load_fixture():
    """Return a loader for the seed payloads under ``tests/fixtures/``.

    ``load_fixture("gh_pr_view.json")`` parses and returns JSON; any other suffix
    returns the raw file text.
    """
    def _load(name: str):
        path = FIXTURES / name
        text = path.read_text()
        return json.loads(text) if path.suffix == ".json" else text

    return _load
