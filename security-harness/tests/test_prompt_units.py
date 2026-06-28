"""Prompt decomposition for the §19.7 text optimizer: frozen method core + tunable tactics."""
from common import prompt_units as pu

PROMPT = """You are the exploit agent. Think like an attacker; use the app's own features.
<!-- TACTICS:BEGIN -->
- try mass-assignment on profile update
- try IDOR on /api/orders/{id}
<!-- TACTICS:END -->
Always stay in scope."""


def test_split_isolates_the_tunable_region():
    parts = pu.split(PROMPT)
    assert parts["has_region"] is True
    assert "You are the exploit agent" in parts["method_core"]
    assert "mass-assignment" in parts["tactics"]
    assert "Always stay in scope" in parts["post"]


def test_unmarked_prompt_has_no_editable_region_fail_closed():
    parts = pu.split("plain prompt, no markers")
    assert parts["has_region"] is False and parts["tactics"] == ""
    assert pu.editable("plain prompt, no markers") == ""        # nothing auto-tunable


def test_recombine_changes_only_the_tactics_block():
    parts = pu.split(PROMPT)
    out = pu.recombine(parts, "- try JWT alg:none\n- try GraphQL introspection")
    assert "You are the exploit agent" in out and "Always stay in scope" in out   # core + post intact
    assert "alg:none" in out and "mass-assignment" not in out                     # tactics replaced
    # cannot edit a prompt with no region (method core is never auto-edited)
    import pytest
    with pytest.raises(ValueError):
        pu.recombine(pu.split("no region"), "x")


def test_with_exemplars_injects_concrete_failing_cases_as_the_gradient():
    out = pu.with_exemplars("- existing tactic", [
        {"objective_id": "CONF-CROSS-TENANT-READ", "reason": "missed tenantB email in a filtered list"},
    ])
    assert "existing tactic" in out
    assert "CONF-CROSS-TENANT-READ" in out and "filtered list" in out
