"""SDLC intake routing (roadmap Phase 4, G1) — pure classification→route logic.

``sdlc_intake`` is the single front door: **classify** a raw request (feature|bug|refactor|chore|
greenfield, size, confidence) → proceed on the best classification (never pauses for a human) → **normalize**
into the downstream artifact → **route**. Routing is two-dimensional (G1): by *kind* (which stage
path) and by *execution mode* (single vs team — read off the decomposition, H12). Classification
itself is an LLM judgment; the routing decision it drives is pure and lives here.
"""

from __future__ import annotations

from common import decomposition

VALID_KINDS = ("feature", "bug", "refactor", "chore", "greenfield")
DEFAULT_CONFIDENCE_THRESHOLD = 0.5


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_kind(kind: object) -> str:
    k = str(kind or "").strip().lower()
    return k if k in VALID_KINDS else "feature"


def decide_route(kind: object, *, item_count: int = 0, force_single: bool = False,
                 force_team: bool = False, mode: str | None = None) -> str:
    """The target engine for a classified request. ``greenfield`` → project_bootstrap (G3); ``bug`` →
    bug_fix (G2); everything else by the decomposition's shape (H12): ``single`` (one piece, plain
    code_parallel), ``parallel`` (independent, file-disjoint pieces — parallel-independent execution),
    ``team`` (coupled pieces — the collaborating swarm). ``mode`` is ``decomposition.assess``'s output;
    without it a multi-item plan is treated as coupled."""
    k = normalize_kind(kind)
    if k == "greenfield":
        return "greenfield"
    if k == "bug":
        return "bug"
    if force_team:
        return "team"
    if force_single:
        return "single"
    if mode in ("single", "parallel", "team"):
        return mode
    return "team" if int(item_count or 0) > 1 else "single"


def plan_route(classification: dict, *, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
               force_single: bool = False, force_team: bool = False, require_code: bool = False) -> dict:
    """Fold a classifier's output into the routing decision the workflow SWITCH acts on.

    Returns ``route`` ∈ {bug, greenfield, single, parallel, team, replan}. Humans are passive observers,
    so a low-confidence classification never pauses for a clarification: it proceeds on the best
    classification (confidence is echoed for the record, ``lowConfidence`` flags it); ``replan`` when
    the decomposition gate rejects the plan (H14 — fed back to the planner before any agent spawns);
    otherwise the kind/mode route read off the decomposition (H12).
    Also echoes the normalized downstream fields (instruction, acceptanceCriteria, subtasks)."""
    classification = classification if isinstance(classification, dict) else {}
    kind = normalize_kind(classification.get("kind"))
    confidence = _num(classification.get("confidence"), 1.0)
    subtasks = classification.get("subtasks") if isinstance(classification.get("subtasks"), list) else []
    item_count = len(subtasks)

    # A contract freezes files the work must build AGAINST, never files the work must build. A planner
    # that lists an item's own file as a frozen contract path would block every edit (H4/H14) — so drop
    # any contract path that is also some item's file. This keeps a plan buildable despite a planner slip.
    item_files = {str(f) for it in subtasks if isinstance(it, dict)
                  for f in (it.get("files") or []) if isinstance(f, str)}
    raw_contract = classification.get("contractPaths") or []
    contract_paths = [str(p) for p in raw_contract if isinstance(p, str) and str(p) not in item_files]

    assessment = decomposition.assess(subtasks, contract_paths=contract_paths, require_code=require_code)
    if (kind not in ("bug", "greenfield") and assessment["verdict"] == "replan"
          and not force_single and (item_count > 1 or assessment.get("fatal"))):
        # A bad cut is sent back to the planner before any agent is spawned (H14). A fatal reason (a
        # placeholder item, a program with no code) rejects a one-item plan too: the planner's
        # single "test" item was routed and built as the whole program (run b8dc259b).
        route = "replan"
    else:
        route = decide_route(kind, item_count=item_count, force_single=force_single,
                             force_team=force_team, mode=assessment["mode"])

    return {
        "route": route,
        "kind": kind,
        "confidence": confidence,
        "lowConfidence": confidence < threshold,
        "size": str(classification.get("size") or "medium"),
        "instruction": str(classification.get("instruction") or classification.get("normalizedInstruction") or ""),
        "acceptanceCriteria": classification.get("acceptanceCriteria") or [],
        "subtasks": subtasks,
        "contractPaths": contract_paths,
        "itemCount": item_count,
        "summary": str(classification.get("summary") or ""),
        "mode": assessment["mode"],
        "decompositionVerdict": assessment["verdict"],
        "replanReasons": assessment["reasons"],
        "replanPrompt": decomposition.replan_prompt(assessment["reasons"]) if assessment["reasons"] else "",
        "routeEvidence": assessment["evidence"],
        # The parallel-independent route hands this plan straight to code_parallel (no re-planning).
        "precomputedPlan": {"subtasks": [
            {"id": str(s.get("id")), "description": str(s.get("description") or s.get("title") or ""),
             "files": [f for f in (s.get("files") or []) if isinstance(f, str)]}
            for s in subtasks if isinstance(s, dict)]},
    }
