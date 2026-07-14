"""Safety and capability policy regression tests for unattended coding sessions."""

from common.tool_policy import DEFAULT_ALLOWED_TOOLS, denied_without_changes


def test_cargo_commands_are_available_to_unattended_agents():
    assert "Bash(cargo *)" in DEFAULT_ALLOWED_TOOLS


def test_denials_with_no_changes_fail_closed():
    assert denied_without_changes([], ["Bash(cargo test) denied"]) is True
    assert denied_without_changes(["test_output.txt"], ["unrelated denial"]) is False
    assert denied_without_changes([], []) is False
