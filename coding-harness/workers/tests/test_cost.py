"""Unit tests for configurable LLM pricing (common/cost.py).

Import-light: exercises pricing/fallback logic. Env-var overrides require an
``importlib.reload`` since the module reads them at import time.
Run from workers/:  python -m pytest tests/test_cost.py -q
"""

from __future__ import annotations

import importlib

from common import cost


def _reload(monkeypatch, **env):
    """Reload common.cost with the given env vars set (others cleared)."""
    for name in ("LLM_DEFAULT_INPUT_RATE", "LLM_DEFAULT_OUTPUT_RATE", "LLM_PRICING_JSON"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(cost)


# --------------------------------------------------------------------------- defaults

def test_default_rates_for_unknown_model():
    assert cost._price_rates("some-mystery-model-9000") == (3.0, 15.0)


def test_default_rates_for_none_and_empty():
    assert cost._price_rates(None) == (3.0, 15.0)
    assert cost._price_rates("") == (3.0, 15.0)


def test_known_substring_resolves_builtin_rate():
    assert cost._price_rates("claude-opus-4-8") == (15.0, 75.0)


# --------------------------------------------------------------------------- env overrides

def test_env_overrides_default_rates(monkeypatch):
    c = _reload(monkeypatch, LLM_DEFAULT_INPUT_RATE="7.5", LLM_DEFAULT_OUTPUT_RATE="42.0")
    try:
        assert c._price_rates(None) == (7.5, 42.0)
        assert c._price_rates("unknown-model") == (7.5, 42.0)
        # known models unaffected
        assert c._price_rates("claude-opus-4") == (15.0, 75.0)
    finally:
        importlib.reload(cost)


def test_non_numeric_default_env_falls_back(monkeypatch):
    c = _reload(monkeypatch, LLM_DEFAULT_INPUT_RATE="not-a-number")
    try:
        assert c._price_rates(None) == (3.0, 15.0)
    finally:
        importlib.reload(cost)


def test_pricing_json_adds_and_overrides(monkeypatch):
    c = _reload(
        monkeypatch,
        LLM_PRICING_JSON='{"my-custom-model": [1.0, 2.0], "claude-opus-4": [99.0, 100.0]}',
    )
    try:
        assert c._price_rates("my-custom-model-v1") == (1.0, 2.0)
        assert c._price_rates("claude-opus-4-8") == (99.0, 100.0)
    finally:
        importlib.reload(cost)


def test_malformed_pricing_json_ignored(monkeypatch):
    c = _reload(monkeypatch, LLM_PRICING_JSON="{not valid json")
    try:
        # built-in table preserved, no exception raised
        assert c._price_rates("claude-opus-4") == (15.0, 75.0)
        assert c._price_rates(None) == (3.0, 15.0)
    finally:
        importlib.reload(cost)


# --------------------------------------------------------------------------- null hardening

def test_price_usage_none_is_zero():
    assert cost.price_usage(None) == 0.0
    assert cost.price_usage({}) == 0.0


def test_usage_tokens_none_is_zero():
    assert cost.usage_tokens(None) == 0
    assert cost.usage_tokens({}) == 0
