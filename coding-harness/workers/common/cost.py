"""Token/cost accumulation + LLM pricing helpers (P3.3)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any


def _env_float(name: str, default: float) -> float:
    """Read a float from env; non-numeric/missing values fall back to default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logging.warning("cost: ignoring non-numeric %s=%r; using default %s", name, raw, default)
        return default


# Default fallback rate (USD per 1M tokens) — sonnet pricing by default.
# Configurable via env so operators can tune the fallback for unknown models.
_DEFAULT_INPUT_RATE = _env_float("LLM_DEFAULT_INPUT_RATE", 3.0)
_DEFAULT_OUTPUT_RATE = _env_float("LLM_DEFAULT_OUTPUT_RATE", 15.0)

# USD per 1M tokens (input/output) — P3.3. Source: Anthropic pricing Jun 2025.
# Key: model id substring → (input_per_1M, output_per_1M)
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4":      (15.0, 75.0),
    "claude-sonnet-4-6":  (3.0,  15.0),
    "claude-haiku-4-5":   (0.8,   4.0),
    "claude-haiku-4":     (0.25,  1.25),
    # OpenAI / Codex (best-effort estimate for the Codex backend, which reports
    # tokens but not USD). Approximate public rates; unknown ids fall back below.
    "gpt-5":              (1.25,  10.0),
    "gpt-4.1":            (2.0,   8.0),
    "o4":                 (1.1,   4.4),
    "o3":                 (2.0,   8.0),
    "codex":              (1.25,  10.0),
    # Google Gemini (best-effort estimate for the Gemini CLI backend, which reports
    # tokens but not USD). Approximate public rates; specific ids before the generic
    # "gemini" fallback (substring match walks dict order).
    "gemini-3":           (2.0,  12.0),
    "gemini-2.5-flash":   (0.3,   2.5),
    "gemini-2.5-pro":     (1.25, 10.0),
    "gemini":             (1.25, 10.0),
}


def _load_pricing_overrides() -> None:
    """Merge LLM_PRICING_JSON overrides onto PRICING (env entries win).

    Expects a JSON object of {"model-substring": [input_per_1M, output_per_1M]}.
    Malformed JSON or entries are ignored (with a warning) and the built-in
    table is kept. Existing keys are overridden in place (preserving their
    position in the substring walk); new keys are appended after built-ins.
    """
    raw = os.environ.get("LLM_PRICING_JSON")
    if not raw:
        return
    try:
        overrides = json.loads(raw)
        if not isinstance(overrides, dict):
            raise ValueError("LLM_PRICING_JSON must be a JSON object")
        for key, rates in overrides.items():
            PRICING[str(key)] = (float(rates[0]), float(rates[1]))
    except (ValueError, TypeError, IndexError, KeyError) as exc:
        logging.warning("cost: ignoring malformed LLM_PRICING_JSON: %s", exc)


_load_pricing_overrides()


def _price_rates(model: str | None) -> tuple[float, float]:
    if not model:
        return (_DEFAULT_INPUT_RATE, _DEFAULT_OUTPUT_RATE)  # default to sonnet pricing
    ml = (model or "").lower()
    for key, rates in PRICING.items():
        if key in ml:
            return rates
    return (_DEFAULT_INPUT_RATE, _DEFAULT_OUTPUT_RATE)


def price_usage(usage: dict[str, Any] | None, model: str | None = None) -> float:
    """Compute USD cost from a Claude usage dict + model id."""
    if not usage:
        return 0.0
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cache_w = int(usage.get("cache_creation_input_tokens") or 0)
    cache_r = int(usage.get("cache_read_input_tokens") or 0)
    in_rate, out_rate = _price_rates(model)
    # cache writes billed at 1.25× input rate; reads at 0.1× input rate
    cost = (inp / 1_000_000 * in_rate + out / 1_000_000 * out_rate
            + cache_w / 1_000_000 * in_rate * 1.25
            + cache_r / 1_000_000 * in_rate * 0.1)
    return round(cost, 6)


def price_tokens(token_count: int, model: str | None = None, is_output: bool = False) -> float:
    """Estimate cost for a flat token count (e.g. LLM_CHAT_COMPLETE tokenUsed)."""
    in_rate, out_rate = _price_rates(model)
    rate = out_rate if is_output else in_rate
    return round(token_count / 1_000_000 * rate, 6)


def usage_tokens(usage: dict[str, Any] | None) -> int:
    """Total tokens from a Claude usage dict (input + output + cache r/w)."""
    if not usage:
        return 0
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("output_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )
