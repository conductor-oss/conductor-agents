from __future__ import annotations

import json
from pathlib import Path


def test_local_review_is_read_only_and_reviews_direct_checkout():
    path = Path(__file__).resolve().parents[1] / "workflows" / "local_review.json"
    workflow = json.loads(path.read_text())
    assert workflow["version"] == 1
    assert workflow["inputParameters"][0] == "repoPath"
    assert workflow["inputTemplate"]["baseRemote"] == "origin"
    assert workflow["inputTemplate"]["baseBranch"] == "main"
    serialized = json.dumps(workflow)
    assert "workspace_prepare" not in serialized
    assert "workspace_cleanup" not in serialized
    assert "pr_submit_review" not in serialized
    assert '"name": "local_diff"' in serialized
    review = workflow["tasks"][1]["inputParameters"]
    assert review["worktreePath"] == "${workflow.input.repoPath}"
    assert review["tools"] == ["Read", "Grep", "Glob"]
    assert review["templateKey"] == "local_review"
