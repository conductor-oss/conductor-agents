"""Pure decision logic for openspec_development."""

from __future__ import annotations


def _num(value: object) -> float | int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def aggregate_usage(*, assessment: object, child: object, verification: object) -> dict:
    """Sum token and dollar usage across the assessment, the child, and semantic verification."""
    assessment, child, verification = _mapping(assessment), _mapping(child), _mapping(verification)
    return {
        "totalTokens": (_num(assessment.get("tokenUsed")) + _num(child.get("totalTokens"))
                        + _num(verification.get("tokenUsed"))),
        "totalCostUsd": (_num(assessment.get("costUsd")) + _num(child.get("totalCostUsd"))
                         + _num(verification.get("costUsd"))),
    }
