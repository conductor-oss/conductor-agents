"""Verify the CLI protocol keeps arbitrary shell commands out of process argv."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from common import coding_agent as coding_agent_module


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "-n",
        'printf "%s" "a b"',
        "printf 'one\\ntwo'",
    ],
)
def test_hol_guard_native_hook_receives_command_only_via_stdin(monkeypatch, command):
    """Pass the exact command privately to the native Claude PreToolUse hook."""
    seen = []
    monkeypatch.setattr(coding_agent_module.shutil, "which", lambda _: "/usr/bin/hol-guard")

    def guard_runner(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "policy_action": "allow",
                "reason_code": "native_policy_allow",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                },
            }),
        )

    monkeypatch.setattr(coding_agent_module.subprocess, "run", guard_runner)

    decision = asyncio.run(
        coding_agent_module._hol_guard_pretool(
            {"cwd": os.getcwd(), "tool_input": {"command": command}}, None, None
        )
    )

    assert decision == {}
    assert len(seen) == 1
    argv, kwargs = seen[0]
    assert argv == ["/usr/bin/hol-guard", "hook", "--harness", "claude-code", "--json"]
    assert command not in argv
    assert json.loads(kwargs["input"]) == {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": os.getcwd(),
    }
    assert kwargs.get("shell", False) is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 10
    assert kwargs["cwd"] == os.getcwd()


def test_hol_guard_binary_lookup_is_bound_before_bash_cwd_switch(monkeypatch, tmp_path):
    """Relative PATH shims from the worker launch checkout cannot authorize Bash."""
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    (launcher / ".git").mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / ".git").mkdir()
    monkeypatch.chdir(launcher)
    monkeypatch.setattr(coding_agent_module.shutil, "which", lambda _: "./bin/hol-guard")
    monkeypatch.setattr(
        coding_agent_module.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("untrusted launch-checkout shim must not run"),
    )
    result = asyncio.run(
        coding_agent_module._hol_guard_pretool(
            {"cwd": str(target), "tool_input": {"command": "git status"}}, None, None
        )
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "untrusted worktree" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("symlinked", [False, True])
def test_guard_rejects_worker_launcher_executable_for_other_bash_checkout(
    monkeypatch, tmp_path, symlinked
):
    """An absolute PATH binary or symlink within the launcher repo is untrusted."""
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    (launcher / ".git").mkdir()
    executable = launcher / ".bin" / "hol-guard"
    executable.parent.mkdir()
    if symlinked:
        external = tmp_path / "external" / "hol-guard"
        external.parent.mkdir()
        external.write_text("test binary", encoding="utf-8")
        try:
            executable.symlink_to(external)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable")
    else:
        executable.write_text("test binary", encoding="utf-8")
    target = tmp_path / "other-checkout"
    target.mkdir()
    (target / ".git").mkdir()
    monkeypatch.chdir(launcher)
    monkeypatch.setattr(coding_agent_module.shutil, "which", lambda _: str(executable))
    monkeypatch.setattr(
        coding_agent_module.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("launcher-owned Guard must not run"),
    )
    result = asyncio.run(
        coding_agent_module._hol_guard_pretool(
            {"cwd": str(target), "tool_input": {"command": "git status"}}, None, None
        )
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "untrusted worktree" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_external_guard_remains_usable_with_explicit_other_bash_checkout(
    monkeypatch, tmp_path
):
    """A binary outside both worktrees can still authorize genuine native policy."""
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    (launcher / ".git").mkdir()
    target = tmp_path / "other-checkout"
    target.mkdir()
    (target / ".git").mkdir()
    binary = tmp_path / "trusted" / "hol-guard"
    binary.parent.mkdir()
    binary.write_text("test binary", encoding="utf-8")
    monkeypatch.chdir(launcher)
    monkeypatch.setattr(coding_agent_module.shutil, "which", lambda _: str(binary))
    seen = []

    def guard_runner(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "policy_action": "allow",
                "reason_code": "native_policy_allow",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                },
            }),
        )

    monkeypatch.setattr(coding_agent_module.subprocess, "run", guard_runner)
    result = asyncio.run(
        coding_agent_module._hol_guard_pretool(
            {"cwd": str(target), "tool_input": {"command": "git status"}}, None, None
        )
    )
    assert result == {}
    assert len(seen) == 1
    assert seen[0][0][0] == str(binary)
    assert seen[0][1]["cwd"] == str(target)


def test_hol_guard_rejects_guard_executable_inside_agent_worktree(monkeypatch, tmp_path):
    """An absolute PATH entry in the project must not supply the security binary."""
    local_guard = tmp_path / "node_modules" / ".bin" / "hol-guard"
    local_guard.parent.mkdir(parents=True)
    local_guard.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(coding_agent_module.shutil, "which", lambda _: str(local_guard))
    monkeypatch.setattr(
        coding_agent_module.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    decision = asyncio.run(
        coding_agent_module._hol_guard_pretool(
            {"cwd": str(tmp_path), "tool_input": {"command": "git status"}},
            None,
            None,
        )
    )

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == []

@pytest.mark.parametrize("git_marker", ["directory", "file"])
@pytest.mark.parametrize("nested_git", [False, True])
def test_guard_rejects_parent_worktree_shim(monkeypatch, tmp_path, git_marker, nested_git):
    """A Bash cwd nested below the Git root cannot trust a parent repo shim."""
    workspace = tmp_path / "worktree"
    cwd = workspace / "src" / "nested"
    shim = workspace / ".bin" / "hol-guard"
    cwd.mkdir(parents=True)
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    if nested_git:
        (cwd / ".git").write_text("untrusted marker", encoding="utf-8")
    marker = workspace / ".git"
    if git_marker == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: /elsewhere\\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(coding_agent_module.shutil, "which", lambda _: str(shim))
    monkeypatch.setattr(
        coding_agent_module.subprocess, "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    decision = asyncio.run(
        coding_agent_module._hol_guard_pretool(
            {"cwd": str(cwd), "tool_input": {"command": "git status"}},
            None, None,
        )
    )
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == []

def test_guard_rejects_worktree_symlink_to_outside_executable(monkeypatch, tmp_path):
    """A worktree PATH link must not impersonate the external installed Guard."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").mkdir()
    outside = tmp_path / "trusted-bin" / "hol-guard"
    outside.parent.mkdir()
    outside.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    project_bin = worktree / ".bin"
    project_bin.mkdir()
    symlink = project_bin / "hol-guard"
    try:
        symlink.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")

    monkeypatch.setattr(coding_agent_module.shutil, "which", lambda _: str(symlink))
    monkeypatch.setattr(
        coding_agent_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("project-controlled symlink must not launch"),
    )
    decision = asyncio.run(
        coding_agent_module._hol_guard_pretool(
            {"cwd": str(worktree), "tool_input": {"command": "git status"}},
            None, None,
        )
    )
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("change", ["command", "cwd", "nested"])
def test_guard_rejects_mutated_hook_inputs_during_async_evaluation(
    monkeypatch, change
):
    """An old native allow must not authorize different command inputs."""
    event = {
        "cwd": os.getcwd(),
        "tool_input": {"command": "git status", "flags": {"mode": "safe"}},
    }
    monkeypatch.setattr(
        coding_agent_module.shutil, "which", lambda _: "/usr/bin/hol-guard"
    )

    async def guard_returns_after_input_changes(*_args, **_kwargs):
        if change == "command":
            event["tool_input"]["command"] = "rm -rf build"
        elif change == "cwd":
            event["cwd"] = "/different-working-directory"
        else:
            event["tool_input"]["flags"]["mode"] = "unsafe"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "policy_action": "allow",
                "reason_code": "native_policy_allow",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                },
            }),
        )

    monkeypatch.setattr(
        coding_agent_module.asyncio, "to_thread",
        guard_returns_after_input_changes,
    )
    result = asyncio.run(
        coding_agent_module._hol_guard_pretool(event, None, None)
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "inputs changed" in result["hookSpecificOutput"]["permissionDecisionReason"]
