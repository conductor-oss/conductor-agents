"""Token/cost accumulation + LLM pricing helpers (P3.3)."""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
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


# The run's own price table (its model policy's ``prices``: the bundled defaults merged with the user's
# models.json, the table the console's Settings edit), set for the length of one agent session.
RUN_PRICES: contextvars.ContextVar[dict] = contextvars.ContextVar("conductor_run_prices", default={})


@contextlib.contextmanager
def using_prices(prices: dict | None):
    """Price the sessions started inside this block from ``prices`` (``{model: {input, output}}``)."""
    token = RUN_PRICES.set(dict(prices or {}))
    try:
        yield
    finally:
        RUN_PRICES.reset(token)


def _policy_prices() -> dict[str, tuple[float, float]]:
    """The bundled policy's prices under the run's own: one table, the policy's, with the built-in
    estimates only for ids the policy does not name."""
    global _BUNDLED
    if _BUNDLED is None:
        try:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "defaults",
                                "models.defaults.json")
            with open(path, encoding="utf-8") as fh:
                _BUNDLED = dict(json.load(fh).get("prices") or {})
        except (OSError, ValueError):
            _BUNDLED = {}
    out: dict[str, tuple[float, float]] = {}
    for table in (_BUNDLED, RUN_PRICES.get()):
        for key, rate in (table or {}).items():
            try:
                out[str(key).lower()] = (float(rate["input"]), float(rate["output"]))
            except (KeyError, TypeError, ValueError):
                continue
    return out


_BUNDLED: dict | None = None


def _price_rates(model: str | None) -> tuple[float, float]:
    """Rates for a model id: an exact entry first (the policy's, then the built-in table's), else the most
    specific entry the id contains. The first contained entry was taken before, and "gpt-5" precedes
    "gpt-5.6-terra", so every GPT-5.6 session was priced at GPT-5's rates (run c5fbbe5b: $2.50 reported
    for $6.03 of usage)."""
    if not model:
        return (3.0, 15.0)  # caller must use codex:default for an empty Codex model
    ml = model.lower()
    table = {**{k.lower(): v for k, v in PRICING.items()}, **_policy_prices()}
    if ml in table:
        return table[ml]
    contained = [k for k in table if k in ml]
    if contained:
        return table[max(contained, key=len)]
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
