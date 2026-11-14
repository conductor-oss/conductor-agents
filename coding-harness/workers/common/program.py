"""Program-scale build logic (roadmap Phase 6, G4 cc_program) — pure ranks + outcome.

A program is several subsystems with cross-subsystem dependencies, built in dependency order on a
shared program branch with an integration check between ranks. The team cycle's board already
dispatches dependency-ready work in waves and integrates continuously, so ``cc_program`` builds the
subsystems *through* ``team_build`` (each subsystem an item with ``dependsOn``); this module computes
the topological ranks for the program report and folds the honest program verdict. 

**Teams of teams (H3, closure plan WP5).** The program has its own board (one item per subsystem); each
subsystem is built by its own child ``team_build`` with its own board, Lead, monitor and reviewers, and
independent subsystems of one rank are built *concurrently* on the shared program branch. The program
Lead folds only subsystem items and child rollups, never every work item — so neither board grows with
the whole program. ``rank_plan`` makes rank-mates file-disjoint (or contract-mediated) by construction;
cross-team ownership is program-scoped and concurrent integrators serialize on the shared branch.
"""

from __future__ import annotations


def _mapping(v):
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _listed(v):
    return v if isinstance(v, list) else []


def topo_ranks(subsystems: list) -> dict:
    """Group subsystems into dependency ranks (rank 0 = no deps; rank k depends only on < k). Returns
    ``{ranks: [[id,...], ...], cyclic: [ids]}``; a cycle (or a dep on an unknown id that never resolves)
    surfaces as ``cyclic`` rather than looping. Pure — deterministic order within a rank by id."""
    items = {}
    for s in _listed(subsystems):
        s = _mapping(s)
        sid = str(s.get("id") or "").strip()
        if sid:
            items[sid] = [str(d).strip() for d in _listed(s.get("dependsOn")) if str(d).strip()]
    placed: dict[str, int] = {}
    ranks: list[list[str]] = []
    remaining = dict(items)
    while remaining:
        ready = sorted(sid for sid, deps in remaining.items()
                       if all(d in placed or d not in items for d in deps))
        if not ready:
            break  # a cycle (or mutual dep) — report the rest as cyclic
        ranks.append(ready)
        for sid in ready:
            placed[sid] = len(ranks) - 1
            remaining.pop(sid)
    return {"ranks": ranks, "cyclic": sorted(remaining.keys())}


def program_report(*, subsystems: list, delivered: object, verdict: object, done_count: object = 0,
                   total: object = 0, tests_passed: object = None, final_head: str = "") -> dict:
    """Fold the program build into an honest report: the dependency ranks, whether every subsystem
    landed in order, and whether cross-subsystem integration is green."""
    ranks_info = topo_ranks(subsystems)
    ranks = ranks_info["ranks"]
    cyclic = ranks_info["cyclic"]
    delivered = delivered is True
    head = str(final_head or "")
    rank_desc = "; ".join(f"rank {i}: {', '.join(r)}" for i, r in enumerate(ranks)) or "(none)"
    if cyclic:
        return {"programDelivered": False, "verdict": "PROGRAM_PLAN_CYCLIC",
                "ranks": ranks, "cyclic": cyclic,
                "report": (f"PROGRAM_PLAN_CYCLIC: the subsystem dependencies do not form a DAG; "
                           f"unresolvable: {', '.join(cyclic)}. No build attempted.")}
    if delivered:
        report = (f"PROGRAM_DELIVERED: all {total} subsystems built in dependency order "
                  f"({rank_desc}) on the shared program branch, cross-subsystem integration green. "
                  f"Head {head[:12]}.")
        return {"programDelivered": True, "verdict": "PROGRAM_DELIVERED", "ranks": ranks,
                "cyclic": [], "report": report}
    # Never a graded failure: a program that did not deliver names what it needs and keeps its state
    # for a continuation (the first principle: the swarm finds a way, it never finds a reason to stop).
    report = (f"PROGRAM_NEEDS: {done_count}/{total} subsystems landed "
              f"(ranks {rank_desc}); downstream verdict {verdict}"
              + ("" if tests_passed is None else f", integration green={tests_passed is True}")
              + f". Head {head[:12]}. Needs — owner decision: continue the run to finish what is left.")
    return {"programDelivered": False, "verdict": "PROGRAM_NEEDS", "ranks": ranks,
            "cyclic": [], "report": report,
            "needs": [{"kind": "owner decision", "detail": f"{done_count}/{total} subsystems landed; {verdict}"}]}


def rank_plan(subsystems: list, *, contract_paths: list | None = None) -> dict:
    """Topological ranks, then split any rank whose members share a (non-contract) file into sub-ranks
    so rank-mates never contend for a file: concurrent child teams are file-disjoint by construction.
    Greedy and deterministic (by id). Returns ``{ranks, cyclic, split}``."""
    base = topo_ranks(subsystems)
    contract = {str(p) for p in (contract_paths or [])}
    files = {}
    for s in _listed(subsystems):
        s = _mapping(s)
        sid = str(s.get("id") or "").strip()
        own = set()
        for f in _listed(s.get("files")):
            own.add(str(f))
        for it in _listed(s.get("items")):
            own |= {str(f) for f in _listed(_mapping(it).get("files"))}
        files[sid] = own - contract
    ranks, split = [], []
    for rank in base["ranks"]:
        groups: list[list[str]] = []
        for sid in rank:
            for g in groups:
                if not any(files.get(sid, set()) & files.get(o, set()) for o in g):
                    g.append(sid)
                    break
            else:
                groups.append([sid])
        if len(groups) > 1:
            split.append({"rank": rank, "into": groups})
        ranks.extend(groups)
    return {"ranks": ranks, "cyclic": base["cyclic"], "split": split}


# Each item's shape is spelled out: with items typed as bare objects a planner wrote every item's
# acceptanceCriteria as one paragraph, the board kept [] for a non-list, and no team of that run had
# a frozen acceptance test (run 90e5e3aa). The validator now rejects that shape and the agent retries.
BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {"subsystems": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"},
            "description": {"type": "string"},
            "deliverable": {"type": "string", "enum": ["code", "docs"]},
            "files": {"type": "array", "items": {"type": "string"}},
            "dependsOn": {"type": "array", "items": {"type": "string"}},
            "acceptanceCriteria": {"type": "array", "items": {"type": "string"},
                                   "description": "one observable criterion per entry, never one paragraph"},
            "reviewCriteria": {"type": "array", "items": {"type": "string"}},
        }, "required": ["id", "description", "files", "acceptanceCriteria"]}},
    }, "required": ["id", "items"]}}},
    "required": ["subsystems"],
}


def design_docs(subsystems: list, delivered: list[str]) -> list[str]:
    """The documents the delivered 'docs' subsystems produced (their files and their items' files):
    the authoritative design the remaining subsystems are broken down from."""
    from common.team_board import deliverable_of
    out: list[str] = []
    for s in _listed(subsystems):
        s = _mapping(s)
        if str(s.get("id")) not in delivered or deliverable_of(s) != "docs":
            continue
        for f in [*_listed(s.get("files")),
                  *(f for it in _listed(s.get("items")) for f in _listed(_mapping(it).get("files")))]:
            if str(f) not in out:
                out.append(str(f))
    return out


def remaining_subsystems(subsystems: list, done: set) -> list[dict]:
    return [_mapping(s) for s in _listed(subsystems) if str(_mapping(s).get("id")) not in done]


def breakdown_prompt(*, description: str, docs: list[str], remaining: list[dict], contract_paths: list,
                     prior: str = "") -> str:
    """The work-breakdown prompt: once the design documents exist they, not the pre-design plan, decide
    how each remaining subsystem is sliced. The program planner slices before any design exists, so its
    items contradicted the design twice (st-lru's hooks in a file another item owned, fc6467b7; an
    r-value item creating the resp/value.go the design forbids, a062184f) and the reviewers enforced the
    design against work the plan had demanded."""
    from common import templating
    return templating.render_default("program_breakdown", breakdown_context(
        description=description, docs=docs, remaining=remaining, contract_paths=contract_paths, prior=prior))


def breakdown_context(*, description: str, docs: list[str], remaining: list[dict], contract_paths: list,
                      prior: str = "") -> dict:
    """The values of the ``program_breakdown`` prompt's placeholders."""
    import json
    keep = ("id", "description", "deliverable", "files", "dependsOn", "acceptanceCriteria", "items")
    subs = [{k: s.get(k) for k in keep if s.get(k) not in (None, "", [])} for s in remaining]
    return {"docs": "\n".join(f"- {d}" for d in docs),
            "contractPaths": ", ".join(str(p) for p in _listed(contract_paths)) or "(none)",
            "description": description,
            "priorSection": f"Prior knowledge about this repository (trust these):\n{prior}\n\n" if prior else "",
            "subsystems": json.dumps(subs, default=str)[:30000]}


def apply_breakdown(subsystems: list, breakdown: dict) -> tuple[list[dict], list[str]]:
    """Replace each subsystem's items with the breakdown's for that id; ids and dependsOn are kept. An
    entry without a usable item (an id and files) leaves its subsystem as it was; an item's dependsOn
    on an id outside its subsystem is dropped (it could never become ready). A subsystem's files
    become the union of its items' files. Returns the new list and the ids whose items changed."""
    by_id: dict[str, list[dict]] = {}
    for entry in _listed(_mapping(breakdown).get("subsystems")):
        entry = _mapping(entry)
        items: list[dict] = []
        for it in _listed(entry.get("items")):
            it = _mapping(it)
            iid = str(it.get("id") or "").strip()
            files = [str(f).strip() for f in _listed(it.get("files")) if str(f).strip()]
            if iid and files and iid not in {x["id"] for x in items}:
                from common.shapes import lines
                items.append({**it, "id": iid, "files": files,
                              "acceptanceCriteria": lines(it.get("acceptanceCriteria")),
                              "reviewCriteria": lines(it.get("reviewCriteria"))})
        ids = {x["id"] for x in items}
        for it in items:
            it["dependsOn"] = [str(d) for d in _listed(it.get("dependsOn")) if str(d) in ids and str(d) != it["id"]]
        if items:
            by_id[str(entry.get("id") or "").strip()] = items
    out, changed = [], []
    for s in _listed(subsystems):
        s = _mapping(s)
        sid = str(s.get("id") or "")
        if sid in by_id and by_id[sid] != _listed(s.get("items")):
            out.append({**s, "items": by_id[sid], "files": sorted({f for it in by_id[sid] for f in it["files"]})})
            changed.append(sid)
        else:
            out.append(s)
    return out, changed


def child_items(subsystem: dict) -> list[dict]:
    """The work items a subsystem's team builds: its own ``items`` when the planner decomposed it, else
    the subsystem itself as one item. Child items keep their own ``dependsOn`` (within the team)."""
    s = _mapping(subsystem)
    from common.shapes import lines
    items = [_mapping(i) for i in _listed(s.get("items")) if _mapping(i).get("id")]
    if items:
        return [{**i, "acceptanceCriteria": lines(i.get("acceptanceCriteria")),
                 "reviewCriteria": lines(i.get("reviewCriteria"))} for i in items]
    return [{"id": str(s.get("id")), "title": str(s.get("title") or s.get("id")),
             "description": str(s.get("description") or ""), "files": _listed(s.get("files")),
             "acceptanceCriteria": lines(s.get("acceptanceCriteria")),
             "reviewCriteria": lines(s.get("reviewCriteria")),
             **({"deliverable": str(s.get("deliverable"))} if s.get("deliverable") else {})}]
