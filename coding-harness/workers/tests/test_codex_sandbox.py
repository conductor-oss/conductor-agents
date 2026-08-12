"""Codex sandbox network-access override.

Codex's workspace-write sandbox denies network/socket syscalls by default
(sandbox_workspace_write.network_access defaults to false), including loopback --
confirmed live: a test binding HTTPServer(("127.0.0.1", 0), ...) failed with
PermissionError, a sandbox artifact rather than a project-code failure. This
harness already isolates the agent to its own worktree (write_roots,
_worktree_guard); there is no additional safety this specific restriction buys.
"""

from __future__ import annotations

import asyncio
import io
import sys
import types

from common import codex


class _FakeProc:
    """Minimal subprocess.Popen stand-in for _run_codex_cli's needs."""

    def __init__(self):
        self.stdout = iter([])
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self):
        return 0

    def kill(self):
        pass


def test_run_codex_cli_enables_network_access_in_workspace_write(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(args, **_kwargs):
        captured["args"] = args
        return _FakeProc()

    monkeypatch.setattr(codex.subprocess, "Popen", fake_popen)

    codex._run_codex_cli("fix the bug", worktree=str(tmp_path), write=True)

    args = captured["args"]
    assert "-c" in args
    assert args[args.index("-c") + 1] == "sandbox_workspace_write.network_access=true"


def test_run_codex_cli_read_only_does_not_touch_network_access(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(args, **_kwargs):
        captured["args"] = args
        return _FakeProc()

    monkeypatch.setattr(codex.subprocess, "Popen", fake_popen)

    codex._run_codex_cli("investigate only", worktree=str(tmp_path), write=False)

    assert "sandbox_workspace_write.network_access=true" not in captured["args"]
    assert "-s" in captured["args"]
    assert captured["args"][captured["args"].index("-s") + 1] == "read-only"


class _FakeThread:
    id = "thread-1"

    async def turn(self, _prompt, **_kwargs):
        raise RuntimeError("stop before streaming -- only thread creation is under test")


class _FakeAsyncCodex:
    """Fake openai_codex.AsyncCodex capturing thread_start/thread_resume kwargs."""

    calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def thread_start(self, **kwargs):
        _FakeAsyncCodex.calls.append({"method": "thread_start", **kwargs})
        return _FakeThread()

    async def thread_resume(self, thread_id, **kwargs):
        _FakeAsyncCodex.calls.append({"method": "thread_resume", "thread_id": thread_id, **kwargs})
        return _FakeThread()


def _install_fake_openai_codex(monkeypatch):
    """The SDK import is inline (`from openai_codex import ...`) so a fake module
    installed in sys.modules is picked up without needing the real package's
    async wiring."""
    fake_module = types.SimpleNamespace(
        ApprovalMode=types.SimpleNamespace(deny_all="deny_all"),
        AsyncCodex=_FakeAsyncCodex,
        Sandbox=types.SimpleNamespace(workspace_write="workspace-write", read_only="read-only"),
    )
    monkeypatch.setitem(sys.modules, "openai_codex", fake_module)
    _FakeAsyncCodex.calls = []


def test_run_codex_sdk_enables_network_access_in_workspace_write(monkeypatch, tmp_path):
    _install_fake_openai_codex(monkeypatch)

    # _run_codex_sdk swallows the thread.turn() stop-signal exception into an
    # error-status result dict (same as any other mid-run failure) -- the
    # session was already created with the intended config by that point.
    result = asyncio.run(codex._run_codex_sdk(
        "fix the bug", worktree=str(tmp_path), model=None, write=True,
        effort=None, output_schema=None, resume_session_id=None, on_turn=None,
    ))

    assert result["status"] == "codex_error"
    assert len(_FakeAsyncCodex.calls) == 1
    call = _FakeAsyncCodex.calls[0]
    assert call["method"] == "thread_start"
    assert call["config"] == {"sandbox_workspace_write": {"network_access": True}}


def test_run_codex_sdk_read_only_passes_no_network_override(monkeypatch, tmp_path):
    _install_fake_openai_codex(monkeypatch)

    asyncio.run(codex._run_codex_sdk(
        "investigate only", worktree=str(tmp_path), model=None, write=False,
        effort=None, output_schema=None, resume_session_id=None, on_turn=None,
    ))

    assert _FakeAsyncCodex.calls[0]["config"] is None


def test_run_codex_sdk_resume_also_enables_network_access(monkeypatch, tmp_path):
    _install_fake_openai_codex(monkeypatch)

    asyncio.run(codex._run_codex_sdk(
        "continue fixing", worktree=str(tmp_path), model=None, write=True,
        effort=None, output_schema=None, resume_session_id="thread-1", on_turn=None,
    ))

    call = _FakeAsyncCodex.calls[0]
    assert call["method"] == "thread_resume"
    assert call["config"] == {"sandbox_workspace_write": {"network_access": True}}
