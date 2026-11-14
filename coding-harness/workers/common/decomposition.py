"""Decomposition quality gate and execution-mode routing (roadmap H12 + H14, closure plan WP3).

The execution mode is an *output* of the decomposition, not a guess (H12c): always decompose first,
then read the shape —

  * **single** — one piece: plain ``code_parallel``, one agent, no boards (H9);
  * **parallel** — several pieces with no dependency edge and no shared file: parallel-independent
    execution (``code_parallel`` on the precomputed plan), trivial merge, no collaboration overhead;
  * **team** — any dependency edge or any shared file: the collaborating swarm (``team_build``), where
    frozen contracts, ownership and continuous integration make coupled work compose.

Routing coverable or cleanly separable work to the swarm is a defect (roadmap G1), and so is a bad cut:
the swarm's value rides on clean, low-coupling items (H14). So a plan is **assessed before any agent is
spawned**, and a bad one is sent back to the planner with precise reasons:

  * a dependency cycle, a dependency on an unknown item, duplicate item ids, an item with no files;
  * **duplicate scope** — two items that own exactly the same files;
  * **heavy overlap** — two items, not ordered by ``dependsOn``, whose file sets overlap by half or more
    (Jaccard ≥ ``overlap_threshold``): the same work split in two, which the ownership gate would only
    serialize into thrash.

Light sharing of a file between otherwise distinct items is *not* a defect — that is exactly the
coupled work the swarm exists for (H1), and it routes to ``team``.
"""

from __future__ import annotations

DEFAULT_OVERLAP = 0.5


def _mapping(v) -> dict:
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _files(item: dict) -> list[str]:
    return sorted({str(f).strip() for f in (item.get("files") or []) if str(f).strip()})


def _reach(deps: dict[str, list[str]]) -> dict[str, set[str]]:
    """Transitive dependency closure per item (cycle-safe)."""
    out: dict[str, set[str]] = {}

    def visit(n: str, stack: frozenset) -> set[str]:
        if n in out:
            return out[n]
        acc: set[str] = set()
        for d in deps.get(n, []):
            if d in stack:
                continue
            acc.add(d)
            acc |= visit(d, stack | {d})
        out[n] = acc
        return acc

    for n in deps:
        visit(n, frozenset({n}))
    return out


def _cycle(deps: dict[str, list[str]]) -> list[str]:
    state: dict[str, int] = {}
    path: list[str] = []

    def dfs(n: str) -> list[str]:
        state[n] = 1
        path.append(n)
        for d in deps.get(n, []):
            if d not in deps:
                continue
            if state.get(d) == 1:
                return path[path.index(d):] + [d]
            if not state.get(d):
                found = dfs(d)
                if found:
                    return found
        state[n] = 2
        path.pop()
        return []

    for n in sorted(deps):
        if not state.get(n):
            found = dfs(n)
            if found:
                return found
    return []


PLACEHOLDER_WORDS = {"test", "todo", "tbd", "placeholder", "ok", "x", "n/a", "none", "tba", "fixme", "..."}
_DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")


def _placeholder(text: object) -> bool:
    return str(text or "").strip().strip(".").lower() in PLACEHOLDER_WORDS


def _is_docs(item: dict) -> bool:
    d = str(item.get("deliverable") or "").strip().lower()
    if d:
        return d == "docs"
    files = [str(f) for f in (item.get("files") or []) if str(f).strip()]
    return bool(files) and all(f.lower().endswith(_DOC_SUFFIXES) for f in files)


def assess(subtasks: list, *, contract_paths: list | None = None,
           overlap_threshold: float = DEFAULT_OVERLAP, require_code: bool = False) -> dict:
    """Assess a plan. Returns ``{verdict: ok|replan, mode: single|parallel|team, reasons, fatal, evidence}``.

    ``evidence`` = ``{items, edges, sharedFiles: {file: [items]}, overlaps: [{a, b, jaccard}]}`` — the
    router's ``routeEvidence`` (Gate 0 reads it to check the boundary was drawn correctly). ``fatal``
    reasons invalidate a plan whatever its size: a placeholder item (description or criterion that is
    just "test"/"ok"/"tbd") and, with ``require_code``, a plan with no code deliverable — the program
    planner sent a one-item "test" plan to get past the output validator, and the program built one
    document and reported itself delivered (run b8dc259b)."""
    items = [_mapping(s) for s in (subtasks or []) if isinstance(s, dict)]
    contract = {str(p).strip() for p in (contract_paths or []) if str(p).strip()}
    reasons: list[str] = []
    fatal: list[str] = []
    for it in items:
        iid = str(it.get("id") or "").strip() or "(no id)"
        if _placeholder(it.get("description") or it.get("title")):
            fatal.append(f"item '{iid}' has a placeholder description "
                         f"('{str(it.get('description') or it.get('title') or '').strip()}'); describe the work")
        crit = [c for c in (it.get("acceptanceCriteria") or []) if _placeholder(c)]
        if crit:
            fatal.append(f"item '{iid}' has placeholder acceptance criteria ({', '.join(repr(str(c)) for c in crit)}); "
                         f"state observable criteria")
    if require_code and items and not any(not _is_docs(it) for it in items):
        fatal.append("every subsystem is a documents deliverable; a program that builds software needs at "
                     "least one subsystem with deliverable 'code' (source and tests)")
    reasons.extend(fatal)
    ids = [str(it.get("id") or "").strip() for it in items]
    seen: set[str] = set()
    for iid in ids:
        if not iid:
            reasons.append("an item has no id")
        elif iid in seen:
            reasons.append(f"duplicate item id '{iid}'")
        seen.add(iid)
    known = set(ids)
    deps = {iid: [str(d).strip() for d in (it.get("dependsOn") or []) if str(d).strip()]
            for iid, it in zip(ids, items) if iid}
    edges = [[d, iid] for iid, ds in deps.items() for d in ds]
    for iid, ds in deps.items():
        for d in ds:
            if d not in known:
                reasons.append(f"item '{iid}' depends on unknown item '{d}'")
    cyc = _cycle(deps)
    if cyc:
        reasons.append("dependency cycle: " + " -> ".join(cyc))
    files = {iid: [f for f in _files(it) if f not in contract] for iid, it in zip(ids, items) if iid}
    for iid, fs in files.items():
        if not fs:
            reasons.append(f"item '{iid}' names no files it will change")
    owners: dict[str, list[str]] = {}
    for iid, fs in files.items():
        for f in fs:
            owners.setdefault(f, []).append(iid)
    shared = {f: sorted(o) for f, o in sorted(owners.items()) if len(o) > 1}
    reach = _reach(deps)
    overlaps = []
    order = sorted(files)
    for x in range(len(order)):
        for y in range(x + 1, len(order)):
            a, b = order[x], order[y]
            fa, fb = set(files[a]), set(files[b])
            if not fa or not fb:
                continue
            inter = fa & fb
            if not inter:
                continue
            jac = len(inter) / len(fa | fb)
            ordered = a in reach.get(b, set()) or b in reach.get(a, set())
            overlaps.append({"a": a, "b": b, "jaccard": round(jac, 3), "ordered": ordered,
                             "files": sorted(inter)})
            if fa == fb and not ordered:
                reasons.append(f"items '{a}' and '{b}' own exactly the same files {sorted(fa)} — "
                               f"duplicate scope; merge them or order them with dependsOn")
            elif jac >= overlap_threshold and not ordered:
                reasons.append(f"items '{a}' and '{b}' overlap heavily ({jac:.0%} of their files: "
                               f"{sorted(inter)}) and are not ordered — split by file or add dependsOn")
    n = len([i for i in ids if i])
    mode = "single" if n <= 1 else ("parallel" if not edges and not shared else "team")
    return {
        "verdict": "replan" if reasons else "ok",
        "mode": mode,
        "reasons": reasons,
        "fatal": bool(fatal),
        "evidence": {"items": n, "edges": edges, "sharedFiles": shared, "overlaps": overlaps,
                     "contractPaths": sorted(contract)},
    }


def replan_prompt(reasons: list[str]) -> str:
    """The feedback handed back to the planner when a decomposition is rejected (H14)."""
    return ("Your previous decomposition was REJECTED by the decomposition gate before any agent was "
            "spawned. Fix every problem below and return a corrected plan:\n"
            + "\n".join(f"- {r}" for r in reasons))
