"""The construction read model — the tracking / learning / review boards (roadmap Phase 2, §4.2).

``common/blackboard.py`` holds only the generic append-log mechanics and deliberately
encodes **no** construction semantics ("the construction read model is built later (roadmap Phase
2)"). This module is that read model: it names the construction fact kinds, provides thin writers for
them, and — the part that makes a pile of append-only facts into *a team's shared state* — **folds**
the log into the current tracking-board kanban, the learning board, and the review board.

Three boards, one append-only log (§4.2 "mixing them is a defect" — they are *kinds*, not stores):

  * **Tracking board** — the living backlog. An ``item`` fact defines a work item; a ``state`` fact
    records a transition (``backlog → ready → in_progress → review → done``, or ``blocked`` /
    ``reopened``). The current board is the fold: the latest state per item wins, so the Lead mutates
    the board by appending, never by rewriting — consistent with the lock-free substrate guarantee.
  * **Learning board** — ``lesson`` facts (text + provenance + confidence, H10/H15). Shared live
    within a run and foldable across runs.
  * **Review / feedback board** — ``review`` facts (a peer-review verdict on an item, H8) and
    ``directive`` facts (steering/constraints, H16: the author field *is* the authority).

Everything here is pure except the thin writers and ``fold_board`` (which reads the board then folds
with the pure ``_fold``). The ``@worker_task`` wrappers live in ``campaign/tasks.py``. File ownership
(``file_ownership``), spend (``budget_gate``), and reopen/retry (convergence) are separate kinds on
the same log and are folded by their own modules; this module references their verdicts, not their
storage.

**One steering channel.** The substrate also offers an agent-to-agent message bus (``send_message`` /
``read_inbox``), which the security swarm uses. The construction team deliberately has ONE channel —
directives on this board — because every directive needs an author-derived authority, a lifecycle, a
place in the audit trail and delivery into running sessions (H11/H16); a second, unaccounted channel
would bypass all four (simplicity policy).
"""

from __future__ import annotations

import uuid

from common import blackboard as bb

# ─────────────────────────── fact kinds (the three boards) ────────────────────
ITEM_KIND = "item"          # tracking board: a work-item definition
STATE_KIND = "state"        # tracking board: an item state transition
LESSON_KIND = "lesson"      # learning board: a lesson/gotcha/invariant
REVIEW_KIND = "review"      # review board: a peer-review verdict on an item
DIRECTIVE_KIND = "directive"  # review board: steering/constraint (author = authority)
EXCEPTION_KIND = "exception"  # review board: an escalation notice for awareness (H7); never awaits a human

# ─────────────────────────── tracking-board states ───────────────────────────
BACKLOG = "backlog"
READY = "ready"
IN_PROGRESS = "in_progress"
REVIEW = "review"
BLOCKED = "blocked"
REOPENED = "reopened"
DONE = "done"

# States that still need work done on them (the board is not drained while any item is here).
OPEN_STATES = frozenset({BACKLOG, READY, IN_PROGRESS, REVIEW, REOPENED})
# States an item can be dispatched from in a wave (dependency-ready work the Lead can assign).
DISPATCHABLE_STATES = frozenset({BACKLOG, READY, REOPENED})


from common.shapes import lines as _lines


def _mapping(value: object) -> dict:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _listed(value: object) -> list:
    return value if isinstance(value, list) else []


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")
_DOC_WORDS = {"docs", "doc", "documentation", "design", "spec", "specification", "research", "adr", "report"}


def deliverable_of(item: dict) -> str:
    """What an item produces: ``code`` (source + tests) or ``docs`` (design docs, specs, ADRs, research
    notes). Agents are not always asked to build code — a design item's output is documents, which need
    no executable acceptance tests and are reviewed against their criteria. Taken from the planner's
    ``deliverable`` when given; otherwise inferred: an item whose every file is a document is ``docs``."""
    item = _mapping(item)
    declared = _str(item.get("deliverable")).strip().lower()
    if declared:
        return "docs" if declared in _DOC_WORDS else "code"
    files = [str(f) for f in _listed(item.get("files")) if str(f).strip()]
    return "docs" if files and all(f.lower().endswith(DOC_SUFFIXES) for f in files) else "code"


# ─────────────────────────── thin writers (append one fact) ───────────────────
def post_item(correlation_id: str, author: str, item: dict, *, backend=None) -> str:
    """Append a work-item definition to the tracking board. ``item`` carries the durable fields
    (id, title, kind, dependsOn, files, acceptanceCriteria, checks, priority); its live *state* is a
    separate transition fact, so re-defining an item never clobbers its progress."""
    item = _mapping(item)
    data = {
        "id": _str(item.get("id")).strip(),
        "title": _str(item.get("title")).strip() or _str(item.get("description")).strip(),
        "description": _str(item.get("description")).strip(),
        "kind": _str(item.get("kind")).strip() or "feature",
        "deliverable": deliverable_of(item),
        "dependsOn": [str(d).strip() for d in _listed(item.get("dependsOn")) if str(d).strip()],
        "replaceDependsOn": item.get("replaceDependsOn") is True,  # an empty list alone means "unchanged"
        "files": [str(f).strip() for f in _listed(item.get("files")) if str(f).strip()],
        # Criteria written as one text (a planner's paragraph) are criteria, not nothing (run 90e5e3aa).
        "acceptanceCriteria": _lines(item.get("acceptanceCriteria")),
        # reviewCriteria are the peer reviewer's CHECK (H8/H13): checked at review, deliberately NOT
        # shown to the implementer, so the author cannot teach to the test and review catches what the
        # shared spec + automated tests do not.
        "reviewCriteria": _lines(item.get("reviewCriteria")),
        "checks": _listed(item.get("checks")),
        "priority": item.get("priority") if isinstance(item.get("priority"), (int, float)) else 0,
        # Frozen acceptance tests written for this item by a DIFFERENT agent before implementation
        # (H13, closure plan WP6), and the commands that run them (the item's own green gate).
        "acceptanceTests": [str(p) for p in _listed(item.get("acceptanceTests")) if str(p).strip()],
        "testCommands": _listed(item.get("testCommands")),
        # The frozen test passed before any work while the item's files already existed: kept as the
        # gate, the implementer confirms the criteria are met (or strengthens it) instead of rebuilding.
        "greenBeforeWork": item.get("greenBeforeWork") is True,
    }
    return bb.post_fact(correlation_id, author, ITEM_KIND, data, backend=backend)


def set_state(correlation_id: str, author: str, item_id: str, state: str, *,
              reason: str = "", attempt: int | None = None, assignee: str = "",
              backend=None) -> str:
    """Append a state transition for one item. The current board is the latest transition per item."""
    data = {"item": str(item_id), "state": str(state), "by": str(author), "reason": str(reason)}
    if attempt is not None:
        data["attempt"] = int(attempt)
    if assignee:
        data["assignee"] = str(assignee)
    return bb.post_fact(correlation_id, author, STATE_KIND, data, backend=backend)


def post_lesson(correlation_id: str, author: str, text: str, *, confidence: str = "medium",
                provenance: str = "", refs: list | None = None, kind: str = "verified",
                backend=None) -> str:
    """Append a lesson to the learning board (H10/H15: provenance + confidence travel with it so a
    reader can distrust an unverified opinion and a later check can invalidate a stale one). ``kind`` is
    ``verified`` (derived from an outcome: integrated, green, reviewed) or ``opinion`` (an agent's own
    assertion — capped at medium confidence and never promoted to a constraint)."""
    kind = "opinion" if str(kind) == "opinion" else "verified"
    if kind == "opinion" and str(confidence) == "high":
        confidence = "medium"
    data = {"text": str(text), "confidence": str(confidence), "provenance": str(provenance),
            "refs": [str(r) for r in (refs or [])], "kind": kind}
    return bb.post_fact(correlation_id, author, LESSON_KIND, data, backend=backend)


def retract_lesson(correlation_id: str, author: str, text: str, reason: str = "", *, backend=None) -> str:
    """Retract a lesson swarm-wide (H15): a lesson contradicted by a later verified outcome stops being
    fed to every agent from the next standup on. Append-only: the retraction is its own fact."""
    return bb.post_fact(correlation_id, author, LESSON_KIND,
                        {"op": "retract", "text": str(text), "reason": str(reason)}, backend=backend)


def post_review(correlation_id: str, author: str, item_id: str, verdict: str, *,
                findings: list | None = None, out_of_scope: list | None = None, scope_only: bool = False,
                backend=None) -> str:
    """Append a peer-review verdict on an item (H8). ``verdict`` is ``lgtm`` or ``changes``; findings
    are the concrete requested changes (never praise/nits — PR review policy). ``out_of_scope`` lists
    changes needed in files the implementer may not write ({file, change}); a round that asks for
    nothing else is ``scope_only`` and counts against no one's review cap (d-store spent one of its two
    rounds on "the Lead needs to widen scope", run 0709d705)."""
    data = {"item": str(item_id), "verdict": str(verdict), "by": str(author),
            "findings": [str(f) for f in (findings or [])],
            "outOfScope": [_mapping(x) for x in (out_of_scope or []) if _mapping(x).get("file")],
            "scopeOnly": bool(scope_only)}
    return bb.post_fact(correlation_id, author, REVIEW_KIND, data, backend=backend)


def post_directive(correlation_id: str, author: str, target: str, text: str, *,
                   authority: str = "agent", sticky: bool = True, urgency: str = "advisory",
                   expires_after_waves: int | None = None, supersedes: str = "",
                   backend=None) -> str:
    """Append a steering directive/constraint (§4.4, H16). ``author`` is who said it = its authority;
    ``target`` is an item id, ``agent:<id>``, ``role:<name>``, or ``team``. ``urgency`` is
    ``advisory`` (injected into a running agent at its next tool boundary) or ``blocking`` (stops the
    running agent; the item is re-opened with the directive, H11)."""
    data = {"id": uuid.uuid4().hex[:12], "target": str(target), "text": str(text), "by": str(author),
            "authority": str(authority), "sticky": bool(sticky),
            "urgency": "blocking" if str(urgency) == "blocking" else "advisory"}
    # Constraint lifecycle (H16): a constraint can lapse after N waves, replace an earlier one, or be
    # lifted — sticky is the default, never "forever by construction".
    if expires_after_waves:
        data["expiresAfterWaves"] = int(expires_after_waves)
    if supersedes:
        data["supersedes"] = str(supersedes)
    return bb.post_fact(correlation_id, author, DIRECTIVE_KIND, data, backend=backend)


def lift_directive(correlation_id: str, author: str, directive_id: str, reason: str = "", *,
                   backend=None) -> str:
    """Lift (retire) a sticky directive/constraint by id (H16 lifecycle)."""
    return bb.post_fact(correlation_id, author, DIRECTIVE_KIND,
                        {"op": "lift", "lifts": str(directive_id), "by": str(author), "reason": str(reason),
                         "target": ""}, backend=backend)


def exempt_directive(correlation_id: str, author: str, directive_id: str, item_id: str, reason: str = "", *,
                     backend=None) -> str:
    """Exempt one item from a directive it cannot follow (the Lead's autonomous answer to an agent's
    honored pushback): the directive stays in force for every other target. Humans are passive in the
    swarm, so nothing waits for a decision; the exemption and its reason are on the board for them to
    see and re-steer."""
    return bb.post_fact(correlation_id, author, DIRECTIVE_KIND,
                        {"op": "exempt", "exempts": str(directive_id), "item": str(item_id), "by": str(author),
                         "reason": str(reason), "target": ""}, backend=backend)


def with_authority(directives: list, *, humans: set | None = None) -> list[dict]:
    """Directives with authority DERIVED from their author (H16): a directive claiming ``human`` from an
    author who is not a registered human ranks as an agent's. Monitor/agent authors keep theirs."""
    from common import actors
    humans = actors.human_emails() if humans is None else humans
    out = []
    for d in directives or []:
        d = _mapping(d)
        by = str(d.get("by") or "")
        out.append({**d, "claimedAuthority": str(d.get("authority") or "agent"),
                    "authority": actors.authority_of(by, humans=humans)})
    return out


def active_directives(directives: list, *, wave_ts: list | None = None) -> list[dict]:
    """The directives still in force (H16 lifecycle): drop lift records, lifted or superseded
    directives, and any whose ``expiresAfterWaves`` have elapsed (counted in waves started after it was
    posted). Oldest first."""
    ds = [_mapping(d) for d in directives or []]
    dead = {str(d.get("lifts")) for d in ds if d.get("op") == "lift"}
    exempt: dict[str, list[str]] = {}
    for d in ds:
        if d.get("op") == "exempt":
            exempt.setdefault(str(d.get("exempts")), []).append(str(d.get("item")))
    dead |= {str(d.get("supersedes")) for d in ds if d.get("supersedes")}
    waves = sorted(float(t) for t in (wave_ts or []))
    out = []
    for d in ds:
        if d.get("op") in ("lift", "exempt") or (d.get("id") and str(d.get("id")) in dead):
            continue
        if str(d.get("id")) in exempt:
            d = {**d, "exempt": exempt[str(d.get("id"))]}
        n = d.get("expiresAfterWaves")
        if isinstance(n, int) and n > 0 and sum(1 for t in waves if t > float(d.get("ts") or 0)) > n:
            continue
        out.append(d)
    return out


def live_lessons(lessons: list, *, repo: str | None = None) -> list[dict]:
    """Lessons safe to feed agents (H10/H15): drop retracted ones (swarm-wide) and, when the shared repo
    is given, any whose refs no longer resolve there (file gone, or ``path::symbol`` symbol gone)."""
    from common import lesson_refs
    ls = [_mapping(x) for x in lessons or []]
    retracted = {str(x.get("text")) for x in ls if x.get("op") == "retract"}
    out = []
    for x in ls:
        if x.get("op") == "retract" or str(x.get("text")) in retracted:
            continue
        if repo and lesson_refs.stale_refs(repo, x.get("refs")):
            continue
        out.append(x)
    return out


def post_exception(correlation_id: str, author: str, item_id: str, kind: str, detail: str, *,
                   severity: str = "warn", backend=None) -> str:
    """Append a monitor escalation to the exception queue (H7) — something above the monitor's
    autonomous authority (a blocked item, a budget breach, a thrash). A notice for awareness: humans are
    passive observers, so nothing waits on it."""
    data = {"item": str(item_id), "kind": str(kind), "detail": str(detail),
            "severity": str(severity), "by": str(author), "resolved": False}
    return bb.post_fact(correlation_id, author, EXCEPTION_KIND, data, backend=backend)


def resolve_exception(correlation_id: str, author: str, item_id: str, kind: str, resolution: str, *,
                      decision: str = "", backend=None) -> str:
    """Resolve an open exception (a human drove the decision from the console/TUI, §4.8). Append-only:
    a later resolution fact supersedes the open one when the queue is folded by (item, kind).
    ``decision`` carries a structured choice the Lead applies (e.g. ``approve``/``reject`` for a
    contract change, WP1.3)."""
    data = {"item": str(item_id), "kind": str(kind), "detail": str(resolution),
            "by": str(author), "resolved": True}
    if decision:
        data["decision"] = str(decision)
    return bb.post_fact(correlation_id, author, EXCEPTION_KIND, data, backend=backend)


def open_exceptions(exceptions: list) -> list[dict]:
    """Fold the exception log by (item, kind): the latest fact wins, keep only the still-open ones."""
    latest: dict[tuple, dict] = {}
    for ex in exceptions:
        ex = _mapping(ex)
        latest[(str(ex.get("item")), str(ex.get("kind")))] = ex
    return [ex for ex in latest.values() if not ex.get("resolved")]


# ─────────────────────────── the fold (pure) ─────────────────────────────────
def _fold(item_facts: list[dict], state_facts: list[dict]) -> dict:
    """Pure fold: item definitions + state transitions -> ``{item_id: current item}``.

    An item's durable fields come from its latest ``item`` fact; its live state from its latest
    ``state`` transition (facts arrive oldest-first, so the last write wins). An item with no state
    transition yet sits in ``backlog``."""
    items: dict[str, dict] = {}
    for fact in item_facts:
        data = _mapping(fact.get("data"))
        iid = _str(data.get("id")).strip()
        if not iid:
            continue
        prev = items.get(iid, {})
        items[iid] = {
            **prev,
            "id": iid,
            "title": _str(data.get("title")) or prev.get("title", ""),
            "description": _str(data.get("description")) or prev.get("description", ""),
            "kind": _str(data.get("kind")) or prev.get("kind", "feature"),
            "deliverable": _str(data.get("deliverable")) or prev.get("deliverable") or deliverable_of(data),
            "dependsOn": (_listed(data.get("dependsOn")) if data.get("replaceDependsOn")
                          else _listed(data.get("dependsOn")) or prev.get("dependsOn", [])),
            "files": _listed(data.get("files")) or prev.get("files", []),
            "acceptanceCriteria": _lines(data.get("acceptanceCriteria")) or prev.get("acceptanceCriteria", []),
            "reviewCriteria": _lines(data.get("reviewCriteria")) or prev.get("reviewCriteria", []),
            "checks": _listed(data.get("checks")) or prev.get("checks", []),
            "priority": data.get("priority", prev.get("priority", 0)),
            "acceptanceTests": _listed(data.get("acceptanceTests")) or prev.get("acceptanceTests", []),
            "testCommands": _listed(data.get("testCommands")) or prev.get("testCommands", []),
            "greenBeforeWork": bool(data.get("greenBeforeWork")) or bool(prev.get("greenBeforeWork")),
            "state": prev.get("state", BACKLOG),
            "attempt": prev.get("attempt", 0),
            "assignee": prev.get("assignee", ""),
            "reason": prev.get("reason", ""),
        }
    for fact in state_facts:
        data = _mapping(fact.get("data"))
        iid = _str(data.get("item")).strip()
        if iid not in items:
            # A transition may reference an item defined later in the same list or not at all; keep a
            # minimal shell so the board never silently drops a referenced item.
            items[iid] = {"id": iid, "title": "", "description": "", "kind": "feature",
                          "dependsOn": [], "files": [], "acceptanceCriteria": [], "reviewCriteria": [],
                          "checks": [], "priority": 0, "state": BACKLOG, "attempt": 0, "assignee": "", "reason": ""}
        cur = items[iid]
        cur["state"] = _str(data.get("state")) or cur["state"]
        if isinstance(data.get("attempt"), (int, float)):
            cur["attempt"] = int(data["attempt"])
        if data.get("assignee"):
            cur["assignee"] = _str(data.get("assignee"))
        cur["reason"] = _str(data.get("reason"))
    return items


def fold_board(correlation_id: str, *, backend=None) -> dict:
    """Read the board and fold it into ``{items, lessons, reviews, directives}``. Items is a map
    ``{id: current item}``; the others are oldest-first lists of their fact data."""
    item_facts = bb.read_facts(correlation_id, kind=ITEM_KIND, backend=backend)
    state_facts = bb.read_facts(correlation_id, kind=STATE_KIND, backend=backend)
    lessons = [{**(f.get("data") or {}), "ts": f.get("ts")}
               for f in bb.read_facts(correlation_id, kind=LESSON_KIND, backend=backend)]
    reviews = [{**(f.get("data") or {}), "ts": f.get("ts")}
               for f in bb.read_facts(correlation_id, kind=REVIEW_KIND, backend=backend)]
    directives = [{**(f.get("data") or {}), "ts": f.get("ts")}
                  for f in bb.read_facts(correlation_id, kind=DIRECTIVE_KIND, backend=backend)]
    exceptions = [f.get("data") or {} for f in bb.read_facts(correlation_id, kind=EXCEPTION_KIND, backend=backend)]
    return {"items": _fold(item_facts, state_facts), "lessons": lessons,
            "reviews": reviews, "directives": directives, "exceptions": exceptions}


# ─────────────────────────── standup / sizing (pure) ──────────────────────────
def ready_items(items: dict) -> list[dict]:
    """Dependency-ready work the Lead can dispatch: items in a dispatchable state whose every
    dependency is ``done``. Sorted by priority (desc) then id, so sizing is deterministic."""
    done = {iid for iid, it in items.items() if it.get("state") == DONE}
    ready = []
    for iid, it in items.items():
        if it.get("state") not in DISPATCHABLE_STATES:
            continue
        deps = [str(d) for d in _listed(it.get("dependsOn"))]
        if all(d in done for d in deps):
            ready.append(it)
    ready.sort(key=lambda it: (-float(it.get("priority") or 0), str(it.get("id"))))
    return ready


def size_wave(ready_count: int, *, max_width: int, budget_state: str = "ok",
              in_flight: int = 0) -> int:
    """Decide this wave's team size (§4.3 step 1, open question #1 — a concrete, testable formula).

    Team size is read off the *ready work*, capped by the remaining parallel slots and throttled by
    the budget: ``halt`` dispatches nothing (let in-flight drain), ``warn`` dispatches at most half
    the free slots (so a near-ceiling run still progresses but slows its burn), ``ok`` fills them.
    Dependency width needs no separate term — only dependency-ready items are counted as ready."""
    if str(budget_state) == "halt":
        return 0
    capacity = max(int(max_width) - max(int(in_flight), 0), 0)
    if capacity <= 0:
        return 0
    n = min(max(int(ready_count), 0), capacity)
    if str(budget_state) == "warn":
        n = min(n, max(1, capacity // 2))
    return n


def pick_best(candidates: list[dict]) -> dict | None:
    """Best-of-N inside the swarm (H12a): choose the winning candidate fork for one item. Pure and
    deterministic, so the choice is auditable. Only candidates that delivered a commit and respect the
    frozen contract are eligible. Order: subtask tests green, delivery audit passed, fewest changed
    files, smallest diff, lowest cost, then candidate ref."""
    eligible = [c for c in (candidates or []) if c.get("delivered") and not c.get("touchesContract")]
    if not eligible:
        return None
    return sorted(eligible, key=lambda c: (
        0 if c.get("testsPassed") else 1,
        0 if c.get("deliveryOutcome") == "passed" else 1,
        int(c.get("changedFiles") or 0),
        int(c.get("diffLines") or 0),
        float(c.get("costUsd") or 0.0),
        str(c.get("ref") or ""),
    ))[0]


def all_done(items: dict) -> bool:
    """Every defined item reached ``done`` (the system-level 'done', H2)."""
    return bool(items) and all(it.get("state") == DONE for it in items.values())


def is_drained(items: dict) -> bool:
    """No item is in an open state — the cycle has nothing left to do (all items are ``done`` or
    terminally ``blocked``). Distinct from ``all_done``: a drained board may still have blocked items,
    which is an honest NO-GO, not convergence."""
    return not any(it.get("state") in OPEN_STATES for it in items.values())


def starvation(items: dict, open_exc: list, *, dispatched: int | None = None, held=None) -> dict:
    """Classify an undrained board on which no work can move (closure plan WP1.4, H2).

    Work can move when some item is dependency-ready (dispatchable) or awaiting review. Otherwise the
    run is stuck and exits naming why — it never waits for a human (humans are passive observers in
    the swarm): open escalations → starved by ``exceptions``; items whose dependencies are blocked →
    starved by ``dependency``. Ready work is not movable either when the wave ``dispatched`` none of it
    because every ready item was ``held`` for a file an open item owns and nothing is in flight to
    land it: the next wave is this wave again (14 empty waves to maxWaves, run a4f1ff1f) → starved by
    ``scope_wait``. Returns ``{starved, starvedBy, detail}``; never starved on a drained board."""
    items = _mapping(items)
    open_exc = _listed(open_exc)
    held = [str(h) for h in _listed(held) if str(h).strip()]
    if is_drained(items):
        return {"starved": False, "starvedBy": "", "detail": ""}
    in_flight = any(it.get("state") in (IN_PROGRESS, REVIEW) for it in items.values())
    if dispatched == 0 and held and not in_flight and ready_items(items):
        return {"starved": True, "starvedBy": "scope_wait",
                "detail": "no work can move: " + "; ".join(held)}
    movable = bool(ready_items(items)) or any(it.get("state") == REVIEW for it in items.values())
    if movable:
        return {"starved": False, "starvedBy": "", "detail": ""}
    blocked = {iid for iid, it in items.items() if it.get("state") == BLOCKED}
    waiting = {}
    for iid, it in items.items():
        if it.get("state") in OPEN_STATES:
            stuck = [str(d) for d in _listed(it.get("dependsOn")) if str(d) in blocked
                     or items.get(str(d), {}).get("state") != DONE]
            if stuck:
                waiting[iid] = stuck
    if open_exc:
        kinds = sorted({str(_mapping(e).get("kind")) for e in open_exc})
        return {"starved": True, "starvedBy": "exceptions",
                "detail": (f"{len(open_exc)} blocked item(s) escalated ({', '.join(kinds)}); waiting items: "
                           f"{', '.join(sorted(waiting)) or '(none)'}")}
    detail = "; ".join(f"{iid} depends on {', '.join(deps)}" for iid, deps in sorted(waiting.items()))
    return {"starved": True, "starvedBy": "dependency",
            "detail": f"dependency-starved: {detail or 'no dependency-ready work'}"}


def dead_dependencies(items: dict) -> dict[str, list[str]]:
    """Dependencies of open items that can never land (pure): on no item of this board, or on a cycle of
    open items. Ghost dependencies are reported for every item that has one; a cycle is reported for one
    item on it (the first by id) with only the edges that lead back to it, so the rest of the order
    stands. The Lead drops exactly these; a real dependency is never touched."""
    items = _mapping(items)
    open_ids = {iid for iid, it in items.items() if it.get("state") in OPEN_STATES}
    ghosts = {iid: [str(d) for d in _listed(items[iid].get("dependsOn")) if str(d) not in items]
              for iid in sorted(open_ids)}
    ghosts = {iid: deps for iid, deps in ghosts.items() if deps}
    if ghosts:
        return ghosts

    def reaches(start: str, target: str, seen: set) -> bool:
        for dep in _listed(items.get(start, {}).get("dependsOn")):
            dep = str(dep)
            if dep == target:
                return True
            if dep in open_ids and dep not in seen:
                seen.add(dep)
                if reaches(dep, target, seen):
                    return True
        return False

    for iid in sorted(open_ids):
        back = [str(d) for d in _listed(items[iid].get("dependsOn"))
                if str(d) == iid or (str(d) in open_ids and reaches(str(d), iid, {iid}))]
        if back:
            return {iid: back}
    return {}


def board_rollup(board: dict) -> dict:
    """A human-digestible summary of the board (H7 — the thing a monitor maintains and a human reads
    instead of the raw firehose). Counts per state, progress, and open review/learning volume."""
    items = _mapping(board.get("items"))
    counts: dict[str, int] = {}
    for it in items.values():
        st = str(it.get("state") or BACKLOG)
        counts[st] = counts.get(st, 0) + 1
    total = len(items)
    done = counts.get(DONE, 0)
    open_reviews = sum(1 for r in _listed(board.get("reviews")) if _str(_mapping(r).get("verdict")) == "changes")
    open_exc = open_exceptions(_listed(board.get("exceptions")))
    return {
        "total": total, "done": done,
        "counts": counts,
        "openStates": {s: counts.get(s, 0) for s in OPEN_STATES if counts.get(s)},
        "blocked": counts.get(BLOCKED, 0),
        "lessons": len(_listed(board.get("lessons"))),
        "openReviews": open_reviews,
        "directives": len(_listed(board.get("directives"))),
        "openExceptions": len(open_exc),
        "allDone": all_done(items),
        "drained": is_drained(items),
        "progress": (done / total) if total else 0.0,
    }
