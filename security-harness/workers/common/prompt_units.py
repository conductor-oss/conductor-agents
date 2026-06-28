"""Prompt decomposition for the §19.7 text-surface optimizer.

Optimizing a whole prompt is high-blast-radius and hard to attribute. So each tunable prompt
is split into a FROZEN method core (how to think for this phase — never auto-edited) and a
TUNABLE tactics/exemplars block (the *only* editable unit). The optimizer's "gradient" is
concrete failing exemplars (the exact missed seeded vuln + transcript), not aggregate stats.

Convention: a prompt marks its tunable region with a fenced marker so the boundary is
explicit and reviewable:

    <method core text ... never auto-edited ...>
    <!-- TACTICS:BEGIN -->
    ... tunable tactics / exemplars ...
    <!-- TACTICS:END -->

Pure string logic; unit-testable. A prompt with no marker has an empty tunable region
(nothing is auto-editable until a human opts a region in) — fail-closed.
"""

from __future__ import annotations

_BEGIN = "<!-- TACTICS:BEGIN -->"
_END = "<!-- TACTICS:END -->"


def split(text: str) -> dict:
    """Split a prompt into {method_core, tactics, has_region}. If unmarked, the whole prompt
    is method_core and there is NO editable region (fail-closed — nothing auto-tunable)."""
    text = text or ""
    if _BEGIN in text and _END in text and text.index(_BEGIN) < text.index(_END):
        pre, rest = text.split(_BEGIN, 1)
        tactics, post = rest.split(_END, 1)
        return {"method_core": pre, "tactics": tactics.strip(), "post": post, "has_region": True}
    return {"method_core": text, "tactics": "", "post": "", "has_region": False}


def recombine(parts: dict, new_tactics: str) -> str:
    """Rebuild a prompt, replacing ONLY the tactics block — the method core is untouched
    (bounded blast radius, §19.7). Raises if the prompt has no editable region."""
    if not parts.get("has_region"):
        raise ValueError("prompt has no TACTICS region; method core is not auto-editable")
    return f"{parts['method_core']}{_BEGIN}\n{new_tactics.strip()}\n{_END}{parts['post']}"


def editable(text: str) -> str:
    """The substring the optimizer is allowed to change (the tactics block), or '' if none."""
    return split(text).get("tactics", "")


def with_exemplars(tactics: str, failing_exemplars: list, *, limit: int = 3) -> str:
    """Augment a tactics block with concrete failing exemplars — the §19.7 'gradient'. Each
    exemplar is a short, sanitized case the technique must now handle."""
    lines = [tactics.strip(), "", "Cases this must now handle (from missed findings):"]
    for ex in (failing_exemplars or [])[:limit]:
        obj = ex.get("objective_id", "?")
        what = str(ex.get("reason") or ex.get("title") or "").strip()[:160]
        lines.append(f"- [{obj}] {what}")
    return "\n".join(lines).strip()
