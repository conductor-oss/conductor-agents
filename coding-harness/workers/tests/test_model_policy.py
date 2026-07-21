from __future__ import annotations

import json

import pytest

from common.model_policy import (
    ModelPolicyError, normalized_models_config, resolve_model_policy, select_role_tier,
    validate_policy,
)
from revision.tasks import score_candidate


def test_standard_profile_does_not_get_masked_by_legacy_defaults():
    resolved = resolve_model_policy({"codeAgent": "", "codeModel": ""})
    assert resolved["profile"] == "standard"
    assert resolved["roles"]["code"]["tiers"][0]["model"] == "claude-sonnet-5"
    assert resolved["roles"]["review"]["tiers"][0]["priceKey"] == "codex:default"


def test_backend_model_mismatch_fails_closed():
    with pytest.raises(ModelPolicyError, match="backend/model mismatch"):
        resolve_model_policy({"modelOverrides": {"code": {"agent": "claude", "model": "gpt-5"}}})


def test_policy_rejects_commands_and_unknown_versions():
    with pytest.raises(ModelPolicyError):
        validate_policy({"version": 1, "commands": ["rm -rf /"]})
    with pytest.raises(ModelPolicyError):
        validate_policy({"version": 2})


def test_models_config_stays_in_worktree(tmp_path):
    assert normalized_models_config(str(tmp_path), ".conductor-code/models.json").parent.parent == tmp_path
    with pytest.raises(ModelPolicyError):
        normalized_models_config(str(tmp_path), "../models.json")


def test_worker_selects_role_tier_without_legacy_defaults_masking_profile():
    tier, resolution = select_role_tier({"modelProfile": "standard"}, role="review")
    assert resolution["profile"] == "standard"
    assert tier["agent"] == "codex"
    assert tier["model"] == ""


def test_explicit_task_override_wins_for_its_role_and_caps_are_bounded():
    tier, _ = select_role_tier(
        {"modelProfile": "standard", "agent": "claude", "model": "claude-sonnet-5",
         "maxTurns": 10, "maxBudgetUsd": 2},
        role="code",
    )
    assert tier["agent"] == "claude" and tier["model"] == "claude-sonnet-5"
    assert tier["maxTurns"] == 10 and tier["maxBudgetUsd"] == 2.0


def test_snapshot_hash_mismatch_fails_before_worker_can_start_an_agent():
    with pytest.raises(ModelPolicyError, match="modelPolicySha256"):
        resolve_model_policy({"modelPolicy": {"version": 1, "profiles": {}}, "modelPolicySha256": "bad"})


def test_worker_reloads_checked_out_repository_policy_after_remote_preflight(tmp_path):
    policy_dir = tmp_path / ".conductor-code"
    policy_dir.mkdir()
    (policy_dir / "models.json").write_text(json.dumps({"version": 1, "defaultProfile": "trivial"}), encoding="utf-8")
    preflight = resolve_model_policy({"modelProfile": "standard"})

    tier, resolution = select_role_tier(
        {"modelResolution": preflight, "modelProfile": "", "agent": "", "model": ""},
        role="code",
        worktree=str(tmp_path),
    )

    assert resolution["profile"] == "trivial"
    assert tier["model"] == "claude-sonnet-5"


def test_scores_skip_checks_and_require_acceptance():
    score = score_candidate({"checks": [{"status": "skipped"}, {"passed": True, "weight": 2}], "findings": [], "accepted": True})
    assert score["checksScore"] == 1.0 and score["autoEligible"]
    rejected = score_candidate({"checks": [{"passed": True}], "findings": [{"severity": "major"}], "accepted": True})
    assert not rejected["autoEligible"] and rejected["reviewScore"] == 0.5
