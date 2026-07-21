"""Desktop notification behavior."""

from __future__ import annotations

from tui import notify


def test_macos_notification_click_activates_tui_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.delenv("CONDUCTOR_TUI_BUNDLE_ID", raising=False)
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kwargs: calls.append(args))

    notify.notify(True, "Approval requested", "pr_review · PR #1341", "http://localhost/run")

    assert calls == [[
        "terminal-notifier", "-title", "Approval requested",
        "-message", "pr_review · PR #1341",
        "-activate", "com.mitchellh.ghostty",
    ]]


def test_macos_notification_bundle_override(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv("TERM_PROGRAM", "unknown")
    monkeypatch.setenv("CONDUCTOR_TUI_BUNDLE_ID", "dev.example.Terminal")
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kwargs: calls.append(args))

    notify.notify(True, "Title", "Message", "http://localhost/run")

    assert calls[0][-2:] == ["-activate", "dev.example.Terminal"]


def test_unknown_terminal_falls_back_to_execution_url(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv("TERM_PROGRAM", "unknown")
    monkeypatch.delenv("CONDUCTOR_TUI_BUNDLE_ID", raising=False)
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kwargs: calls.append(args))

    notify.notify(True, "Title", "Message", "http://localhost/run")

    assert calls[0][-2:] == ["-open", "http://localhost/run"]


def test_approval_notification_click_signals_and_activates_tui(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.delenv("CONDUCTOR_TUI_BUNDLE_ID", raising=False)
    monkeypatch.setattr(notify.os, "getpid", lambda: 1234)
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kwargs: calls.append(args))

    notify.notify(True, "Approval requested", "pr_review · PR #1341",
                  "http://localhost/run", open_approvals=True)

    assert calls[0][-2] == "-execute"
    assert calls[0][-1] == (
        "/bin/kill -USR1 1234; /usr/bin/open -b com.mitchellh.ghostty"
    )
