from __future__ import annotations

import json
from pathlib import Path

from tui.chat.tools import _apply_model_choice
from tui.model_profiles import Profile, apply_profile_snapshot, choose_profile, write_profile
from tui.screens.automations import apply_schedule_model_snapshot
from tui.screens.launcher import apply_launch_model_snapshot
from common.model_policy import select_role_tier


def test_explicit_model_maps_to_coding_roles():
    selected = _apply_model_choice("code_parallel", {"model": "gpt-5.6-terra"})
    assert selected["codeModel"] == "gpt-5.6-terra"
    assert "codeAgent" not in selected


def _openai_policy(**extra):
    return {
        "version": 1,
        "name": "Local models",
        "defaultProfile": "openai-standard",
        "profiles": {
            "openai-standard": {"roles": {
                "design": {"agent": "codex", "model": "gpt-5.6-sol"},
                "plan": {"agent": "codex", "model": "gpt-5.6-sol"},
                "code": {"tiers": [{"agent": "codex", "model": "gpt-5.6-terra"}]},
                "review": {"agent": "codex", "model": "gpt-5.6-terra"},
                "judge": {"agent": "codex", "model": "gpt-5.6-terra"},
                "scribe": {"agent": "codex", "model": "gpt-5.6-luna"},
            }},
            "alternate": {"roles": {"code": {"agent": "claude", "model": "claude-sonnet-5"}}},
        },
        **extra,
    }


def test_unscoped_local_models_policy_is_the_global_tui_default_and_keeps_roles(tmp_path):
    path = tmp_path / "models.json"
    write_profile(path, _openai_policy())
    profile = Profile(path, _openai_policy())

    selected = apply_profile_snapshot("code_parallel", {"repoPath": "/work/repo"},
                                      profiles=[profile])

    assert selected["modelProfile"] == "openai-standard"
    assert selected["modelPolicy"] == profile.data
    assert selected["modelPolicySource"] == f"user:{path}"
    assert len(selected["modelPolicySha256"]) == 64
    assert "model" not in selected and "codeModel" not in selected
    tier, _ = select_role_tier(selected, role="code")
    assert tier["agent"] == "codex"
    assert tier["model"] == "gpt-5.6-terra"


def test_explicit_and_scoped_policy_selection_beat_the_global_default(tmp_path):
    global_profile = Profile(tmp_path / "global.json", _openai_policy())
    scoped_policy = _openai_policy(name="Scoped", workflows=["code_parallel"])
    del scoped_policy["profiles"]["alternate"]
    scoped_policy["defaultProfile"] = "openai-standard"
    scoped_profile = Profile(tmp_path / "scoped.json", scoped_policy)

    assert choose_profile("code_parallel", profiles=[global_profile, scoped_profile]) == scoped_profile
    assert choose_profile("local_review", profiles=[global_profile, scoped_profile],
                          explicit="alternate") == global_profile

    selected = apply_profile_snapshot("code_parallel", {},
                                      profiles=[global_profile, scoped_profile])
    assert selected["modelProfile"] == "openai-standard"
    explicit = apply_profile_snapshot("local_review", {"modelProfile": "alternate"},
                                      profiles=[global_profile, scoped_profile])
    assert explicit["modelProfile"] == "alternate"


def test_chat_form_and_schedule_paths_attach_the_same_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "harness-home" / "model-profiles"
    root.mkdir(parents=True)
    write_profile(root / "models.json", _openai_policy())
    monkeypatch.setenv("CONDUCTOR_HARNESS_HOME", str(tmp_path / "harness-home"))

    chat = _apply_model_choice("code_parallel", {"repoPath": "/work/repo"})
    form = apply_launch_model_snapshot("code_parallel", {"repoPath": "/work/repo"})
    chat_schedule = _apply_model_choice("pr_review_sweep", {"repo": "acme/app"})
    screen_schedule = apply_schedule_model_snapshot("pr_review_sweep", "acme/app", {})
    envelope = ("modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256")

    assert {key: chat[key] for key in envelope} == {key: form[key] for key in envelope}
    assert {key: chat_schedule[key] for key in envelope} == {
        key: screen_schedule[key] for key in envelope}


def test_workflows_do_not_accept_backend_overrides():
    root = Path(__file__).resolve().parents[2] / "workers" / "workflows"
    for name, fields in {
        "document_plan.json": ("agent",),
        "code_parallel.json": ("openspecPlanAgent", "codeAgent"),
        "openspec_plan.json": ("openspecPlanAgent",),
        "issue_to_pr.json": ("openspecPlanAgent", "codeAgent"),
    }.items():
        workflow = json.loads((root / name).read_text())
        for field in fields:
            assert field not in workflow["inputParameters"], f"{name}:{field} must defer to modelProfile"
            assert field not in workflow["inputTemplate"], f"{name}:{field} must defer to modelProfile"
