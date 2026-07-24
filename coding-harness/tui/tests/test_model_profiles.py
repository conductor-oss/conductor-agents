from __future__ import annotations

from tui.model_profiles import bootstrap_profiles, load_profiles


def test_bootstrap_creates_editable_anthropic_profile(tmp_path):
    starter = bootstrap_profiles(tmp_path)
    assert starter.name == "models.json"
    profiles = load_profiles(tmp_path)
    assert profiles[0].data["defaultProfile"] == "anthropic-standard"
    assert profiles[0].data["profiles"]["openai-standard"]["roles"]["code"]["tiers"][0]["model"] == "gpt-5.6-terra"
    assert bootstrap_profiles(tmp_path) == starter  # never overwrites a user's file
    assert [profile.path.name for profile in profiles] == ["models.json"]
