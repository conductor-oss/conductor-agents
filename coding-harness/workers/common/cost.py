"""Token/cost accumulation + LLM pricing helpers (P3.3)."""

from __future__ import annotations

from typing import Any

# USD per 1M tokens (input/output) — P3.3. Source: Anthropic pricing Jun 2025.
# Key: model id substring → (input_per_1M, output_per_1M)
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4":      (5.0,  25.0),
    "claude-sonnet-5":    (3.0,  15.0),
    "claude-haiku-4-5":   (1.0,   5.0),
    "claude-haiku-4":     (0.25,  1.25),
    # OpenAI / Codex (best-effort estimate for the Codex backend, which reports
    # tokens but not USD). Approximate public rates; unknown ids fall back below.
    "gpt-5":              (1.25,  10.0),
    "gpt-5.6-sol":        (5.0,  30.0),
    "gpt-5.6-terra":      (2.5,  15.0),
    "gpt-5.6-luna":       (1.0,   6.0),
    "gpt-4.1":            (2.0,   8.0),
    "o4":                 (1.1,   4.4),
    "o3":                 (2.0,   8.0),
    "codex":              (1.25,  10.0),
    "codex:default":      (1.25,  10.0),
    # Google Gemini (best-effort estimate for the Gemini CLI backend, which reports
    # tokens but not USD). Approximate public rates; specific ids before the generic
    # "gemini" fallback (substring match walks dict order).
    "gemini-3":           (2.0,  12.0),
    "gemini-2.5-flash":   (0.3,   2.5),
    "gemini-2.5-pro":     (1.25, 10.0),
    "gemini":             (1.25, 10.0),
}


def _price_rates(model: str | None) -> tuple[float, float]:
    if not model:
        return (3.0, 15.0)  # caller must use codex:default for an empty Codex model
    ml = (model or "").lower()
    for key, rates in PRICING.items():
        if key in ml:
            return rates
    return (3.0, 15.0)


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
