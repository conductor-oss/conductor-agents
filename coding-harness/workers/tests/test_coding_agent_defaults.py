"""Env-configurable runtime defaults for the coding_agent worker.

The three fleet-wide knobs (max turns, budget, heartbeat) are read once at import
time via a validated helper. These tests reload ``coding_agent.tasks`` under a
monkeypatched environment and assert the module-level constants pick up valid
overrides and fall back to the hardcoded defaults on unset/invalid values.
"""

from __future__ import annotations

import importlib


def _reload(monkeypatch, **env):
    """Reload coding_agent.tasks with the given env and return the fresh module."""
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    import coding_agent.tasks as mod
    return importlib.reload(mod)


def test_defaults_when_unset(monkeypatch):
    mod = _reload(
        monkeypatch,
        CODING_AGENT_MAX_TURNS=None,
        CODING_AGENT_MAX_BUDGET_USD=None,
        CODING_AGENT_HEARTBEAT_S=None,
    )
    assert mod.CODING_AGENT_MAX_TURNS == 50
    assert mod.CODING_AGENT_MAX_BUDGET_USD == 50.0
    assert mod.CODING_AGENT_HEARTBEAT_S == 30.0


def test_valid_env_overrides(monkeypatch):
    mod = _reload(
        monkeypatch,
        CODING_AGENT_MAX_TURNS="12",
        CODING_AGENT_MAX_BUDGET_USD="7.5",
        CODING_AGENT_HEARTBEAT_S="5",
    )
    assert mod.CODING_AGENT_MAX_TURNS == 12
    assert isinstance(mod.CODING_AGENT_MAX_TURNS, int)
    assert mod.CODING_AGENT_MAX_BUDGET_USD == 7.5
    assert mod.CODING_AGENT_HEARTBEAT_S == 5.0


def test_invalid_env_falls_back(monkeypatch):
    mod = _reload(
        monkeypatch,
        CODING_AGENT_MAX_TURNS="not-a-number",
        CODING_AGENT_MAX_BUDGET_USD="abc",
        CODING_AGENT_HEARTBEAT_S="",
    )
    assert mod.CODING_AGENT_MAX_TURNS == 50
    assert mod.CODING_AGENT_MAX_BUDGET_USD == 50.0
    assert mod.CODING_AGENT_HEARTBEAT_S == 30.0


def test_blank_and_float_for_int_fall_back(monkeypatch):
    # A blank turns value falls back; a float string is not a valid int -> fallback.
    mod = _reload(
        monkeypatch,
        CODING_AGENT_MAX_TURNS="3.5",
        CODING_AGENT_MAX_BUDGET_USD="   ",
        CODING_AGENT_HEARTBEAT_S="not-a-float",
    )
    assert mod.CODING_AGENT_MAX_TURNS == 50
    assert mod.CODING_AGENT_MAX_BUDGET_USD == 50.0
    assert mod.CODING_AGENT_HEARTBEAT_S == 30.0
