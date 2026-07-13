"""Test fixtures: keep chat-session writes out of the real home directory."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HARNESS_HOME", str(tmp_path / "harness-home"))
