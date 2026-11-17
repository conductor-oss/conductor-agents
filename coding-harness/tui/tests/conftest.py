"""Test fixtures: keep chat-session writes out of the real home directory."""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HARNESS_HOME", str(tmp_path / "harness-home"))



@pytest.fixture(autouse=True)
def _test_actor_registry(tmp_path_factory, monkeypatch):
    """A known actor registry (closure plan WP7): ``human:ops@example.com``, bearer ``test-token``."""
    path = tmp_path_factory.mktemp("actors") / "actors.json"
    path.write_text(json.dumps({"tokens": {"test-token": {"name": "Ops", "email": "ops@example.com",
                                                          "authority": "human"}}}))
    monkeypatch.setenv("SWARM_ACTORS_FILE", str(path))
