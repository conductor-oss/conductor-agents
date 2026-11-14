from __future__ import annotations

import json

import pytest

from common import model_policy

from common.model_policy import (
    ModelPolicyError, normalized_models_config, resolve_model_policy, select_role_tier,
    validate_policy,
)


def test_standard_profile_does_not_get_masked_by_legacy_defaults():
    resolved = resolve_model_policy({"codeModel": ""})
    assert resolved["profile"] == "standard"
    assert resolved["roles"]["code"]["tiers"][0]["model"] == "claude-sonnet-5-5"
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


def test_explicit_model_override_infers_its_backend_and_caps_are_bounded():
    tier, _ = select_role_tier(
        {"modelProfile": "standard", "model": "claude-sonnet-5",
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
        {"modelResolution": preflight, "modelProfile": "", "model": ""},
        role="code",
        worktree=str(tmp_path),
    )

    assert resolution["profile"] == "trivial"
    assert tier["model"] == "claude-sonnet-5-5"




def test_effort_is_a_tier_field_the_rescue_ladder_climbs():
    """The planner ran at the CLI default and spent 215 of its 317 s thinking (run 8fb0cf98). Effort
    lives on the profile: the Architect's roles run high; an implementer starts at medium and each
    rescue (`modelTier`) climbs to a higher effort before a stronger model."""
    policy = {"version": 1, "profiles": {"p": {"roles": {
        "plan": {"agent": "claude", "model": "claude-opus-4-8", "effort": "high"},
        "code": {"tiers": [{"agent": "claude", "model": "claude-sonnet-5", "effort": "medium"},
                           {"agent": "claude", "model": "claude-sonnet-5", "effort": "high"},
                           {"agent": "claude", "model": "claude-opus-4-8", "effort": "high"}]}}}}}
    model_policy.validate_policy(policy)
    base = {"modelPolicy": policy, "modelProfile": "p"}
    assert model_policy.select_role_tier(base, role="plan")[0]["effort"] == "high"
    assert model_policy.select_role_tier(base, role="code")[0]["effort"] == "medium"
    second = model_policy.select_role_tier({**base, "modelTier": 1}, role="code")[0]
    assert (second["model"], second["effort"]) == ("claude-sonnet-5", "high")
    third = model_policy.select_role_tier({**base, "modelTier": 2}, role="code")[0]
    assert (third["model"], third["effort"]) == ("claude-opus-4-8", "high")
    bad = {"version": 1, "profiles": {"p": {"roles": {"plan": {"agent": "claude", "effort": "turbo"}}}}}
    with pytest.raises(model_policy.ModelPolicyError):
        model_policy.validate_policy(bad)


def test_bundled_profiles_carry_the_effort_ladder():
    """Measured on run 8ab37c0f's planner prompt: Opus 4.8 took 383-408 s at high and 400 s at medium
    (effort barely moves it); Opus 5.5 took 305 s at high and 147 s at medium ($0.44 against $1.02).
    The Architect starts on Opus 5.5 at medium and a replan or a repair round climbs it to high. An
    implementer replay (run 8ab37c0f, be-scaffold): Sonnet 5 took 123-132 s, Sonnet 5.5 at medium 46 s."""
    roles = model_policy.resolve_model_policy({"modelProfile": "standard"})["roles"]
    for role in ("plan", "design"):
        assert [(t["model"], t["effort"]) for t in roles[role]["tiers"]] == [
            ("claude-opus-5-5", "medium"), ("claude-opus-5-5", "high")]
    assert [(t["model"], t["effort"]) for t in roles["code"]["tiers"]] == [
        ("claude-sonnet-5-5", "medium"), ("claude-sonnet-5-5", "high"), ("claude-opus-5-5", "high")]


def test_a_replan_runs_the_planner_one_rung_higher():
    """The planner runs inside a DO_WHILE and passes the loop's iteration (1 on the first plan) with
    ``modelTierOrigin`` 1: the first plan is rung 0, a replan rung 1."""
    import json
    from pathlib import Path
    first = model_policy.select_role_tier({"modelProfile": "standard", "modelTier": 1, "modelTierOrigin": 1}, role="plan")[0]
    again = model_policy.select_role_tier({"modelProfile": "standard", "modelTier": 2, "modelTierOrigin": 1}, role="plan")[0]
    assert (first["effort"], again["effort"]) == ("medium", "high")
    wf = json.loads((Path(__file__).resolve().parents[1] / "workflows" / "cc_program.json").read_text())
    text = json.dumps(wf)
    assert '"modelTier": "${plan_loop.output.iteration}", "maxTurns": 80' in text.replace("\n", "") or \
        '"modelTier": "${plan_loop.output.iteration}"' in text
    assert '"modelTierOrigin": 1' in text
