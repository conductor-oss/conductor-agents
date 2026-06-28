#!/usr/bin/env python3
"""The §19.7 PROPOSER — turn a diagnosed failure into a concrete config edit.

Two tiers, both bounded to the **TACTICS region** of a prompt (`prompt_units`): the method core
is frozen, only the tactics/exemplars block is editable (bounded blast radius, attributable).

  deterministic_proposer  — NO LLM, fully offline + reproducible. The §19.7 "gradient" is the
                            concrete failing exemplars from the trace cluster: it appends the
                            missed cases to the tactics block (`prompt_units.with_exemplars`).
                            This is the DEFAULT and needs no API key. Fail-closed: returns None
                            unless the prompt has an opted-in TACTICS region.
  llm_proposer            — OPTIONAL upgrade, gated on ANTHROPIC_API_KEY + the `anthropic` SDK
                            (mirrors the GHSA/NVD feed gating). Asks the model to rewrite ONLY
                            the tactics block to better detect the objective, given the diagnosis
                            and sanitized exemplars. Best-effort: any failure degrades to None.

A `propose_fn` has signature ``(proposal, path) -> str | None`` where ``proposal`` is a
hillclimb.diagnose() dict ({surface, objective_id, diagnosis, evidence, signature}) and the
return is the FULL new file content (or None to skip). ``surface_path`` maps a diagnosed surface
to its file. Profiles/catalog/evidence-bar are not deterministically editable here (JSON / human-
ratify surfaces) — they return None and are handled by a human or the LLM tier.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "workers"))
from common import prompt_units  # noqa: E402

# Diagnosed surface -> the repo file the optimizer edits for it. Prompt = the technique surface
# (exploit phase); evidence_bar/catalog are human-ratify surfaces (the LLM tier may draft them).
_SURFACE_FILES = {
    "prompt": "prompts/exploit.md",
    "profile": "profiles/conductor.json",
    "evidence_bar": "prompts/verify.md",
    "catalog": "catalog/objectives.yaml",
    "tradecraft": "catalog/tradecraft.yaml",
}


def surface_path(surface: str, objective_id: str | None = None, *, root: str = ROOT) -> str | None:
    """Absolute path of the config file for a diagnosed surface, or None if unmapped/missing."""
    rel = _SURFACE_FILES.get(surface)
    if not rel:
        return None
    path = os.path.join(root, rel)
    return path if os.path.isfile(path) else None


def _exemplars(proposal: dict) -> list:
    """The §19.7 gradient: the concrete missed cases (sanitized reasons) for this objective."""
    obj = proposal.get("objective_id")
    return [{"objective_id": obj, "reason": e} for e in (proposal.get("evidence") or []) if e]


_HC_RUNG_FAMILY = "hc-candidate-escalation"


def _load_ladder(path: str, sink: str):
    """(data, rungs) for ``sink``'s ladder in the tradecraft YAML, or None if the file/ladder is
    missing or a candidate rung was already proposed (idempotence). Shared by the deterministic and
    LLM tradecraft proposers so both honor the same structure + one-candidate-per-ladder invariant."""
    if not sink:
        return None
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return None
    ladders = data.get("ladders")
    rungs = ladders.get(sink) if isinstance(ladders, dict) else None
    if not isinstance(rungs, list) or not rungs:
        return None
    if any(isinstance(r, dict) and r.get("family") == _HC_RUNG_FAMILY for r in rungs):
        return None                                   # already proposed -> no change (idempotent)
    return data, rungs


def _append_candidate(data: dict, rungs: list, idea: str) -> str | None:
    """Append the single HC candidate rung carrying ``idea`` and serialize the YAML (the structure is
    code-controlled — a proposer only supplies the bounded ``idea`` string, never the rung shape or
    other surfaces, which bounds the blast radius of an LLM-synthesized technique)."""
    rungs.append({"family": _HC_RUNG_FAMILY, "idea": idea.strip()})
    try:
        import yaml
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=120)
    except Exception:
        return None


def _candidate_idea(sink: str, lessons: list) -> str:
    """The deterministic candidate-rung text. When the deepen ladder left LESSONS of what blocked
    each prior attempt (sanitized, via the proposal evidence), fold them in so the rung is concrete
    and actionable for the ratifier — not a bare 'add something stronger' placeholder."""
    base = (f"HC-PROPOSED (needs ratification): the existing {sink} rungs were exhausted without "
            f"confirmation across runs")
    blockers = [str(x).strip() for x in (lessons or []) if str(x).strip()][:3]
    if blockers:
        return (f"{base}. Prior attempts were blocked because: {'; '.join(blockers)}. Add a rung that "
                f"specifically defeats those obstacles (a new encoding, channel, gadget, or engine).")
    return (f"{base} — add a stronger/alternate technique here (new encoding, channel, gadget, or "
            f"engine) before concluding not-exploitable.")


def _propose_tradecraft(proposal: dict, path: str) -> str | None:
    """Ratify-gated tradecraft edit: when a sink's technique ladder was exhausted without
    confirmation, append a CANDIDATE escalation rung to that ladder in catalog/tradecraft.yaml,
    folding in the deepen LESSONS (why prior attempts were blocked) so the rung is concrete.
    Idempotent (one candidate per ladder). Classifier signature gaps (no sink_class) are left to the
    LLM tier / human — the deterministic proposer will not fabricate a detection signature (it could
    cause false positives)."""
    sink = str(proposal.get("sink_class") or "").strip()
    loaded = _load_ladder(path, sink)
    if loaded is None:
        return None
    data, rungs = loaded
    lessons = [e["reason"] for e in _exemplars(proposal) if e.get("reason")]
    return _append_candidate(data, rungs, _candidate_idea(sink, lessons))


def deterministic_proposer(*, root: str = ROOT):
    """A no-LLM `propose_fn`. Edits a prompt's TACTICS region (auto surface) by appending failing
    exemplars, OR appends a candidate ladder rung to the tradecraft data file (ratify surface).
    Returns None (skip) for unmapped surfaces / no editable region (fail-closed)."""

    def propose_fn(proposal: dict, path: str) -> str | None:
        if proposal.get("surface") == "tradecraft":
            return _propose_tradecraft(proposal, path)
        if proposal.get("surface") != "prompt":
            return None
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            return None
        parts = prompt_units.split(text)
        if not parts.get("has_region") or not parts.get("tactics"):
            return None                                   # fail-closed: nothing auto-editable
        exemplars = _exemplars(proposal)
        if not exemplars:
            return None                                   # no gradient (no missed cases) -> no edit
        new_tactics = prompt_units.with_exemplars(parts["tactics"], exemplars)
        if new_tactics.strip() == parts["tactics"].strip():
            return None                                   # no actual change (no exemplars) -> skip
        return prompt_units.recombine(parts, new_tactics)

    return propose_fn


def _anthropic_client():
    """The Anthropic SDK client, or None if the key/SDK is unavailable (best-effort, like the
    GHSA/NVD feed gating)."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")):
        return None
    try:
        import anthropic  # noqa: F401
    except Exception:
        return None
    try:
        return anthropic.Anthropic()
    except Exception:
        return None


_LLM_SYSTEM = (
    "You optimize a penetration-testing prompt's TACTICS block to better detect a specific "
    "security objective. You are given the current tactics text, a diagnosis of why the current "
    "technique is weak, and concrete missed cases. Rewrite ONLY the tactics — keep it concrete, "
    "imperative, and bounded; do not weaken safety/scope rules. Output ONLY the new tactics text, "
    "no preamble, no code fences."
)

_LLM_TRADECRAFT_SYSTEM = (
    "You are a senior exploitation engineer hardening a technique LADDER for one sink class. A "
    "ladder is an ordered list of escalation rungs the harness walks; the existing rungs were "
    "exhausted without confirming exploitability, and you are given WHY each prior attempt was "
    "blocked. Synthesize ONE concrete next rung that specifically defeats those obstacles — name a "
    "real technique (a precise encoding, channel, gadget chain, parser quirk, or engine), not a "
    "vague 'try harder'. Authorized engagement; this is detection/escalation tradecraft for a "
    "ratify-gated catalog a human reviews before use. Keep it to 1-3 imperative sentences, concrete "
    "and bounded. Do NOT weaken scope/safety rules, do not target anything new, output ONLY the "
    "technique text — no preamble, no code fences, no list markers."
)


def _ladder_summary(rungs: list) -> str:
    return "\n".join(
        f"- {r.get('family', '?')}: {str(r.get('idea', '')).strip()}"
        for r in rungs if isinstance(r, dict)
    ) or "- (none)"


def llm_proposer(*, root: str = ROOT, model: str = "claude-opus-4-8", max_tokens: int = 1500):
    """An LLM-backed `propose_fn`, gated on ANTHROPIC_API_KEY + the SDK. For the PROMPT surface it
    rewrites ONLY the tactics region (method core frozen); for the ratify-gated TRADECRAFT surface it
    synthesizes ONE concrete escalation rung from the diagnosis + sanitized deepen lessons (the rung
    structure is code-controlled — the LLM supplies only the bounded `idea` string). Inputs are
    sanitized (diagnosis + bounded exemplars, never raw target content — H7). Best-effort: any
    failure / no key / unsupported surface falls back to the deterministic proposer so a diagnosed
    surface is never silently dropped."""
    client = _anthropic_client()
    _fallback = deterministic_proposer(root=root)

    def _propose_prompt(proposal: dict, path: str) -> str | None:
        parts = prompt_units.split(open(path, encoding="utf-8").read())
        if not parts.get("has_region"):
            return None
        cases = "\n".join(f"- [{e['objective_id']}] {e['reason']}" for e in _exemplars(proposal)) or "- (none)"
        user = (f"Objective: {proposal.get('objective_id')}\nDiagnosis: {proposal.get('diagnosis')}\n"
                f"Current tactics:\n{parts['tactics']}\n\nMissed cases:\n{cases}")
        resp = client.messages.create(model=model, max_tokens=max_tokens,
                                      system=_LLM_SYSTEM, messages=[{"role": "user", "content": user}])
        new_tactics = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if not new_tactics or new_tactics == parts["tactics"].strip():
            return None
        return prompt_units.recombine(parts, new_tactics)

    def _propose_tradecraft_llm(proposal: dict, path: str) -> str | None:
        sink = str(proposal.get("sink_class") or "").strip()
        loaded = _load_ladder(path, sink)                 # honors the one-candidate-per-ladder gate
        if loaded is None:
            return None
        data, rungs = loaded
        lessons = "\n".join(f"- {e['reason']}" for e in _exemplars(proposal) if e.get("reason")) or "- (none)"
        user = (f"Sink class: {sink}\nDiagnosis: {proposal.get('diagnosis')}\n"
                f"Existing rungs (exhausted):\n{_ladder_summary(rungs)}\n\n"
                f"Why prior attempts were blocked:\n{lessons}")
        resp = client.messages.create(model=model, max_tokens=max_tokens,
                                      system=_LLM_TRADECRAFT_SYSTEM, messages=[{"role": "user", "content": user}])
        idea = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if not idea:
            return None
        return _append_candidate(data, rungs, idea)

    def propose_fn(proposal: dict, path: str) -> str | None:
        if client is None:
            return None                                   # no key -> inert; caller selects deterministic
        surface = proposal.get("surface")
        if surface in ("prompt", "tradecraft"):
            try:
                out = _propose_prompt(proposal, path) if surface == "prompt" \
                    else _propose_tradecraft_llm(proposal, path)
                if out is not None:
                    return out
            except Exception:
                pass                                      # LLM available but declined/failed -> fall back
        return _fallback(proposal, path)

    return propose_fn
