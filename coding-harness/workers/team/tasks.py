"""The construction team cycle (roadmap Phase 2, §4.3) — the collaborating-swarm engine.

Phase 1's ``convergence_spike`` proved the hard premise (≥3 agents converge on shared code behind a
frozen contract, by continuous rebase, no big-bang merge). This module generalizes that proven
wave-loop into the **team cycle** the roadmap specifies:

    standup → fan out → implement ∥ review → integrate → learn → re-plan   (a DO_WHILE)

The static subtask list becomes a **mutable tracking board** (``common/team_board.py``) the Lead
mutates by appending; a **learning board** (lessons shared live) and a **review board** (mid-run peer
review, H8) sit on the same append-only log. Each worker here is a thin wrapper over the pure board
read-model and the Phase-1 primitives (``file_ownership``, ``continuous_integrate``,
``contract_freeze``, ``budget_gate``) — no new shared-state abstraction, per §4.6.

Why each task earns its place (AGENTS.md simplicity policy):
  * ``team_plan_init`` — seeds the tracking board + freezes the contract (H4), once per run.
  * ``team_standup`` — the Lead: folds the board, computes dependency-ready work, **sizes the wave**
    from it (open question #1 formula in ``team_board.size_wave``), gates dispatch on shared-file
    ownership (H1), and **injects the live board rollup + teammates' lessons + the item's directives**
    into each implementer's prompt (this is what makes it a team, §4.2).
  * ``team_integrate`` — continuous-rebase integrator (H1): clean deliveries move to ``review``,
    empty/conflicting ones are re-queued or blocked (honest, bounded), spend is recorded (H5),
    ownership released.
  * ``team_review_build`` / ``team_apply_reviews`` — mid-run peer review (H8/H13): a *different* agent
    than the implementer reviews each item's diff against its acceptance criteria; ``changes`` reopens
    the item with the findings attached as a directive (bounded rounds), ``lgtm`` marks it done and
    posts a lesson. This is the "implement ∥ review → re-plan" half of the cycle.
  * ``team_outcome`` — folds the whole board into an honest delivered / not-delivered verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

from conductor.client.worker.worker_task import worker_task

from common import (architect, budget_gate, contract_event, contract_freeze, continuous_integrate, prompt_files, retrospective,
                    rung_policy, templating, file_ownership, git, monitor, team_board, verification)
from common.code_parallel import CODE_SUBTASK_TOOLS
from common.results import fail, ok
from common.continuous_integrate import RETRYABLE_REOPEN as _RETRYABLE_REOPEN
from common.shapes import (iter_joined as _iter_joined, listed as _list, mapping as _mapping,  # noqa: F401
                           num as _num, subtask_worktree as _subtask_worktree, truth as _truth)

_REVIEW_TOOLS = ["Read", "Grep", "Glob"]          # read-only: a reviewer never edits code
# The implementer's structured end-of-session report (WP7/WP8): what it learned (an opinion until an
# outcome verifies it, H15) and any constraint it refuses to follow (pushback, H16).
IMPLEMENTER_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "one line: what you changed"},
        "lessons": {"type": "array", "description": "facts about THIS codebase a teammate should know "
                    "(conventions, gotchas, invariants), each with the files/symbols it is about",
                    "items": {"type": "object", "properties": {
                        "text": {"type": "string"},
                        "refs": {"type": "array", "items": {"type": "string"},
                                 "description": "repo paths, or path::symbol"}}, "required": ["text"]}},
        "pushback": {"type": ["object", "null"], "description": "set ONLY if one of the steering "
                     "directives/constraints listed in your task is harmful or impossible and you therefore did "
                     "NOT follow it; 'directive' must quote that directive. A tool, permission or environment "
                     "limitation is NOT pushback — leave this null.",
                     "properties": {"directive": {"type": "string"}, "reason": {"type": "string"}}},
        # cmd-dispatch's implementer found its registry/dispatch already written by a sibling inside
        # that sibling's file, said so in prose, and changed nothing: the audit ran six repair agents
        # and the item blocked as an empty delivery (run 90e5e3aa). A structured claim routes it to
        # the peer reviewer, who decides done-as-is or what is misplaced.
        "alreadySatisfied": {"type": ["object", "null"], "description": "set ONLY if you made NO change "
                             "because the shared branch ALREADY satisfies this item (e.g. a sibling delivered "
                             "it); 'evidence' names the files and symbols that do. A peer reviewer confirms "
                             "it — never report this only as a lesson or in the summary.",
                             "properties": {"evidence": {"type": "string"}}},
    },
    "required": ["summary"],
}
REVIEWER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["lgtm", "changes"],
                    "description": "changes only for a blocking, concrete problem"},
        "findings": {"type": "array", "items": {"type": "string"},
                     "description": "each a specific requested change the implementer can make "
                                    "in the files it may write; empty for lgtm"},
        "outOfScope": {"type": "array", "items": {"type": "object", "properties": {
            "file": {"type": "string"}, "change": {"type": "string"}},
            "required": ["file", "change"]},
            "description": "blocking changes needed in files the implementer may NOT write "
                           "(the exact repository path); the Lead widens the item's scope "
                           "and re-dispatches it"},
        "retract": {"type": "array", "items": {"type": "string"},
                    "description": "exact text of any team lesson this diff proves WRONG"},
        "unresolved": {"type": "array", "items": {"type": "string"},
                       "description": "the PREVIOUS round's findings (the steering directives "
                                      "'Address the peer-review findings: ...') this revision "
                                      "did NOT address — copy their text; empty when every prior "
                                      "finding is addressed or this is the first round"},
    },
    "required": ["verdict", "findings", "unresolved"],
}
_DIFF_LIMIT = 24000                               # chars of per-item diff shown to a reviewer


def _review_fields(out: dict) -> dict:
    """Extract {verdict, findings} from a coding_agent review output. A schema-structured coding_agent
    returns the fields under ``structured`` (parsed) and ``result`` (a JSON string), NOT at the task
    output's top level. Prefer the parsed object, then the JSON string, then any top-level fields (the
    shape unit tests feed). A review that produced no parseable verdict (the reviewer task failed, was
    skipped, or returned free text) is ``unavailable`` — never ``lgtm``: a missing review must not
    count as a passing one (closure plan WP1.1, H8)."""
    out = _mapping(out)
    for src in (out.get("structured"), out.get("result"), out):
        obj = src
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except (ValueError, TypeError):
                continue
        obj = _mapping(obj)
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict in ("lgtm", "changes"):
            out_of_scope = []
            for x in _list(obj.get("outOfScope")):
                x = {"file": str(x), "change": ""} if isinstance(x, str) else _mapping(x)
                if str(x.get("file") or "").strip():
                    out_of_scope.append({"file": str(x["file"]).strip(), "change": str(x.get("change") or "").strip()})
            # ``unresolved`` absent (an older reviewer shape) is unknown, not "all addressed": None.
            unresolved = ([str(f) for f in _list(obj.get("unresolved")) if str(f).strip()]
                          if isinstance(obj.get("unresolved"), list) else None)
            return {"verdict": verdict, "findings": [str(f) for f in _list(obj.get("findings")) if str(f).strip()],
                    "outOfScope": out_of_scope, "unresolved": unresolved}
    return {"verdict": "unavailable", "findings": [], "outOfScope": [], "unresolved": None}


def _implementer_report(out: dict) -> dict:
    """The implementer's structured report (IMPLEMENTER_SCHEMA) from a code_subtask output."""
    out = _mapping(out)
    st = out.get("structured")
    if isinstance(st, str):
        try:
            st = json.loads(st)
        except (ValueError, TypeError):
            st = {}
    return _mapping(st)


def _norm_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if len(w) > 2}


def _honored_pushback(pushback: dict | None, given: list[str]) -> dict | None:
    """A pushback counts only if it names a directive the agent was actually given (H16): otherwise it is
    not a refused constraint (e.g. 'I could not run the tests' — measured live, run 484c7a00) and must
    not block a delivery. Match = word overlap of at least half of the smaller text."""
    pb = _mapping(pushback)
    if not str(pb.get("reason") or "").strip():
        return None
    claim = _norm_words(pb.get("directive") or "")
    for text in given:
        words = _norm_words(text)
        if claim and words and len(claim & words) >= max(1, min(len(claim), len(words)) // 2):
            return {**pb, "matched": text}
    return None


def _spend_args(out: dict) -> dict:
    out = _mapping(out)
    usd, tok = out.get("costUsd"), out.get("tokenUsed")
    return {"usd": float(usd) if isinstance(usd, (int, float)) and not isinstance(usd, bool) else None,
            "tokens": int(tok) if isinstance(tok, (int, float)) and not isinstance(tok, bool) else None,
            "model": str(out.get("model") or "")}


def _record(cid: str, author: str, out: dict) -> bool:
    """Record one agent's real spend on the run's budget (H5). Every agent the team runs is recorded
    at the join that consumes its output — implementers, reviewers, the planner, test repair."""
    a = _spend_args(out)
    if a["usd"] is None and a["tokens"] is None:
        return False
    budget_gate.record_spend(cid, author, usd=a["usd"], tokens=a["tokens"], model=a["model"])
    return True


# ─────────────────────────── prompt-injection digests ─────────────────────────
def _lesson_digest(lessons: list, limit: int = 8) -> str:
    out = []
    for les in list(lessons)[-limit:]:
        les = _mapping(les)
        text = str(les.get("text") or "").strip()
        if not text:
            continue
        conf = str(les.get("confidence") or "medium")
        kind = "opinion" if les.get("kind") == "opinion" else "verified"
        refs = ", ".join(str(r) for r in _list(les.get("refs")))
        out.append(f"- [{kind}, {conf}] {text}" + (f" (refs: {refs})" if refs else ""))
    return "\n".join(out)


def _parent_directives(i: dict) -> list[dict]:
    """Steering across levels (H3): program-board directives for this team's subsystem (or the whole
    program), presented as team-wide directives. Used by the standup AND the reviewer — a reviewer blind
    to a program directive rejects work for following it (measured live, run dcc695a6)."""
    parent = str(i.get("parentCorrelationId") or "").strip()
    if not parent:
        return []
    sub = str(i.get("subsystemId") or "")
    out = []
    for d in team_board.fold_board(parent)["directives"]:
        target = str(d.get("target") or "")
        if target in (f"subsystem:{sub}", "team"):
            out.append({**d, "target": "team"})
        elif target.startswith(f"subsystem:{sub}:"):
            # The Architect named the item: only that implementer reads it (five siblings pushed back on
            # advice about sorted-set scores that was for one item, run 351812b2).
            out.append({**d, "target": target[len(f"subsystem:{sub}:"):]})
    return out


def _steering(cid: str | None, board: dict) -> list[dict]:
    """The directives in force, with authority derived from their author (H16)."""
    waves = [f.get("ts") for f in file_ownership.bb.read_facts(cid, kind="wave")] if cid else []
    return team_board.with_authority(team_board.active_directives(board.get("directives") or [], wave_ts=waves))


def _directive_digest(directives: list, item_id: str) -> str:
    # Human authority first, then the rest; only directives for this item or the whole team.
    rank = {"human": 0, "monitor": 1, "agent": 2}
    picked = [_mapping(d) for d in directives
              if str(_mapping(d).get("target")) in (item_id, "team")
              and item_id not in (_mapping(d).get("exempt") or [])]
    picked.sort(key=lambda d: rank.get(str(d.get("authority")), 3))
    return "\n".join(f"- ({d.get('authority','agent')}) {d.get('text','')}" for d in picked if d.get("text"))


def _rollup_line(rollup: dict) -> str:
    counts = _mapping(rollup.get("counts"))
    parts = [f"{v} {k}" for k, v in counts.items()]
    return f"{rollup.get('done', 0)}/{rollup.get('total', 0)} done" + (
        f" (" + ", ".join(parts) + ")" if parts else "")


# ─────────────────────────────── team_route (H9/H12) ──────────────────────────
@worker_task(task_definition_name="team_route")
def team_route(task):
    """Graceful-degradation router (H9/H12): the execution mode is read off the decomposition, not
    guessed. One small piece → ``single`` (plain code_parallel, no boards, zero ceremony); coupled or
    multi-item work → ``team`` (the board-driven cycle). This is the Phase-2 boundary; Phase 4's
    sdlc_intake layers richer request classification on the same seam."""
    i = task.input_data or {}
    try:
        items = _list(i.get("subtasks"))
        force_single = _truth(i.get("forceSingle"), False)
        force_team = _truth(i.get("forceTeam"), False)
        single = force_single or (not force_team and len(items) <= 1)
        mode = "single" if single else "team"
        first = _mapping(items[0]) if items else {}
        instruction = str(i.get("instruction") or first.get("description") or first.get("title") or "").strip()
        reason = ("forced single" if force_single else
                  "forced team" if force_team else
                  f"{len(items)} item(s) → {'one small piece, no boards' if single else 'team cycle'}")
        return ok(task, {"mode": mode, "instruction": instruction, "itemCount": len(items), "reason": reason},
                  [f"[team_route] mode={mode} ({reason})"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_route", e)


# ─────────────────────────────── team_plan_init ───────────────────────────────
@worker_task(task_definition_name="team_plan_init")
def team_plan_init(task):
    """Seed the tracking board from the input work items and freeze the contract (H4) once per run."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip() or None
        items = _list(i.get("subtasks"))
        contract_paths = [str(p).strip() for p in _list(i.get("contractPaths")) if str(p).strip()]
        seeded = []
        planned_spend = False
        parent = str(i.get("parentCorrelationId") or "").strip()
        if cid and parent and not [f for f in file_ownership.bb.read_facts(parent, kind="child")
                                   if _mapping(f.get("data")).get("childRun") == cid]:
            # Teams of teams (H3): register this team on the program board so the program Lead and the
            # console see the team tree (the program reads rollups, never this team's items).
            file_ownership.bb.post_fact(parent, cid, "child", {"subsystem": str(i.get("subsystemId") or ""),
                                                              "childRun": cid})
        if cid:
            if not file_ownership.bb.read_facts(cid, kind=contract_event.CONTRACT_KIND):
                file_ownership.bb.post_fact(cid, "lead", contract_event.CONTRACT_KIND,
                                            {"paths": contract_paths, "frozen": True, "revision": 1})
                # The planner that produced this plan is an agent too (H5): its spend counts once.
                planned_spend = _record(cid, "planner", _mapping(i.get("planUsage")))
            for entry in items:
                entry = _mapping(entry)
                sid = str(entry.get("id") or "").strip()
                if not sid:
                    continue
                team_board.post_item(cid, "lead", entry)
                seeded.append(sid)
            # No separate acceptance-author chain: the Architect's checks, the items' own tests and the
            # peer review are the gates. The board drains when its items are done.
            file_ownership.bb.post_fact(cid, "acceptance", ACCEPTANCE_SETTLED_KIND, {"authored": 0, "reused": []})
        return ok(task, {"total": len(seeded), "itemIds": seeded, "contractPaths": contract_paths,
                         "plannerSpendRecorded": planned_spend},
                  [f"[team_plan_init] seeded {len(seeded)} items; contract={contract_paths}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_plan_init", e)


# ─────────────────────────── contract decisions (WP1.3, H4) ─────────────────────
AMENDMENT_KIND = "acceptance_amendment"


def _frozen_acceptance(cid: str) -> dict:
    """The latest contract fact that froze acceptance tests: {acceptanceTests, acceptanceCommit, amendedAt}."""
    facts = [_mapping(f.get("data")) for f in file_ownership.bb.read_facts(cid, kind=contract_event.CONTRACT_KIND)]
    acc = [d for d in facts if d.get("acceptanceCommit")]
    return acc[-1] if acc else {}


def _pending_amendment(cid: str | None, gid: str) -> dict:
    """This item's latest acceptance-test amendment, unless it has already been decided."""
    if not cid:
        return {}
    last = {}
    for f in file_ownership.bb.read_facts(cid, kind=AMENDMENT_KIND):
        d = _mapping(f.get("data"))
        if d.get("item") == gid:
            last = {} if d.get("decision") else d
    return last


def _decide_amendment(cid: str, repo: str, branch: str, amend: dict, approve: bool, why: str) -> bool:
    """The reviewer's decision on an acceptance-test amendment. Approve → merge the fork and re-freeze the
    amended tests at the merged head; reject → discard the fork. Returns True when merged."""
    gid = str(amend.get("item"))
    merged = False
    if approve:
        try:
            res = continuous_integrate.integrate_ready(repo, branch, [{"id": gid, "worktree": amend.get("worktree"),
                                                                        "branch": amend.get("branch"), "readyAt": 0}])
        except Exception as exc:  # noqa: BLE001
            # A git failure on the merge ended the whole resp team as a FAILED task (an untracked
            # internal/resp/writer.go another team's repair agent had written into the shared worktree,
            # run a4f1ff1f). The approved work is rebuilt against the current head instead.
            res = {"integrated": []}
            why = f"approved, but the merge failed: {exc}"
        merged = any(str(r.get("item")) == gid for r in res.get("integrated", []))
        if merged:
            cur = _frozen_acceptance(cid)
            head = git.head(repo)
            contract = contract_event.current_contract(
                file_ownership.bb.read_facts(cid, kind=contract_event.CONTRACT_KIND))
            file_ownership.bb.post_fact(cid, "lead", contract_event.CONTRACT_KIND, {
                **cur, "paths": contract["paths"], "frozen": True, "revision": contract["revision"] + 1,
                "amendedAt": {**_mapping(cur.get("amendedAt")), **{str(p): head for p in _list(amend.get("paths"))}}})
            file_ownership.bb.post_fact(cid, "integrator", "delivery", {
                "item": gid, "base": str(amend.get("base") or ""), "head": head, "files": _list(amend.get("files"))})
    # Decided either way, the fork is finished: merged forks live on the shared branch, rejected ones
    # are discarded. Its worktree and branch go (see team_integrate on why worktrees must not linger).
    git.worktree_remove(repo, str(amend.get("candidate") or gid))
    git.git(repo, "branch", "-D", str(amend.get("branch") or ""), check=False)
    file_ownership.bb.post_fact(cid, "lead", AMENDMENT_KIND, {
        "item": gid, "decision": "approve" if merged else "reject", "paths": _list(amend.get("paths")),
        "reason": why[:600]})
    return merged
def _done_section(repo: str) -> str:
    """The program's definition of done from the branch (``.cc/contract.json`` and its check scripts),
    as a prompt section; empty on a repository without one (a plain ``team_build``)."""
    brief = architect.read_contract_brief(repo) if repo and os.path.isdir(repo) else ""
    return f"\n{brief}\n" if brief else ""


def _board_contract_paths(cid: str | None, given: list[str]) -> list[str]:
    """The frozen contract = the workflow's declared paths ∪ the board's current contract fact (which
    later steps — e.g. acceptance tests frozen at plan time — may extend)."""
    paths = list(given)
    if cid:
        cur = contract_event.current_contract(file_ownership.bb.read_facts(cid, kind=contract_event.CONTRACT_KIND))
        paths += [p for p in cur["paths"] if p not in paths]
    return paths


def _apply_contract_decisions(cid: str, repo: str, board: dict) -> list[dict]:
    """The Lead applies every recorded-but-unapplied contract decision before sizing the wave. Humans are
    passive observers and a frozen contract stays frozen, so the decision is always the Lead's reject
    (``team_integrate`` records it the moment the edit is detected): the proposer is re-opened with a
    "do not edit the frozen contract" directive and its fork is rebuilt from the current head."""
    bb = file_ownership.bb
    decisions = contract_event.pending_decisions(board.get("exceptions") or [],
                                                 bb.read_facts(cid, kind=contract_event.CONTRACT_EVENT_KIND))
    applied = []
    for dec in decisions:
        gid, by = dec["item"], dec["by"]
        attempt = int(_mapping(board["items"].get(gid)).get("attempt") or 0)
        team_board.set_state(cid, "lead", gid, team_board.REOPENED, attempt=attempt + 1,
                             reason="contract change rejected")
        team_board.post_directive(
            cid, by, gid, "Do NOT edit the frozen contract; build against it as-is. " + (dec["text"] or ""),
            authority="agent")
        proposals = [_mapping(f.get("data")) for f in bb.read_facts(cid, kind="contract_proposal")
                     if _mapping(f.get("data")).get("item") == gid]
        ref = str((proposals[-1] if proposals else {}).get("candidate") or gid)
        git.worktree_remove(repo, ref)
        git.git(repo, "branch", "-D", git.group_branch(repo, ref), check=False)
        bb.post_fact(cid, "lead", contract_event.CONTRACT_EVENT_KIND,
                     {"item": gid, "decision": "reject", "applied": True, "outcome": "rejected; re-dispatched"})
        applied.append({"item": gid, "decision": "reject", "outcome": "rejected; re-dispatched"})
    return applied


def _denied_writes(cid: str | None) -> dict[str, list[str]]:
    """Per item, the files its latest implementer session was refused as outside its write roots
    (``team_integrate`` records them on the ``session`` fact; the latest session per item wins)."""
    out: dict[str, list[str]] = {}
    if cid:
        for f in file_ownership.bb.read_facts(cid, kind="session"):
            d = _mapping(f.get("data"))
            out[str(d.get("item"))] = [str(p) for p in _list(d.get("deniedWrites")) if str(p).strip()]
    return out


def _team_span(items: dict) -> set[str]:
    """The directories a team's items span (pure): the team's write authority ends there. A root-level
    file spans only the root."""
    return {os.path.dirname(str(p).strip()) for it in items.values() for p in _list(it.get("files"))
            if str(p).strip()}


def _in_span(path: str, span: set[str]) -> bool:
    d = os.path.dirname(path)
    return any((s == "" and d == "") or (s != "" and (d == s or d.startswith(s + "/"))) for s in span)


def _widen_scope(items: dict, sid: str, files: list[str], denied: list[str],
                 contract_paths: list[str], foreign_open: set[str] | None = None) -> tuple[list[str], list[str], list[str]]:
    """The Lead's scope decision for a re-dispatched item (pure): ``(widen, held_by, refused)``.

    An implementer refused a file outside its write roots is evidence that the plan sliced the item
    too narrowly — st-lru's criteria needed eviction hooks in ``store.go``, owned by st-core; every
    attempt was denied there and the item burned its review rounds on work-arounds until the whole
    program halted (run fc6467b7). The file is added to the item's scope once no *open* item still
    owns it (a done or blocked owner's claim is over); while one does, the item waits for that owner
    to land, exactly like ownership contention. A frozen-contract path is never widened: editing the
    contract is a controlled event with its own path. A path outside the directories this team's
    items span is refused, not widened: the design team's d-store (a documents item) was widened to
    ``store/errors.go`` and ``store/store.go`` and took ownership of code the breakdown then assigned
    to the store team (run a33d7061) — writing outside the team's subsystem is the program's call."""
    widen, held_by, refused = [], [], []
    span = _team_span(items)
    for path in denied:
        if path in files or path in contract_paths:
            continue
        if not _in_span(path, span) and (foreign_open is None or path in foreign_open):
            # Outside the team's directories AND a subsystem still being built owns it (or the program
            # is unknown): the program's call. A file of a subsystem already DELIVERED has no live owner:
            # ce-zset-inf-scores needed one line in internal/store/zsets.go, whose team was long done,
            # was refused three rounds and the gap waited for the final repair (run 351812b2).
            if path not in refused:
                refused.append(path)
            continue
        holders = sorted(oid for oid, it in items.items() if oid != sid and path in _list(it.get("files"))
                         and it.get("state") in team_board.OPEN_STATES)
        # A holder that cannot start until THIS item lands (it depends on it, directly or through
        # others) would be waited for forever: st-stats owned stats.go and depended on st-core, which
        # needed a Stats stub there — the team spun 14 empty waves to maxWaves (run a4f1ff1f). Such a
        # wait is a cycle; widen now, and the holder lands its own version on top when its turn comes.
        holders = [o for o in holders if sid not in _transitive_deps(items, o)]
        if holders:
            held_by.append(f"{sid} waits for {', '.join(holders)} to land {path}")
        elif path not in widen:
            widen.append(path)
    return widen, held_by, refused


# ─────────────────────────────── team_standup ─────────────────────────────────
@worker_task(task_definition_name="team_standup")
def team_standup(task):
    """The Lead's standup: fold the board, size this wave from dependency-ready work, gate dispatch on
    shared-file ownership, and dispatch implementer forks with the live board + lessons + directives
    injected into each prompt. Items blocked by ownership or not selected this wave stay on the board."""
    i = task.input_data or {}
    try:
        shared_repo = str(i.get("repoPath") or "").strip()
        cid = str(i.get("correlationId") or "").strip() or None
        # A team its program re-runs starts its agents one tier up (program_rank_build: baseModelTier).
        base_tier = max(int(i.get("baseModelTier") or 0), 0)
        contract_paths = _board_contract_paths(
            cid, [str(p).strip() for p in _list(i.get("contractPaths")) if str(p).strip()])
        max_width = max(int(i.get("maxWidth") or 6), 1)
        best_of = max(int(i.get("bestOf") or 1), 1)
        budget_state = str(i.get("budgetState") or "ok")

        board = team_board.fold_board(cid) if cid else {"items": {}, "lessons": [], "reviews": [], "directives": []}
        decisions = []
        board["directives"] += _parent_directives(i)
        if cid and shared_repo and os.path.isdir(shared_repo):
            decisions = _apply_contract_decisions(cid, shared_repo, board)
            if decisions:
                parent_dirs = [d for d in board["directives"] if d.get("target") == "team"]
                board = team_board.fold_board(cid)
                board["directives"] += [d for d in parent_dirs if d not in board["directives"]]
        if cid:
            file_ownership.bb.post_fact(cid, "lead", "wave", {"readyAt": time.time()})
        board["directives"] = _steering(cid, board)
        board["lessons"] = team_board.live_lessons(board.get("lessons") or [],
                                                   repo=shared_repo if shared_repo and os.path.isdir(shared_repo) else None)
        # ONE board per run: what every team and agent posted on the program's board (lessons, and the
        # steers addressed to everyone or to this team) reaches this team's briefs too.
        run_cid = str(i.get("parentCorrelationId") or "").strip()
        if run_cid and run_cid != cid:
            rb = team_board.fold_board(run_cid)
            board["lessons"] = board["lessons"] + team_board.live_lessons(rb.get("lessons") or [])
            mine = {"run", "team", f"team:{str(i.get('subsystemId') or '')}"}  # "team" on the run board = everyone
            board["directives"] = board["directives"] + [
                {**d, "target": "team"} for d in team_board.with_authority(team_board.active_directives(rb.get("directives") or []))
                if str(d.get("target")) in mine]
        items = board["items"]
        # Acceptance tests are authored BEFORE the first wave (team_build: acceptance_build precedes
        # wave_loop). Authored beside it, the frozen test always landed after its implementer started:
        # it was in neither the prompt nor the fork's own test cycle, and re-opened the item for a
        # whole second build-and-review round (runs 14b40b1a, a8167484, 0f829d58).
        rollup = team_board.board_rollup(board)
        ready = team_board.ready_items(items)
        wave_n = team_board.size_wave(len(ready), max_width=max(max_width // best_of, 1),
                                      budget_state=budget_state)
        lesson_digest = _lesson_digest(board["lessons"])
        # Learnings from earlier runs' retrospectives: this repo, this host's toolchain, its languages.
        learned = retrospective.digest(retrospective.relevant(
            repo=retrospective.repo_key(shared_repo), languages=retrospective.detect_languages(shared_repo),
            host_name=retrospective.host(), limit=12)) if shared_repo else ""

        dynamic_tasks, dynamic_inputs, group_ids, dispatched = [], {}, [], []
        wave_head = git.head(shared_repo) if shared_repo and os.path.isdir(shared_repo) else ""
        # The program's definition of done, read from the branch: the same text the test author and
        # the reviewers read, so the interface is decided once, by the checks, not by each agent.
        done_section = _done_section(shared_repo)
        contract_rev = (contract_event.current_contract(
            file_ownership.bb.read_facts(cid, kind=contract_event.CONTRACT_KIND))["revision"] if cid else 0)
        # One ownership fold per standup; claims made this wave are simulated on the in-memory map. Under
        # a program (H3) ownership is PROGRAM-scoped, so two teams never hold the same file.
        parent = str(i.get("parentCorrelationId") or "").strip()
        own_cid = parent or cid
        owners = file_ownership.owner_map(own_cid) if own_cid else {}
        denied = _denied_writes(cid)
        rescued_items = _rescue_map(cid)
        # F: help climbs one tier per helper, up to maxRescues tiers, then stays at the strongest reached.
        tiers_up = max(int(i.get("maxRescues") if i.get("maxRescues") not in (None, "") else 2), 0)
        # Hill climbing across items (common/rung_policy): each item starts on the cheapest rung of the
        # code ladder that has got its kind of work done on this repository, probing one cheaper after a
        # success; only agent-fixable failures climb.
        ladder_top = _code_ladder_top(i, shared_repo)
        outcome_repo = rung_policy.repo_key(shared_repo) if shared_repo else ""
        scope_waits: list[str] = []
        scope_refusals: list[str] = []
        # Files other subsystems of the program still own (not DELIVERED): the only paths outside this
        # team's directories it may not widen to. Without a program, every outside path is refused.
        foreign_open: set[str] | None = None
        foreign_owner: dict[str, str] = {}
        if parent:
            foreign_open = set()
            for oid, it in team_board.fold_board(parent)["items"].items():
                if oid != str(i.get("subsystemId") or "") and it.get("state") != team_board.DONE:
                    for path in _list(it.get("files")):
                        foreign_open.add(str(path))
                        foreign_owner[str(path)] = oid
        for entry in ready[:wave_n]:
            sid = str(entry.get("id"))
            files = [str(p).strip() for p in _list(entry.get("files")) if str(p).strip()]
            attempt = int(entry.get("attempt") or 0)
            # Help for a stuck item may write anywhere, so it is never held or refused on scope: the
            # foundation item's fix sat in internal/store/zsets.go, outside its team's directories, and
            # both helpers were refused there until it blocked (run 41fddf7a).
            helps = rescued_items.get(sid, [])
            widen, held_by, refused = ([], [], []) if helps else _widen_scope(items, sid, files, denied.get(sid, []),
                                                                              contract_paths, foreign_open)
            for path in refused:
                mech = (f"refused to widen {sid}'s write scope to {path}: outside the directories this "
                        f"team's items span ({', '.join(sorted(d or '.' for d in _team_span(items)))})"
                        + (f" and owned by a subsystem still being built ({foreign_owner.get(path, '?')})" if foreign_open else ""))
                scope_refusals.append(mech)
                if cid and not any(_mapping(e).get("detail") == mech for e in (board.get("exceptions") or [])):
                    team_board.post_exception(cid, "lead", sid, "write_scope", mech, severity="info")
                    team_board.resolve_exception(cid, "lead", sid, "write_scope", mech)
            if held_by:
                scope_waits.extend(held_by)
                continue  # the file's owner has not landed; the item waits for the next wave
            if widen:
                files = files + widen
                mech = (f"widened {sid}'s write scope to {', '.join(widen)}: its implementer was refused "
                        f"writing there and no open item that could land first owns it")
                # The widened item now owns the file for the rest of this standup too: a later item
                # refused the same file waits for it instead of being widened alongside (four store
                # items were widened to store/store.go in one standup and edited it concurrently,
                # run a062184f).
                items[sid] = {**_mapping(items.get(sid)), **entry, "files": files}
                if cid:
                    team_board.post_item(cid, "lead", {**entry, "files": files})
                    team_board.post_exception(cid, "lead", sid, "write_scope", mech, severity="info")
                    team_board.resolve_exception(cid, "lead", sid, "write_scope", mech)
            if cid:
                claimant = f"{cid}:{sid}" if parent else sid
                if file_ownership.blocked_by(owners, claimant, files):
                    continue  # a free slot this wave is contended; the item waits for the next wave
                for path in files:
                    file_ownership.post_claim(own_cid, claimant, path)
                    owners.setdefault(path, claimant)
                if parent:
                    # Another team may have claimed concurrently: confirm against the shared log.
                    live = file_ownership.owner_map(own_cid)
                    if any(live.get(p) != claimant for p in files):
                        for path in files:
                            file_ownership.release(own_cid, claimant, path)
                        owners = live
                        continue
                team_board.set_state(cid, "lead", sid, team_board.IN_PROGRESS, assignee=sid, attempt=attempt)
            directive_digest = _directive_digest(board["directives"], sid)
            acceptance = "\n".join(f"- {c}" for c in _list(entry.get("acceptanceCriteria")))
            # An item that carries directives (findings) is a REVISION: its code already exists on the
            # branch and was returned by review. Lead with the required changes so the agent revises the
            # existing code instead of treating the (already-satisfied) original task as done and not editing.
            header = (
                f"This work item was RETURNED by peer review / a human and MUST be revised. The code in "
                f"{', '.join(files) or 'the target files'} already exists and already passes the automated "
                f"tests — that is NOT sufficient. The required changes below are deliberately NOT covered by "
                f"the tests; you MUST make every one of them by editing the code. Do NOT report 'no changes "
                f"needed' or stop because the tests pass — an unaddressed directive is a failed task.\n\n"
                f"Required changes (make every one, then verify the code reflects them):\n{directive_digest}\n\n"
                f"Task context: {entry.get('description') or entry.get('title') or ''}\n"
            ) if directive_digest else (
                f"{entry.get('description') or entry.get('title') or ''}\n"
            )
            docs_item = team_board.deliverable_of(entry) == "docs"
            deliverable_note = (
                "\nDELIVERABLE: DOCUMENTS, not code. Write the documents in the target files (Markdown unless "
                "a file says otherwise). Do not write or change source code or tests. Make them specific "
                "enough that other teams can build from them without guessing: concrete names, interfaces, "
                "data formats, error behaviour, edge cases, and the reasoning behind each decision.\n"
                if docs_item else "")
            frozen_tests = _list(entry.get("acceptanceTests"))
            prompt_key = "team_implement"
            prompt_context = {
                "header": header, "deliverableNote": deliverable_note, "files": ", ".join(files) or "(none)",
                "contractPaths": ", ".join(contract_paths) or "(none)", "doneSection": done_section,
                "acceptanceSection": f"\nAcceptance criteria for this item:\n{acceptance}\n" if acceptance else "",
                "frozenTestsSection": (
                    f"\nFROZEN ACCEPTANCE TESTS for this item, written by a different agent: "
                    f"{', '.join(frozen_tests)}. They are the check: make them pass. "
                    f"Never weaken them to fit your code. Only if one is genuinely WRONG (it contradicts the design "
                    f"documents, the frozen contract or the acceptance criteria) correct that assertion in the "
                    f"same change and explain why, citing the source, in your summary: a peer reviewer then "
                    f"decides whether the amendment stands.\n"
                    + ("This test ALREADY PASSES on the branch and the item's files already exist: first verify "
                       "whether every acceptance criterion is genuinely met by the code as it is. If it is, report "
                       "alreadySatisfied with the evidence and change nothing; if the test passes only because it "
                       "is too weak, strengthen it (an amendment the reviewer decides) and implement what is "
                       "missing.\n" if entry.get("greenBeforeWork") else "") if frozen_tests else ""),
                "boardSection": f"\nTeam board: {_rollup_line(rollup)}.\n" if rollup.get("total") else "",
                "lessonsSection": (f"\nLessons from teammates (build on these, do not repeat their mistakes):\n"
                                   f"{lesson_digest}\n" if lesson_digest else ""),
                "learnedSection": (f"\nLearnings from earlier runs on this repository, machine and language "
                                   f"(apply them):\n{learned}\n" if learned else ""),
            }
            prompt = templating.render_default(prompt_key, prompt_context)
            # Help for a stuck item (``_rescue``): a stronger teammate, briefed with everything tried so far.
            kind = rung_policy.item_kind(entry)
            start, rung_reason = (rung_policy.start_rung(outcome_repo, role="code", kind=kind, top=ladder_top)
                                  if outcome_repo else (0, "no repository to learn from"))
            climbs = [h for h in helps if rung_policy.agent_fixable(h.get("cause"))]
            model_tier = min(base_tier + start + min(len(climbs), tiers_up), ladder_top)
            if climbs:
                rung_reason = (f"{rung_reason}; climbed {min(len(climbs), tiers_up)} rung(s) for being stuck "
                               f"({climbs[-1].get('cause') or 'stuck'})")
            if base_tier:
                rung_reason += f"; the program re-runs this team {base_tier} rung(s) up"
            if helps:
                prompt_key = "team_rescue"
                prompt_context = {"item": sid, "mechanism": helps[-1].get("mechanism"),
                                  "history": _item_history(cid, sid), "brief": prompt}
                prompt = templating.render_default(prompt_key, prompt_context)
            # Best-of-N (H12a): N independent candidate forks of the same item, one ownership claim; the
            # integrator keeps the best and discards the rest. bestOf=1 is the plain single fork.
            refs = [sid] if best_of <= 1 else [f"{sid}_c{k}" for k in range(1, best_of + 1)]
            group_ids.extend(refs)
            dispatched.append({"id": sid, "description": entry.get("description") or entry.get("title") or "",
                               "files": files, "attempt": attempt, "candidates": refs})
            if cid:
                # Provenance (§7 #6): exactly what each agent was handed — base commit, contract revision,
                # prompt hash, model, and WHICH lessons/directives were injected — so any change can be
                # traced back to the inputs that produced it.
                digest_lessons = [les for les in list(board["lessons"])[-8:] if str(les.get("text") or "").strip()]
                digest_dirs = [d for d in board["directives"] if str(d.get("target")) in (sid, "team")
                               and sid not in (d.get("exempt") or [])]
                for ref in refs:
                    file_ownership.bb.post_fact(cid, "lead", "dispatch", {
                        "item": sid, "candidate": ref, "attempt": attempt, "waveBase": wave_head,
                        "rung": model_tier, "kind": kind, "rungReason": rung_reason,
                        "contractRevision": contract_rev, "model": str(i.get("codeModel") or ""),
                        "promptSha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "lessons": [{"ts": les.get("ts"), "text": str(les.get("text"))[:160],
                                     "kind": les.get("kind") or "verified"} for les in digest_lessons],
                        "directives": [{"id": d.get("id"), "by": d.get("by"), "authority": d.get("authority"),
                                        "text": str(d.get("text"))[:160]} for d in digest_dirs],
                        "acceptanceTests": _list(entry.get("acceptanceTests"))})
            for ref in refs:
                dynamic_tasks.append({"name": "code_subtask", "taskReferenceName": ref, "type": "SUB_WORKFLOW",
                                      "optional": True, "subWorkflowParam": {"name": "code_subtask"}})
                dynamic_inputs[ref] = {
                  "repoPath": shared_repo, "name": ref, "model": i.get("codeModel") or "",
                  "maxTurns": i.get("maxTurns") or 250, "maxBudgetUsd": i.get("maxBudgetUsd") or 10,
                  "prompt": prompt, "promptTemplate": "", "promptTemplateSource": "",
                  "promptPath": prompt_files.stash(shared_repo, ref, prompt, prompt_context),
                  # The brief is the item's own default prompt. Under templateKey "code" with a "subtask"
                  # context the resolver sent only the item description: target files, frozen contract,
                  # acceptance tests, lessons and the report format never reached the implementer.
                  "templateKey": prompt_key, "promptContext": prompt_context,
                  "specContextPath": "", "contextPaths": [],
                  "allowedTools": CODE_SUBTASK_TOOLS,
                  # Its own frozen acceptance test is writable only so a WRONG test can be amended; that edit
                  # is never merged unless the peer reviewer approves it (team_integrate / apply_reviews).
                  # Help is unscoped (empty): it fixes the root cause wherever that is.
                  "allowedWriteRoots": [] if helps else files + [p for p in _list(entry.get("acceptanceTests"))
                                                                 if p not in files],
                  # ...and the delivery audit never counts that untouched check as a missing delivery.
                  "acceptanceTests": _list(entry.get("acceptanceTests")),
                  "modelProfile": i.get("modelProfile") or "", "modelPolicy": i.get("modelPolicy") or {},
                  "modelPolicySource": i.get("modelPolicySource") or "",
                  "modelPolicySha256": i.get("modelPolicySha256") or "",
                  "modelsConfig": i.get("modelsConfig") or "",
                  "modelOverrides": i.get("modelOverrides") or {},
                  # The item's own green gate is its frozen acceptance tests when it has them (H13).
                  "testCommands": _list(entry.get("testCommands")),
                  "outputSchema": IMPLEMENTER_SCHEMA,
                  "sandboxChecks": _truth(i.get("sandboxChecks"), True),
                  # In-session feedback (H11): the running agent takes new directives for this item after
                  # every tool call; anything older than this prompt is already in it.
                  "feedbackCorrelationId": cid or "", "feedbackItem": sid, "feedbackSince": time.time(),
                  # ONE board per run: lessons, questions, decisions and steers live on the program's board.
                  "feedbackRunId": (str(i.get("parentCorrelationId") or "").strip() or cid or ""),
                  # ONE working tree per team: no fork; the item commits only its own files, atomically.
                  "sharedTree": True,
                  "feedbackTeam": str(i.get("subsystemId") or ""),
                  "feedbackEveryTurns": int(i.get("feedbackEveryTurns") if i.get("feedbackEveryTurns")
                                            not in (None, "") else 1),
                  "checkpointEveryTurns": int(i.get("checkpointEveryTurns") or 0),
                  "modelTier": model_tier,
                }
        wave_base = git.head(shared_repo) if shared_repo and os.path.isdir(shared_repo) else ""
        return ok(task, {
            "dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
            "groupIds": ",".join(group_ids), "dispatched": dispatched, "contractPaths": contract_paths,
            "waveBase": wave_base, "dispatchedCount": len(group_ids), "readyCount": len(ready),
            "teamSize": wave_n, "rollup": rollup, "drainedBefore": team_board.is_drained(items),
            "contractDecisions": decisions, "scopeWaits": scope_waits, "scopeRefusals": scope_refusals,
        }, [f"[team_standup] ready={len(ready)} teamSize={wave_n} dispatched={len(group_ids)} "
            f"contractDecisions={len(decisions)} scopeWaits={len(scope_waits)} "
            f"scopeRefusals={len(scope_refusals)} budget={budget_state} board={_rollup_line(rollup)}"]
           + [f"[team_standup] {w}" for w in scope_waits + scope_refusals])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_standup", e)


# ─────────────────────────────── team_integrate ───────────────────────────────
CHECK_REOPEN = "integration_check_red"
ACCEPTANCE_SETTLED_KIND = "acceptance_settled"  # the frozen tests (or none) are on the branch; waves may drain
RESCUE_KIND = "rescue"


def _rescue_map(cid: str | None) -> dict[str, list[dict]]:
    """Every item's help so far, one read of the rescue log per step."""
    out: dict[str, list[dict]] = {}
    for f in (file_ownership.bb.read_facts(cid, kind=RESCUE_KIND) if cid else []):
        d = _mapping(f.get("data"))
        out.setdefault(str(d.get("item")), []).append({**d, "ts": f.get("ts")})
    return out


def _rescues(cid: str | None, gid: str) -> list[dict]:
    if not cid:
        return []
    return [{**_mapping(f.get("data")), "ts": f.get("ts")} for f in file_ownership.bb.read_facts(cid, kind=RESCUE_KIND)
            if str(_mapping(f.get("data")).get("item")) == str(gid)]


def _rescue(cid: str | None, gid: str, mechanism: str, *, max_rescues: int, cause: str = "") -> bool:
    """A swarm helps the agent that is struggling instead of giving up on its item. Where an item would
    block — attempts exhausted, review rounds exhausted with findings unaddressed, a pushback that could
    not be resolved, contract churn — the Lead reopens it with a fresh attempt budget and the next standup
    hands it to a STRONGER teammate (the next model tier) together with everything the item went through
    (``_item_history``): every attempt, review finding, failure output, pushback and refused file. The
    swarm never gives up on an item: help keeps coming until the work lands or the run reaches its budget
    or wave limit. ``max_rescues`` is how many tiers up help may climb (one per helper, then the strongest
    reached, see ``team_standup``); 0 turns help off. Returns True when the item was rescued."""
    if not cid or max_rescues <= 0:
        return False
    n = len(_rescues(cid, gid))
    # The cause decides whether the helper runs a rung higher (common/rung_policy): a session that never
    # ran or a harness fault retries on the same rung, since a stronger model cannot fix it.
    cause = cause or str(mechanism).split(":", 1)[0].strip()
    file_ownership.bb.post_fact(cid, "lead", RESCUE_KIND, {"item": gid, "rescue": n + 1, "cause": cause,
                                                          "mechanism": str(mechanism)[:1500]})
    team_board.set_state(cid, "lead", gid, team_board.REOPENED, attempt=0,
                         reason=f"stuck: {str(mechanism)[:240]} — a stronger teammate takes it over with its "
                                f"full history (help {n + 1})")
    note = f"'{gid}' got stuck ({str(mechanism)[:300]}); a stronger teammate takes it over (help {n + 1})"
    team_board.post_exception(cid, "lead", gid, "rescue", note, severity="info")
    team_board.resolve_exception(cid, "lead", gid, "rescue", note)
    return True


def _code_ladder_top(i: dict, repo: str) -> int:
    """Index of the code role's last rung in this run's model profile (2 when it cannot be read)."""
    try:
        from common import model_policy
        return max(len(model_policy.resolve_model_policy(i, worktree=repo or None)["roles"]["code"]["tiers"]) - 1, 0)
    except Exception:  # noqa: BLE001 - an unreadable policy fails at the agent, not at the standup
        return 2


def _record_rung_outcomes(cid: str | None, repo: str, items: dict) -> int:
    """Teach the ladder (common/rung_policy) what this team's items show: the rung each done item was
    finished on, and the rung each agent-fixable failure happened on. Returns how many were recorded."""
    if not cid or not repo:
        return 0
    key = rung_policy.repo_key(repo)
    dispatch, rescue, reopen = {}, {}, {}
    for kind, into in (("dispatch", dispatch), (RESCUE_KIND, rescue), ("reopen", reopen)):
        for f in file_ownership.bb.read_facts(cid, kind=kind):
            d = _mapping(f.get("data"))
            into.setdefault(str(d.get("item")), []).append({**d, "ts": f.get("ts") or 0})
    n = 0
    for gid, it in items.items():
        if it.get("kind", "subsystem") == "subsystem" and gid not in dispatch:
            continue
        sent = dispatch.get(gid) or []
        kinds = [d.get("kind") for d in sent if d.get("kind")]
        if not kinds:
            continue
        last_reopen = sorted(reopen.get(gid) or [], key=lambda d: d["ts"])[-1:] or [{}]
        for o in rung_policy.outcomes_from_board(dispatches=sent, rescues=rescue.get(gid) or [],
                                                 final_state=str(it.get("state") or ""),
                                                 blocked_reason=str(last_reopen[0].get("reason") or "")):
            rung_policy.record(key, role="code", kind=kinds[-1], rung=o["rung"], ok=o["ok"], cause=o["cause"],
                               item=gid, run=cid)
            n += 1
    return n


def _item_history(cid: str | None, gid: str, limit: int = 7000) -> str:
    """Everything one item went through, oldest first, in a few lines per event: what the helper reads."""
    if not cid:
        return ""
    events = []
    for kind in ("dispatch", "session", "review", "retry", "reopen", "directive", RESCUE_KIND, "acceptance_amendment"):
        for f in file_ownership.bb.read_facts(cid, kind=kind):
            d = _mapping(f.get("data"))
            target = str(d.get("item") or d.get("target") or "")
            if target != str(gid):
                continue
            if kind == "dispatch":
                line = f"dispatched attempt {int(d.get('attempt') or 0) + 1}" + (f" ({d.get('model')})" if d.get("model") else "")
            elif kind == "session":
                pb = _mapping(d.get("pushback"))
                line = (f"implementer finished: delivery {d.get('deliveryOutcome') or '?'}, own tests "
                        f"{'passed' if d.get('testsPassed') else 'not passed'}"
                        + (f"; refused writes {_list(d.get('deniedWrites'))}" if _list(d.get("deniedWrites")) else "")
                        + (f"; pushback: {pb.get('reason')}" if pb.get("reason") else "")
                        + "".join(f"; noted: {str(_mapping(x).get('text'))[:200]}" for x in _list(d.get("lessons"))[:3]))
            elif kind == "review":
                line = f"review {d.get('verdict')}" + "".join(f"\n    - {str(x)[:600]}" for x in _list(d.get("findings")))
            elif kind in ("retry", "reopen"):
                line = f"{d.get('reason')}: {str(d.get('mechanism') or '')[:800]}"
            elif kind == "directive":
                line = f"directive from {d.get('by') or f.get('author')}: {str(d.get('text') or '')[:800]}"
            elif kind == RESCUE_KIND:
                line = f"handed to a stronger teammate (help {d.get('rescue')}): {str(d.get('mechanism') or '')[:400]}"
            else:
                line = f"acceptance-test amendment {d.get('decision') or 'proposed'}: {str(d.get('reason') or '')[:300]}"
            events.append((float(f.get("ts") or 0), f"- {line}"))
    events.sort(key=lambda e: e[0])
    text = "\n".join(line for _, line in events)
    return text if len(text) <= limit else "…(earlier events omitted)\n" + text[-limit:]


def _integration_check(cid: str | None, repo: str, head: str, items: dict, *, sandbox: bool,
                       since: str = "", landed: list | None = None) -> dict:
    """Run every integrated item's frozen acceptance tests at the integrated head (H13 on the shared
    branch, not only on each fork). A fork proves its own test in isolation; a sibling landing later can
    falsify an assumption the test was written with — sv-core's test asserted PING is an unknown
    command, sv-connection then registered PING — and the final test cycle ran only the repository's
    `make test`, which never executes the acceptance suite: the program delivered 6/6 with 2 of its 290
    frozen tests red at the head (run 299b4907). Items in ``review``/``done`` whose fork is not held
    for an amendment are checked; ``red`` maps a failing item to the tail of its failing output.
    Given ``since`` (the head before this wave's merges) and ``landed`` (the items merged), only the
    items this wave can have affected are checked: the landed ones, those whose files or frozen test
    the wave's diff touched, and those depending on a landed item — every integrated item's tests at
    every wave made test time grow with the square of the item count, and a wave that lands one
    leaf cannot falsify a test of an item it did not touch."""
    changed: set[str] | None = None
    if since and head and repo and os.path.isdir(repo):
        diff = git.git(repo, "diff", "--name-only", since, head, check=False)
        if diff.code == 0:
            changed = {p.strip() for p in diff.stdout.splitlines() if p.strip()}
    landed_ids = {str(g) for g in _list(landed)}

    def touched(it: dict) -> bool:
        if changed is None or landed is None or (not changed and not landed_ids):
            return True  # no wave delta known, or a wave that moved nothing: check everything (the old rule)
        if str(it.get("id")) in landed_ids or landed_ids & {str(d) for d in _list(it.get("dependsOn"))}:
            return True
        own = [str(p) for p in _list(it.get("files"))] + [str(p) for p in _list(it.get("acceptanceTests"))]
        return any(c == f or c.startswith(f.rstrip("/") + "/") for c in changed for f in own if f)

    targets: dict[str, dict] = {}
    for gid, it in sorted(items.items()):
        if it.get("state") not in (team_board.REVIEW, team_board.DONE):
            continue
        paths = [str(p) for p in _list(it.get("acceptanceTests"))]
        commands = [_mapping(c) for c in _list(it.get("testCommands")) if _list(_mapping(c).get("argv"))]
        if not paths or not commands or (cid and _pending_amendment(cid, gid)) or not touched({**it, "id": gid}):
            continue
        targets[gid] = {"paths": paths, "commands": commands}
    if not targets or not repo or not head or not os.path.isdir(repo):
        return {"state": "skipped", "checked": [], "red": {}}
    seen: set[tuple] = set()
    commands = []
    for t in targets.values():
        for c in t["commands"]:
            argv = [str(a) for a in _list(c.get("argv"))]
            if tuple(argv) not in seen:
                seen.add(tuple(argv))
                commands.append({"argv": argv, "source": "acceptance", "kind": "acceptance"})
    try:
        run = verification.verify_candidate(repo, head, commands, scope="changed", sandbox=sandbox)
    except verification.VerificationBlocked as exc:
        return {"state": "blocked", "reason": str(exc), "checked": sorted(targets), "red": {}}
    if run.get("verificationState") == "blocked":
        return {"state": "blocked", "reason": str(run.get("executionOutcome") or "blocked"),
                "checked": sorted(targets), "red": {}}
    red: dict[str, str] = {}
    for gid, t in targets.items():
        naming = [_mapping(c) for c in _list(run.get("commands"))
                  if any(p in str(a) for p in t["paths"] for a in _list(_mapping(c).get("argv")))]
        failed = [c for c in naming if int(c.get("exitCode") or 0) != 0]
        if failed:
            red[gid] = str(failed[0].get("output") or "")[-1500:]
    return {"state": "failed" if red else "passed", "checked": sorted(targets), "red": red}


def _shared_delivery_base(repo: str, dispatched_at: str, files: list[str], head: str) -> str:
    """Where a shared-tree item's delivery range starts, for its reviewer's diff: the shared head when it
    was dispatched, so the range holds its own commit (teammates' commits in that range touch other
    files, and the reviewer reads this item's files). With no dispatch record, the parent of the
    newest commit that touched its files; with none, an empty range at ``head`` (the reviewer then
    judges the files on the tree)."""
    if dispatched_at and git.git(repo, "merge-base", "--is-ancestor", dispatched_at, head, check=False).code == 0:
        return dispatched_at
    newest = git.git(repo, "log", "-1", "--format=%H", head, "--", *files, check=False).stdout.strip() if files else ""
    if newest:
        parent = git.git(repo, "rev-parse", "--verify", "--quiet", f"{newest}^", check=False).stdout.strip()
        return parent or head
    return head


@worker_task(task_definition_name="team_integrate")
def team_integrate(task):
    """Integrate the wave by continuous rebase (no big-bang merge), update the tracking board, record
    spend, release ownership. Clean deliveries move to ``review`` (peer review gates ``done``, H13);
    empty/conflicting deliveries are re-queued (``reopened``) under the attempt cap or ``blocked``."""
    i = task.input_data or {}
    try:
        shared_repo = str(i.get("sharedRepo") or "").strip()
        shared_branch = str(i.get("sharedBranch") or "").strip()
        cid = str(i.get("correlationId") or "").strip() or None
        contract_paths = [str(p).strip() for p in _list(i.get("contractPaths")) if str(p).strip()]
        group_ids = [g.strip() for g in str(i.get("groupIds") or "").split(",") if g.strip()]
        dispatched = _list(i.get("dispatched"))
        max_attempts = max(int(i.get("maxAttempts") or 2), 1)
        max_contract_events = max(int(i.get("maxContractEvents") or 2), 1)
        peer_review = _truth(i.get("enablePeerReview"), True)
        by_id = {str(_mapping(d).get("id")): _mapping(d) for d in dispatched}
        prior_exc = team_board.fold_board(cid)["exceptions"] if cid else []

        eligible, failures, contract_events, interrupted = [], [], [], []
        best_of_picks, discarded, pushbacks, shared_lessons, satisfied, claimed = [], [], [], 0, [], []
        joined_by_ref = {re.sub(r"__[0-9]+$", "", str(ref)): out for ref, out in _iter_joined(i.get("joined"))}
        # Items this wave: each with its candidate fork refs (best-of-N) — a plain fork is one candidate.
        wave = [_mapping(d) for d in dispatched] or [{"id": g, "candidates": [g]} for g in group_ids]
        for idx, item in enumerate(wave):
            gid = str(item.get("id"))
            refs = [str(r) for r in _list(item.get("candidates"))] or [gid]
            given = []  # the directives this item's agents were dispatched with (the dispatch record)
            given_ids: dict[str, str] = {}
            dispatched_at = ""  # the shared head when this item's latest dispatch went out
            if cid:
                for f in file_ownership.bb.read_facts(cid, kind="dispatch"):
                    d = _mapping(f.get("data"))
                    if d.get("item") == gid:
                        dispatched_at = str(d.get("waveBase") or "")
                        given = [str(_mapping(x).get("text") or "") for x in _list(d.get("directives"))]
                        given_ids = {str(_mapping(x).get("text") or ""): str(_mapping(x).get("id") or "")
                                     for x in _list(d.get("directives"))}
            cands = []
            for ref in refs:
                out = _mapping(joined_by_ref.get(ref))
                worktree = _subtask_worktree(shared_repo, ref)
                branch = git.group_branch(shared_repo, ref)
                ahead, changed, diff_lines, shared = "0", [], 0, False
                if not os.path.isdir(worktree):
                    # ONE tree per team (git.worktree_shared): no fork and no branch of its own. The item
                    # was built in the shared tree and committed its own files there atomically, so its
                    # delivery is already ON the shared branch: integrating it is recording it, never a
                    # rebase. The delivery audit judged it, and its verdict is the delivery: the files
                    # this session committed plus the planned files an earlier attempt already committed.
                    # Rebasing the shared tree onto its own branch failed on teammates' uncommitted files
                    # and re-opened five delivered items as "conflicts" with no conflicting file; their
                    # re-dispatch had nothing left to commit and looped as "empty delivery" for an hour
                    # while every audit said passed (run 836437d7).
                    worktree, branch, shared = shared_repo, shared_branch, True
                    staged = [str(p) for p in _list(out.get("stagedPaths")) if str(p).strip()]
                    present = [str(p) for p in _list(out.get("presentPaths")) if str(p).strip()]
                    changed = sorted({*staged, *present}) if str(out.get("deliveryOutcome") or "") == "passed" \
                        else [str(p) for p in _list(out.get("filesChanged")) if str(p).strip()]
                    ahead = "1" if changed else "0"
                elif os.path.isdir(worktree):
                    ahead = git.git(worktree, "rev-list", "--count", f"{shared_branch}..{branch}",
                                    check=False).stdout.strip() or "0"
                    names = git.git(worktree, "diff", "--name-only", f"{shared_branch}...{branch}",
                                    check=False).stdout
                    changed = [n for n in names.split("\n") if n.strip()]
                    stat = git.git(worktree, "diff", "--numstat", f"{shared_branch}...{branch}", check=False).stdout
                    diff_lines = sum(int(a) + int(b) for a, b, *_ in (ln.split("\t") for ln in stat.splitlines())
                                     if a.isdigit() and b.isdigit())
                cands.append({
                    "ref": ref, "worktree": worktree, "branch": branch, "changed": changed, "shared": shared,
                    "delivered": ahead != "0" and bool(changed),
                    "touchesContract": bool(contract_paths) and bool(changed)
                    and not contract_freeze.is_integration_eligible(changed, contract_paths),
                    "interruptedBy": out.get("interruptedBy"),
                    "pushback": _honored_pushback(_implementer_report(out).get("pushback"), given),
                    "unmatchedPushback": (_mapping(_implementer_report(out).get("pushback"))
                                          if _implementer_report(out).get("pushback")
                                          and not _honored_pushback(_implementer_report(out).get("pushback"), given)
                                          else None),
                    "lessons": _list(_implementer_report(out).get("lessons")),
                    # The implementer made no change and claims the shared branch already satisfies the
                    # item (a sibling delivered it): the peer reviewer decides, never the implementer (H13).
                    "satisfiedClaim": (str(_mapping(_implementer_report(out).get("alreadySatisfied")).get("evidence")
                                           or "(no evidence given)")
                                       if str(out.get("deliveryOutcome") or "") == "satisfied" else ""),
                    # Files inside the fork the guard refused as outside the item's write roots, and files
                    # the commit left unstaged for the same reason (written through Bash, which the guard
                    # does not see): the next standup widens the item's scope from this (run fc6467b7,
                    # st-lru / store.go; run ba2491ff, sv-cmd-decr / echo.go).
                    "deniedWrites": sorted({str(p) for p in [*_list(out.get("deniedWrites")),
                                                             *_list(out.get("outsideScope"))] if str(p).strip()}),
                    "testsPassed": out.get("testsPassed") is True,
                    "deliveryOutcome": str(out.get("deliveryOutcome") or ""),
                    "stagingFault": [str(p) for p in _list(out.get("stagingFault")) if str(p).strip()],
                    "agentCompleted": out.get("agentCompleted") is True,
                    "changedFiles": len(changed), "diffLines": diff_lines,
                    "costUsd": _spend_args(out)["usd"] or 0.0,
                })
            if cid:
                for c in cands:
                    out = _mapping(joined_by_ref.get(c["ref"]))
                    file_ownership.bb.post_fact(cid, "integrator", "session", {
                        "item": gid, "candidate": c["ref"], "delivered": c["delivered"],
                        "deliveryOutcome": c["deliveryOutcome"], "testsPassed": c["testsPassed"],
                        "costUsd": c["costUsd"], "model": str(out.get("model") or ""),
                        "feedbackInjections": _list(out.get("feedbackInjections")),
                        "interruptedBy": out.get("interruptedBy"), "checkpoints": _list(out.get("checkpoints")),
                        "restoredCheckpoint": out.get("restoredCheckpoint"),
                        "pushback": c["pushback"], "unmatchedPushback": c["unmatchedPushback"],
                        "lessons": c["lessons"], "deniedWrites": c["deniedWrites"]})
            # Pushback (H16): the agent judged a constraint harmful/impossible and did not follow it. Its
            # work is not integrated; the item blocks and a human decides (lift the constraint or not).
            pushed = next((c for c in cands if c["pushback"]), None)
            worked = any(c["delivered"] or c["testsPassed"] or c["satisfiedClaim"] for c in cands)
            if pushed and not worked:
                pushbacks.append({"item": gid, **pushed["pushback"],
                                  "directiveId": given_ids.get(str(pushed["pushback"].get("matched") or ""), ""),
                                  "deniedWrites": pushed["deniedWrites"]})
                continue
            if pushed and worked and cid:
                # The implementer did its item and also refused a directive that was not about it (the
                # Architect's advice on sorted-set scores reached ce-keys, which then proved itself already
                # satisfied, run 351812b2): the delivery stands and the directive is exempted for this item.
                did = given_ids.get(str(pushed["pushback"].get("matched") or ""), "")
                if did:
                    team_board.exempt_directive(cid, "lead", did, gid, reason=str(pushed["pushback"].get("reason") or ""))
                team_board.post_exception(cid, "integrator", gid, "pushback",
                                          f"{gid} refused a directive it judged not its own ('{str(pushed['pushback'].get('directive') or '')[:120]}') "
                                          f"and delivered its item anyway; the directive is exempted for it", severity="info")
                team_board.resolve_exception(cid, "lead", gid, "pushback", "delivery stands; directive exempted")
            stopped = next((c["interruptedBy"] for c in cands if c["interruptedBy"]), None)
            winner = team_board.pick_best([c for c in cands if not c["interruptedBy"]])
            if stopped and not winner:
                # Blocking feedback stopped this agent mid-item (H11): never integrate the partial work;
                # re-open the item so the next standup re-dispatches it with the directive attached.
                interrupted.append({"item": gid, "by": _mapping(stopped).get("by"),
                                    "text": _mapping(stopped).get("text")})
            elif winner:
                eligible.append({"id": gid, "worktree": winner["worktree"], "branch": winner["branch"],
                                 "readyAt": idx, "shared": winner["shared"], "files": winner["changed"],
                                 "dispatchedAt": dispatched_at})
                for les in winner["lessons"]:
                    les = _mapping(les)
                    if cid and str(les.get("text") or "").strip():
                        # An implementer's lesson is its OPINION until an outcome verifies it (H15).
                        team_board.post_lesson(cid, f"implementer:{gid}", str(les["text"]), kind="opinion",
                                               confidence="medium", provenance=f"implementer:{gid}",
                                               refs=[str(r) for r in _list(les.get("refs"))])
                        shared_lessons += 1
                if len(cands) > 1:
                    best_of_picks.append({"item": gid, "winner": winner["ref"], "candidates": [
                        {k: c[k] for k in ("ref", "delivered", "touchesContract", "testsPassed",
                                           "deliveryOutcome", "changedFiles", "diffLines", "costUsd")}
                        for c in cands]})
            elif any(c["touchesContract"] for c in cands):
                c = next(c for c in cands if c["touchesContract"])
                contract_events.append({"item": gid, "candidate": c["ref"], "worktree": c["worktree"],
                                        "paths": contract_freeze.contract_paths_touched(c["changed"], contract_paths),
                                        "branch": c["branch"], "changed": c["changed"],
                                        "head": git.git(c["worktree"], "rev-parse", "HEAD", check=False).stdout.strip()})
            elif (cid and any(c["testsPassed"] for c in cands)
                  and _list(team_board.fold_board(cid)["items"].get(gid, {}).get("acceptanceTests"))):
                # No commit, yet the item's frozen acceptance tests (proven red before any implementation)
                # pass on its fork: the work is already on the shared branch. Done, not a failed delivery.
                satisfied.append(gid)
            elif cid and peer_review and any(c["satisfiedClaim"] for c in cands):
                # No commit and no frozen test to prove it, but the implementer reports the shared branch
                # already satisfies the item (cmd-dispatch: a sibling's file held its registry and
                # dispatch; six repair agents and a block followed, run 90e5e3aa). The peer reviewer
                # judges the claim against the criteria on the current tree: lgtm → done, changes → the
                # findings re-open it as any review would.
                claimed.append((gid, next(c["satisfiedClaim"] for c in cands if c["satisfiedClaim"])))
            elif any(c.get("stagingFault") for c in cands):
                unseen = next(c["stagingFault"] for c in cands if c.get("stagingFault"))
                failures.append((gid, "staging_fault",
                                 f"the item's files are on disk but git does not report them, a harness fault "
                                 f"no agent can repair: {', '.join(unseen[:10])}", []))
            elif all(c.get("deliveryOutcome") in ("implementation_unavailable", "audit_unavailable")
                     and not c.get("agentCompleted") for c in cands):
                failures.append((gid, "agent_unavailable",
                                 f"fork {', '.join(refs)}: the agent session did not complete "
                                 f"({', '.join(sorted({c['deliveryOutcome'] for c in cands}))}); retried on the same rung", []))
            else:
                failures.append((gid, "empty_delivery",
                                 f"fork {', '.join(refs)} produced no commit; not counted as integrated", []))
            # Losing candidates are discarded (their worktree and branch), never merged.
            keep = {winner["ref"]} if winner else set()
            keep |= {e["candidate"] for e in contract_events if e["item"] == gid}
            if len(cands) > 1:
                for c in cands:
                    if c["ref"] not in keep:
                        git.worktree_remove(shared_repo, c["ref"])
                        git.git(shared_repo, "branch", "-D", c["branch"], check=False)
                        discarded.append(c["ref"])

        base_before = git.head(shared_repo)
        # Fork candidates are rebased onto the moving shared branch; a shared-tree item is already on it.
        result = continuous_integrate.integrate_ready(shared_repo, shared_branch,
                                                      [e for e in eligible if not e["shared"]])
        on_branch = [e for e in eligible if e["shared"]]
        result["integrated"] = [*result.get("integrated", []),
                                *({"item": e["id"], "branch": shared_branch, "head": base_before,
                                   "base": _shared_delivery_base(shared_repo, e["dispatchedAt"], e["files"],
                                                                 base_before)}
                                  for e in on_branch)]
        for rec in result.get("reopened", []):
            failures.append((str(rec.get("item")), "rebase_conflict",
                             str(rec.get("mechanism") or "rebase conflict"), _list(rec.get("conflicts"))))

        reviewing, done_now, retry, blocked, pending_contract = [], [], [], [], []
        prev_head = base_before
        for rec in result.get("integrated", []):
            gid = str(rec.get("item"))
            git.git(shared_repo, "update-ref", "-d", f"refs/cc-checkpoints/{rec.get('branch')}", check=False)
            if cid:
                # The exact range this item contributed — the reviewer's diff, even if review slips a wave.
                file_ownership.bb.post_fact(cid, "integrator", "delivery", {
                    "item": gid, "base": str(rec.get("base") or prev_head), "head": str(rec.get("head") or ""),
                    "files": _list(by_id.get(gid, {}).get("files"))})
            if not rec.get("base"):
                prev_head = str(rec.get("head") or prev_head)
            if peer_review:
                if cid:
                    team_board.set_state(cid, "integrator", gid, team_board.REVIEW,
                                         reason="integrated; awaiting peer review")
                reviewing.append(gid)
            else:
                if cid:
                    team_board.set_state(cid, "integrator", gid, team_board.DONE, reason="integrated")
                    item = by_id.get(gid, {})
                    team_board.post_lesson(cid, gid,
                                           f"item {gid} implemented across {len(_list(item.get('files')))} file(s)",
                                           confidence="high", provenance="integrated",
                                           refs=_list(item.get("files")))
                done_now.append(gid)

        for gid in satisfied:
            if cid:
                team_board.set_state(cid, "integrator", gid, team_board.DONE,
                                     reason="already satisfied on the shared branch: its frozen acceptance tests "
                                            "pass with no new commit")
            done_now.append(gid)

        for gid, evidence in claimed:
            # An empty range at the current head: the reviewer judges the claim on the tree, not a diff.
            head_now = git.head(shared_repo)
            file_ownership.bb.post_fact(cid, "integrator", "delivery", {
                "item": gid, "base": head_now, "head": head_now,
                "files": _list(by_id.get(gid, {}).get("files")), "claimedSatisfied": evidence})
            team_board.set_state(cid, "integrator", gid, team_board.REVIEW,
                                 reason="implementer reports the item already satisfied on the shared branch; "
                                        "the peer reviewer decides")
            reviewing.append(gid)

        # Integration check (H13 at the integrated head): every integrated item's frozen acceptance
        # tests must still pass on the shared branch after this wave's merges. One that fails re-opens
        # its owner with the failure — the re-dispatch fixes the code, or amends a test whose assumption
        # a sibling's landing falsified (the peer reviewer decides). Bounded by maxAttempts below.
        board_items = team_board.fold_board(cid)["items"] if cid else {}
        check = _integration_check(cid, shared_repo, result["final_head"], board_items,
                                     sandbox=_truth(i.get("sandboxChecks"), True), since=base_before,
                                     landed=[str(r.get("item")) for r in result.get("integrated", [])])
        for gid, tail in sorted(check["red"].items()):
            paths = [str(p) for p in _list(_mapping(board_items.get(gid)).get("acceptanceTests"))]
            landed = ", ".join(str(r.get("item")) for r in result.get("integrated", [])) or "none"
            mech = (f"frozen acceptance test(s) {', '.join(paths)} of '{gid}' fail at the integrated head "
                    f"{result['final_head'][:12]} after this wave's merges ({landed}); re-dispatched to make "
                    f"them pass there")
            if gid in reviewing:
                reviewing.remove(gid)  # not worth a reviewer until its check is green on the branch
            if gid in done_now:
                done_now.remove(gid)
            failures.append((gid, CHECK_REOPEN, mech, paths))
            if cid:
                team_board.post_directive(
                    cid, "integrator", gid,
                    f"Its frozen acceptance test(s) {', '.join(paths)} FAIL on the shared branch at "
                    f"{result['final_head'][:12]} now that sibling items have landed. Make them pass there: fix "
                    f"the implementation, or — if a test's assumption was falsified by a sibling (e.g. it asserted "
                    f"a command the sibling registers is unknown) — amend that assertion, citing the design; the "
                    f"peer reviewer decides the amendment. Failing output:\n{tail}", authority="agent")

        # An honored pushback (H16) is decided by the Lead, never held for a human — humans are passive
        # observers. The item is exempted from the one directive it cannot follow (the directive stays in
        # force for everyone else) and re-dispatched; the decision is recorded as a resolved escalation so
        # the human sees it and can re-steer. Bounded by maxAttempts like any other re-dispatch.
        span = _team_span(board_items)
        max_rescues = max(int(i.get("maxRescues") if i.get("maxRescues") not in (None, "") else 2), 0)
        rescued: list[str] = []
        for rec in pushbacks:
            gid = rec["item"]
            mech = (f"implementer of '{gid}' pushed back on a constraint instead of following it: "
                    f"'{rec.get('directive', '')}' — {rec.get('reason', '')}")
            item = by_id.get(gid, {"id": gid})
            attempt = int(item.get("attempt") or 0)
            did = str(rec.get("directiveId") or "")
            denied_in_span = [p for p in _list(rec.get("deniedWrites")) if _in_span(str(p), span)]
            if cid and denied_in_span:
                # The implementer tried the edit the directive needs and the guard refused it: a scope
                # defect of the plan, not a refused constraint. sv-cmd-del and sv-cmd-decr were told to
                # make their frozen tests pass, found the cause (a type declared twice) in two siblings'
                # files, were refused there, and both blocked as constraint_pushback at the attempt cap —
                # the server subsystem ended there (run ba2491ff). Re-opened at the same attempt; the next
                # standup widens the item from the recorded refusal.
                team_board.set_state(cid, "lead", gid, team_board.REOPENED, attempt=attempt,
                                     reason=f"pushback names files outside the item's scope "
                                            f"({', '.join(denied_in_span)}); the Lead widens it at the next dispatch")
                team_board.post_exception(cid, "integrator", gid, "pushback", mech, severity="info")
                team_board.resolve_exception(cid, "lead", gid, "pushback",
                                             f"scope: {gid} is widened to {', '.join(denied_in_span)} at the next "
                                             f"dispatch instead of being held to the directive")
                retry.append({**item, "attempt": attempt})
                continue
            if cid and did:
                # Exempted and re-dispatched at the SAME attempt: a refused directive is not a failed
                # attempt, and an item has only as many directives to refuse as it was given. Counting it
                # sent five items of one team to a stronger-tier helper for refusing advice that was never
                # theirs (run 351812b2).
                team_board.exempt_directive(cid, "lead", did, gid, reason=str(rec.get("reason") or ""))
                team_board.set_state(cid, "lead", gid, team_board.REOPENED, attempt=attempt,
                                     reason="constraint_pushback: exempted from the directive; re-dispatched")
                team_board.post_exception(cid, "integrator", gid, "pushback", mech, severity="info")
                team_board.resolve_exception(cid, "lead", gid, "pushback",
                                             f"lead exempted {gid} from directive {did} and re-dispatched it")
                retry.append({**item, "attempt": attempt})
                continue
            if _rescue(cid, gid, f"constraint_pushback: {mech}", max_rescues=max_rescues):
                rescued.append(gid)
                continue
            blocked.append({"op": "reopen", "item": gid, "reason": "constraint_pushback", "mechanism": mech,
                            "conflicts": []})
            if cid:
                team_board.set_state(cid, "integrator", gid, team_board.BLOCKED, reason="constraint_pushback")
                team_board.post_exception(cid, "integrator", gid, "pushback", mech, severity="high")

        for rec in interrupted:
            gid = rec["item"]
            if cid:
                team_board.set_state(cid, "integrator", gid, team_board.REOPENED,
                                     reason=f"interrupted by blocking feedback from {rec['by']}")
            if shared_repo:
                # Its checkpoint must not be restored on re-dispatch — the human said to stop that work.
                git.git(shared_repo, "update-ref", "-d", f"refs/cc-checkpoints/{git.group_branch(shared_repo, gid)}",
                        check=False)

        # A frozen-contract edit is a controlled event (H4): never merged, never blindly retried. Humans are
        # passive observers, so the Lead decides at once: a frozen contract stays frozen — the change is
        # rejected and the next standup re-dispatches the proposer against the contract as-is (bounded by
        # maxContractEvents, after which the item blocks naming the churn).
        frozen_tests = set(_frozen_acceptance(cid).get("acceptanceTests") or []) if cid else set()
        rescued_items = _rescue_map(cid)
        held: set[str] = set()  # forks kept unmerged until the peer reviewer decides their amendment
        for ev in contract_events:
            gid = ev["item"]
            tests_only = set(ev["paths"]) <= frozen_tests
            what = "acceptance test(s)" if tests_only else "file(s)"
            amend = {"item": gid, "candidate": ev["candidate"], "worktree": ev["worktree"], "branch": ev["branch"],
                     "head": ev["head"], "base": base_before, "paths": ev["paths"], "files": ev["changed"]}
            if cid and not peer_review and rescued_items.get(gid):
                # Help was told a wrong frozen file is its to correct, and no reviewer is configured to judge
                # it: the Lead approves the strongest teammate's correction rather than loop it for good.
                if _decide_amendment(cid, shared_repo, shared_branch, amend, True,
                                     "no peer review configured; the help's correction stands"):
                    team_board.set_state(cid, "integrator", gid, team_board.DONE,
                                         reason=f"integrated with its correction of frozen {what} {', '.join(ev['paths'])}")
                    done_now.append(gid)
                else:
                    failures.append((gid, "rebase_conflict", f"the correction of frozen {what} "
                                                             f"{', '.join(ev['paths'])} did not merge", ev["paths"]))
                continue
            if peer_review and cid and (tests_only or rescued_items.get(gid)):
                # An amendment of frozen acceptance test(s) only — the test itself may be wrong (one asserted
                # Exists==2 right after deleting both keys, contradicting the design; run 3e502ddc). Held
                # UNMERGED and judged by the peer reviewer (a different agent than both authors), not
                # auto-rejected: a defective check otherwise deadlocks its item for good.
                held.add(str(ev["candidate"]))
                file_ownership.bb.post_fact(cid, "integrator", AMENDMENT_KIND, amend)
                team_board.set_state(cid, "integrator", gid, team_board.REVIEW,
                                     reason=f"proposes amending frozen {what} {', '.join(ev['paths'])}; "
                                            f"the peer reviewer decides")
                reviewing.append(gid)
                continue
            prior = contract_event.change_count(prior_exc, gid)
            if prior >= max_contract_events:
                mech = (f"item {gid} proposed {prior + 1} frozen-contract changes (cap {max_contract_events}); "
                        f"interface churn; the item is blocked")
                if _rescue(cid, gid, f"contract_churn: {mech}", max_rescues=max_rescues):
                    rescued.append(gid)
                    continue
                blocked.append({"op": "reopen", "item": gid, "reason": contract_event.CHURN_EXCEPTION,
                                "mechanism": mech, "conflicts": ev["paths"]})
                if cid:
                    team_board.set_state(cid, "integrator", gid, team_board.BLOCKED, reason=mech)
                    team_board.post_exception(cid, "integrator", gid, contract_event.CHURN_EXCEPTION, mech,
                                              severity="high")
                continue
            mech = (f"fork {gid} edited the frozen contract {', '.join(ev['paths'])} on {ev['branch']} "
                    f"({ev['head'][:12]}); rejected by the lead — {gid} is re-dispatched against the frozen "
                    f"contract")
            pending_contract.append({**ev, "mechanism": mech})
            if cid:
                file_ownership.bb.post_fact(cid, "integrator", "contract_proposal", {
                    "item": gid, "candidate": ev["candidate"], "worktree": ev["worktree"],
                    "branch": ev["branch"], "head": ev["head"], "paths": ev["paths"]})
                team_board.set_state(cid, "integrator", gid, team_board.BLOCKED,
                                     reason=contract_event.PENDING_REASON)
                team_board.post_exception(cid, "integrator", gid, contract_event.CHANGE_EXCEPTION, mech,
                                          severity="info")
                team_board.resolve_exception(
                    cid, "lead", gid, contract_event.CHANGE_EXCEPTION,
                    f"The frozen contract {', '.join(ev['paths'])} must not be edited; implement against it "
                    f"as-is.", decision="reject")

        for gid, reason, mechanism, conflicts in failures:
            rec = {"op": "reopen", "item": gid, "reason": reason, "mechanism": mechanism, "conflicts": conflicts}
            # An item the check re-opens may have been done a wave ago: its attempt is on the board.
            item = by_id.get(gid) or {**_mapping(board_items.get(gid)), "id": gid}
            attempt = int(item.get("attempt") or 0)
            if reason in _RETRYABLE_REOPEN and (attempt + 1) < max_attempts:
                retry.append({**item, "attempt": attempt + 1})
                if cid:
                    team_board.set_state(cid, "integrator", gid, team_board.REOPENED,
                                         reason=mechanism, attempt=attempt + 1)
                    file_ownership.bb.post_fact(cid, gid, "retry", {**rec, "op": "retry", "nextAttempt": attempt + 1})
            elif reason == "staging_fault":
                # A harness fault: a retry or a stronger teammate would redo work that is already on disk.
                blocked.append(rec)
                if cid:
                    file_ownership.bb.post_fact(cid, gid, "reopen", rec)
                    team_board.post_exception(cid, "integrator", gid, "staging_fault", mechanism)
                    team_board.set_state(cid, "integrator", gid, team_board.BLOCKED, reason=mechanism)
            else:
                if cid:
                    file_ownership.bb.post_fact(cid, gid, "reopen", rec)
                if _rescue(cid, gid, f"{reason}: {mechanism}", max_rescues=max_rescues):
                    rescued.append(gid)
                    continue
                blocked.append(rec)
                if cid:
                    team_board.set_state(cid, "integrator", gid, team_board.BLOCKED, reason=mechanism)

        # A fork is finished once the wave has decided it: integrated work is on the shared branch, a
        # re-dispatch starts from a fresh fork (its checkpoint lives in refs/cc-checkpoints, not in the
        # worktree) and a discarded fork is gone for good. Only a fork held for an amendment decision
        # keeps its worktree. Lingering worktrees are not free: Claude Code's Bash sandbox adds deny
        # paths for every worktree registered on the repository, and at 171 of them the profile exceeded
        # the OS exec argument limit — every implementer's every Bash call failed at spawn (E2BIG, 591
        # calls) and no agent could build or test (run ec0fb6dc).
        removed_forks = []
        for item in wave:
            for ref in [str(r) for r in _list(item.get("candidates"))] or [str(item.get("id"))]:
                if ref in held or ref in discarded:
                    continue
                if os.path.isdir(_subtask_worktree(shared_repo, ref)):
                    git.worktree_remove(shared_repo, ref)
                    git.git(shared_repo, "branch", "-D", git.group_branch(shared_repo, ref), check=False)
                    removed_forks.append(ref)

        released, recorded = 0, 0
        if cid:
            parent = str(i.get("parentCorrelationId") or "").strip()
            for item in dispatched:
                item = _mapping(item)
                sid = str(item.get("id") or "")
                for path in _list(item.get("files")):
                    file_ownership.release(parent or cid, f"{cid}:{sid}" if parent else sid, str(path))
                    released += 1
            for ref, out in _iter_joined(i.get("joined")):
                a = _spend_args(out)
                budget_gate.record_spend(cid, f"implementer:{re.sub(r'__[0-9]+$', '', str(ref))}",
                                         usd=a["usd"], tokens=a["tokens"], model=a["model"])
                recorded += 1

        return ok(task, {
            "reviewing": reviewing, "doneNow": done_now, "retry": retry, "blocked": blocked,
            "contractEvents": pending_contract, "contractEventCount": len(pending_contract),
            "interrupted": interrupted,
            "bestOf": best_of_picks, "discardedCandidates": discarded, "pushbacks": pushbacks,
            "lessonsShared": shared_lessons, "removedForks": removed_forks, "check": check, "rescued": rescued,
            "integratedCount": len(result.get("integrated", [])), "reviewingCount": len(reviewing),
            "retryCount": len(retry), "blockedCount": len(blocked), "releasedFiles": released,
            "spendRecorded": recorded, "big_bang_merge": result["big_bang_merge"],
            "final_head": result["final_head"],
        }, [f"[team_integrate] integrated={len(result.get('integrated', []))} review={len(reviewing)} "
            f"done={len(done_now)} retry={len(retry)} blocked={len(blocked)} "
            f"released={released} removedForks={len(removed_forks)} head={result['final_head'][:12]} "
            f"check={check['state']} checked={len(check['checked'])} red={sorted(check['red'])}"
            + (f" reason={check.get('reason')}" if check.get("reason") else "")])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_integrate", e)


# ─────────────────────────────── team_review_build ────────────────────────────
def _latest_delivery(cid: str | None) -> dict:
    out: dict[str, dict] = {}
    if cid:
        for f in file_ownership.bb.read_facts(cid, kind="delivery"):
            d = _mapping(f.get("data"))
            out[str(d.get("item"))] = d
    return out


@worker_task(task_definition_name="team_review_build")
def team_review_build(task):
    """Build read-only peer-review forks for every item awaiting review (H8). The reviewer is a
    DIFFERENT agent than the implementer (H13 — the author is not the check). Each item's change is the
    exact range it contributed at integration (its ``delivery`` fact), so an item whose review slipped a
    wave (reviewer unavailable) is still reviewed against its own diff. Every item in ``review`` is
    reviewed — the round cap is enforced on the verdict (quarantine), never by skipping review."""
    i = task.input_data or {}
    try:
        shared_repo = str(i.get("sharedRepo") or "").strip()
        cid = str(i.get("correlationId") or "").strip() or None
        wave_base = str(i.get("waveBase") or "").strip()
        final_head = str(i.get("finalHead") or "").strip()

        board = team_board.fold_board(cid) if cid else {"items": {}, "reviews": []}
        board["directives"] = (board.get("directives") or []) + _parent_directives(i)
        items = board["items"]
        reviewing = [str(x) for x in _list(i.get("reviewing"))]
        reviewing += sorted(gid for gid, it in items.items()
                            if it.get("state") == team_board.REVIEW and gid not in reviewing)
        deliveries = _latest_delivery(cid)
        rescued_items = _rescue_map(cid)
        done_section = _done_section(shared_repo)

        dynamic_tasks, dynamic_inputs, reviewed = [], {}, []
        for gid in reviewing:
            item = _mapping(items.get(gid))
            files = [str(p) for p in _list(item.get("files"))]
            dlv = deliveries.get(gid, {})
            amend = _pending_amendment(cid, gid)
            if amend:  # the unmerged fork that proposes amending frozen acceptance test(s)
                dlv = amend
                files = files + [p for p in _list(amend.get("paths")) if p not in files]
            if rescued_items.get(gid):
                files = []  # help may have changed anything: the reviewer reads its whole diff
            base = str(dlv.get("base") or wave_base or f"{final_head}~1")
            head = str(dlv.get("head") or final_head)
            diff = ""
            # The reviewer reads its own worktree pinned at the reviewed head, never the shared
            # integration worktree: the agent runner resets the worktree it ran in to the HEAD it
            # started from, which undid a sibling team's concurrent merge there and left that team's
            # files untracked — its next fast-forward aborted and the team failed (run a062184f, resp).
            review_wt = shared_repo
            if shared_repo and os.path.isdir(shared_repo) and head:
                diff = git.git(shared_repo, "diff", f"{base}..{head}", "--", *files,
                               check=False).stdout[:_DIFF_LIMIT]
                review_wt = git.worktree_add_detached(shared_repo, f"review-{gid}", head)["worktreePath"]
            # The reviewer's check = the shared acceptance criteria PLUS the hidden reviewCriteria the
            # implementer never saw (H8/H13 — catch what tests and the handed-over spec do not).
            acceptance = "\n".join(f"- {c}" for c in
                                   (_list(item.get("acceptanceCriteria")) + _list(item.get("reviewCriteria"))))
            ref = f"review_{gid}"
            # Steering the implementer received (human first). A human directive overrides the item's
            # original description (H16 authority): the reviewer must judge against it, not re-litigate
            # it — otherwise review undoes what a human asked for.
            steering = _directive_digest(_steering(cid, board), gid)
            prompt_context = {
                "item": gid, "description": item.get("description") or item.get("title") or "",
                "steeringSection": (f"Steering directives on this item (a (human) directive OVERRIDES the description "
                                    f"above; never request a change that contradicts one):\n{steering}\n\n"
                                    if steering else ""),
                "acceptance": acceptance or "(none stated)", "files": ", ".join(files) or "(any file)",
                "doneSection": (done_section + "\nA change that meets the item's criteria but not a check's interface "
                                "(its syntax, paths, ports or output format) is a blocking finding: name the check.\n"
                                if done_section else ""),
                "claimSection": (
                    f"THE IMPLEMENTER MADE NO CHANGE: it reports the shared branch ALREADY satisfies this item "
                    f"(its evidence: {dlv.get('claimedSatisfied')}). The diff below is empty by design. Read the "
                    f"current tree and decide: 'lgtm' = the item is done as-is (every criterion is met by code "
                    f"that already exists); 'changes' = what is missing, or what the design places in this "
                    f"item's files but lives elsewhere, as concrete requested changes. "
                    if dlv.get("claimedSatisfied") else ""),
                "amendSection": (
                    f"THIS CHANGE AMENDS FROZEN FILE(S) {', '.join(_list(amend.get('paths')))} — the acceptance "
                    f"test or contract another agent wrote before implementation. It is held unmerged and YOU decide: "
                    f"approve or reject, you cannot send the test back for more edits. Return 'lgtm' to approve "
                    f"when the original test is genuinely wrong (it contradicts the design documents, the frozen "
                    f"contract or the acceptance criteria, or cannot run as written — cite where) AND the amended "
                    f"test checks at least what the original did. A weakness the original test also had is not "
                    f"grounds to reject: judge the correction, not whether the test could be stronger. Return "
                    f"'changes' only to reject, when the original test was right. The test then stays exactly as "
                    f"it was, so every finding must be a change to the implementation, never to the test. "
                    if amend else ""),
                "checkNote": (
                    "This item's deliverable is DOCUMENTS, not code: you are the only check (there are no "
                    "executable tests for it). Check every acceptance criterion is actually met, and that the "
                    "documents are concrete enough for other teams to build from without guessing (names, "
                    "interfaces, formats, error behaviour, edge cases); vague or missing parts are blocking. "
                    if team_board.deliverable_of(item) == "docs" else
                    "The automated test suite is the correctness check and runs separately — do NOT just "
                    "restate that tests pass. Your job is to catch what tests cannot: an acceptance "
                    "criterion left unmet, a frozen-contract violation, a design/security smell, or a test in "
                    "the diff that cannot fail (a tautological test is a blocking finding). "),
                "lessons": _lesson_digest(team_board.live_lessons(board.get("lessons") or [])) or "(none)",
                "diffFiles": ", ".join(files) or "(all files)", "diff": diff or "(empty diff)",
            }
            prompt = templating.render_default("team_review", prompt_context)
            dynamic_tasks.append({"name": "coding_agent", "taskReferenceName": ref, "type": "SIMPLE", "optional": True})
            # A reviewer that returned no verdict is itself the one stuck: its re-review runs a tier up.
            silent = sum(1 for r in board.get("reviews") or [] if str(_mapping(r).get("item")) == gid
                         and _mapping(r).get("verdict") == "unavailable")
            dynamic_inputs[ref] = {
                "worktreePath": review_wt, "tools": _REVIEW_TOOLS, "templateKey": "team_review", "modelTier": silent,
                "model": i.get("codeModel") or "", "maxTurns": i.get("reviewMaxTurns") or 60,
                "maxBudgetUsd": i.get("reviewMaxBudgetUsd") or 3, "prompt": prompt,
                "promptPath": prompt_files.stash(shared_repo, ref, prompt, prompt_context),
                "promptContext": prompt_context, "promptTemplate": "", "promptTemplateSource": "",
                "modelProfile": i.get("modelProfile") or "", "modelPolicy": i.get("modelPolicy") or {},
                "modelPolicySource": i.get("modelPolicySource") or "",
                "modelPolicySha256": i.get("modelPolicySha256") or "",
                "modelsConfig": i.get("modelsConfig") or "", "modelOverrides": i.get("modelOverrides") or {},
                "schema": REVIEWER_SCHEMA,
            }
            reviewed.append(gid)
        return ok(task, {"dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
                         "reviewed": reviewed, "reviewCount": len(reviewed)},
                  [f"[team_review_build] reviewing={len(reviewed)} ({', '.join(reviewed)})"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_review_build", e)


def _final_gate(board: dict, shared_repo: str, parent_cid: str, subsystem_id: str = "") -> dict:
    """What the team's final test cycle verifies, proves and may repair, read from the board NOW.

    ``finalHead`` is the shared head AFTER the caller's step: an approved acceptance-test amendment is
    merged by apply_reviews, after the wave's integrate step, so the integrator's head is stale by
    then — the server team re-froze its amended test at the merged head and was then judged "modified
    after frozen" against the pre-merge head, blocking cmd and integration (run ec0fb6dc). The frozen
    acceptance tests of the items that are DONE are what the cycle must prove (an undone item's test
    is red by construction — the store team's final cycle ran st-core's test and its repair agent set
    out to implement st-core, with write roots derived from a sibling team's last commit, run
    a4f1ff1f), and the files the team's items span are its write authority (never another team's
    directory). A program's team is gated on its OWN check: in a repository that documents no test
    command, discovery inferred a bare root `pytest`, which ran every team's acceptance tests — the
    pgtype team was refused for the parser team's tests while that team was still building in the
    same rank, and its own two tests never ran (run 037e1cbe); cross-team checks are the integration
    check's (every wave) and the program's final full suite's. A program's team with nothing of its
    own to gate on (a documents team) is verified against its diff only (``targeted``), never the
    repository's whole suite, which cost a documents team 23 minutes on a sibling's stale, hanging
    test (team 11d9aaa2). A standalone team keeps the repository's own command (``full``), and so does
    a program's team with no frozen test when no sibling subsystem is still open: there is nothing of
    anyone else's for the full suite to run, and ``targeted`` discovery cannot map a new project's
    package.json to a unit, so the one-item React team's final cycle fell back to an agent that
    proposed nothing and then authored a test it did not need (run b00146a9)."""
    final_head = git.head(shared_repo) if shared_repo and os.path.isdir(shared_repo) else ""
    items = board["items"]
    acceptance_commands = [c for gid, it in sorted(items.items()) if it.get("state") == team_board.DONE
                           for c in _list(it.get("testCommands")) if _list(_mapping(c).get("argv"))]
    write_roots = sorted({str(p) for it in items.values() for p in _list(it.get("files")) if str(p).strip()})
    gate_commands = acceptance_commands if parent_cid.strip() else []
    siblings_open = parent_cid.strip() and any(
        it.get("kind", "subsystem") == "subsystem" and sid != subsystem_id and it.get("state") != team_board.DONE
        for sid, it in team_board.fold_board(parent_cid.strip())["items"].items())
    gate_mode = ("commands" if gate_commands else "targeted") if siblings_open else ("commands" if gate_commands else "full")
    return {"finalHead": final_head, "acceptanceCommands": acceptance_commands, "writeRoots": write_roots,
            "gateCommands": gate_commands, "gateMode": gate_mode,
            "testMode": "targeted" if gate_mode == "targeted" else "full"}


# ─────────────────────────────── team_gate_refresh ────────────────────────────
@worker_task(task_definition_name="team_gate_refresh")
def team_gate_refresh(task):
    """Re-read the final gate from the board once BOTH branches of ``accept_and_build`` have joined.

    The frozen acceptance tests are authored beside the first wave, so they can land on the branch
    after the last wave's ``team_apply_reviews`` computed the gate: the calculator team's one item
    was done at 27 s, its test file was committed at 72 s, the wave loop's variables still said "no
    frozen commands", and the final cycle ran nothing — the delivered head was judged
    ``acceptanceUnproven`` and the frozen test "modified after frozen" against a head older than its
    own commit (run 884f7eec). The gate the final cycle runs is the board's state at the join."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip()
        board = team_board.fold_board(cid) if cid else {"items": {}}
        gate = _final_gate(board, str(i.get("sharedRepo") or "").strip(), str(i.get("parentCorrelationId") or ""),
                           str(i.get("subsystemId") or ""))
        return ok(task, gate, [f"[team_gate_refresh] head={gate['finalHead'][:12]} "
                               f"acceptance_commands={len(gate['acceptanceCommands'])} mode={gate['gateMode']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_gate_refresh", e)


# ─────────────────────────────── team_apply_reviews ───────────────────────────
@worker_task(task_definition_name="team_apply_reviews")
def team_apply_reviews(task):
    """Apply peer-review verdicts and re-plan (the 'learn → re-plan' half of the cycle).

    ``lgtm`` → ``done`` + a lesson. ``changes`` → ``reopened`` with the findings as a directive, while
    the item has absorbed fewer than ``maxReviewRounds`` change requests; the next ``changes`` QUARANTINES
    it (``blocked``, ``review_rounds_exhausted``, exception ``review_cap``) for a human — a rejected item
    is never silently accepted (H2/H8). ``unavailable`` (the reviewer produced no verdict) keeps the item
    in ``review`` for the next wave, up to ``maxReviewAttempts`` → ``blocked`` (``reviewer_unavailable``).
    Every reviewer's spend is recorded on the run budget (H5)."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip() or None
        shared_repo = str(i.get("sharedRepo") or "").strip()
        shared_branch = str(i.get("sharedBranch") or "").strip()
        reviewed = [str(x) for x in _list(i.get("reviewed"))]
        max_rounds = max(int(i.get("maxReviewRounds") or 2), 1)
        max_attempts = max(int(i.get("maxReviewAttempts") or 2), 1)
        if shared_repo and os.path.isdir(shared_repo):
            for gid in reviewed:  # the reviewers' pinned worktrees (team_review_build) are done with
                git.worktree_remove(shared_repo, f"review-{gid}")
        # Inside the DO_WHILE each forked review task ref gets a ``__<iteration>`` suffix, so the JOIN
        # output is keyed ``review_<id>__N``, not ``review_<id>``. Normalize the suffix so a real
        # ``changes`` verdict is actually read.
        joined = {}
        recorded = 0
        for ref, out in _iter_joined(i.get("joined")):
            joined[ref] = out
            bare = re.sub(r"__\d+$", "", str(ref))
            joined.setdefault(bare, out)
            if cid and _record(cid, bare, out):
                recorded += 1

        board = team_board.fold_board(cid) if cid else {"items": {}, "reviews": []}
        items = board["items"]
        prior_changes: dict[str, int] = {}
        prior_unavailable: dict[str, int] = {}
        max_rescues = max(int(i.get("maxRescues") if i.get("maxRescues") not in (None, "") else 2), 0)
        rescued_now: list[str] = []
        # Review rounds restart when a stronger teammate takes an item over: the cap measures the helper's
        # rounds, not the ones that got the item stuck.
        last_rescue = {}
        for f in (file_ownership.bb.read_facts(cid, kind=RESCUE_KIND) if cid else []):
            last_rescue[str(_mapping(f.get("data")).get("item"))] = float(f.get("ts") or 0)
        for rev in board.get("reviews", []):
            rev = _mapping(rev)
            iid = str(rev.get("item"))
            if float(rev.get("ts") or 0) <= last_rescue.get(iid, -1):
                continue
            if rev.get("verdict") == "changes" and not rev.get("scopeOnly"):
                prior_changes[iid] = prior_changes.get(iid, 0) + 1
            elif rev.get("verdict") == "unavailable":
                prior_unavailable[iid] = prior_unavailable.get(iid, 0) + 1

        changes, done, quarantined, unavailable = [], [], [], []
        retracted = []
        for gid in reviewed:
            raw = _mapping(joined.get(f"review_{gid}"))
            st = raw.get("structured") if isinstance(raw.get("structured"), dict) else {}
            if not st and isinstance(raw.get("result"), str):
                try:
                    st = _mapping(json.loads(raw["result"]))
                except (ValueError, TypeError):
                    st = {}
            for text in _list(_mapping(st).get("retract")):
                if cid and str(text).strip():
                    team_board.retract_lesson(cid, f"reviewer_{gid}", str(text), reason=f"contradicted by {gid}'s diff")
                    retracted.append(str(text))
            fields = _review_fields(joined.get(f"review_{gid}"))
            verdict, findings, out_of_scope = fields["verdict"], fields["findings"], fields["outOfScope"]
            # A round that asks only for files the implementer may not write is a scope defect of the
            # plan, not a rejection of the work: d-store's reviewer named architecture.md in round one,
            # the Lead widened only after the implementer's refusal a wave later, and round two — "not a
            # fault of this implementer, the Lead needs to widen" — spent the item's last round; a new
            # finding in round three quarantined it and the program ended at 0/6 (run 0709d705).
            scope_only = bool(out_of_scope) and not findings
            # A file outside the directories this team's items span can never be widened to (the
            # standup refuses it: writing outside the subsystem is the program's call). A round that asks
            # ONLY for such files cannot be addressed by this implementer: re-dispatching sent it a
            # directive it had to push back on, the Lead exempted it, and a fourth round began for
            # nothing (storage-core, run c4a9237d). The item is accepted; the change is carried to the
            # team (and the program's Architect reads the board) as a directive, decided and recorded.
            span = _team_span(items)
            unreachable = [x for x in out_of_scope if not _in_span(str(_mapping(x).get("file")), span)
                           and str(_mapping(x).get("file")) not in _list(_mapping(items.get(gid)).get("files"))]
            if scope_only and unreachable and len(unreachable) == len(out_of_scope):
                carried = "; ".join(f"in {_mapping(x).get('file')}: {_mapping(x).get('change') or 'a change this item needs there'}"
                                    for x in unreachable)
                mech = (f"peer review of '{gid}' asked only for files outside this team's directories "
                        f"({', '.join(str(_mapping(x).get('file')) for x in unreachable)}); accepted, the change "
                        f"carried to the team: {carried}")
                if cid:
                    team_board.post_review(cid, f"reviewer_{gid}", gid, "lgtm", findings=[], out_of_scope=out_of_scope,
                                           scope_only=True)
                    team_board.post_directive(cid, "lead", "team", f"'{gid}' was accepted; its reviewer named a change "
                                              f"outside this team's directories, for whoever owns it: {carried}",
                                              authority="agent")
                    team_board.set_state(cid, "lead", gid, team_board.DONE,
                                         reason="peer review passed; a change outside this team's directories carried on")
                    team_board.post_exception(cid, "lead", gid, "scope_unreachable", mech, severity="info")
                    team_board.resolve_exception(cid, "lead", gid, "scope_unreachable", mech)
                    team_board.post_lesson(cid, gid, f"item {gid} delivered and peer-reviewed", confidence="high",
                                           provenance="reviewed", refs=_list(_mapping(items.get(gid)).get("files")))
                done.append(gid)
                continue
            if out_of_scope and verdict == "lgtm":
                # The reviewer approved the diff but named a change the item still needs in a file its
                # implementer may not write: the item is not done until that lands. sv-cmd-registry was
                # marked done on such a verdict, its outOfScope (a duplicate Handler type in echo.go)
                # was never applied, and the package stayed unbuildable for the rest of the run (ba2491ff).
                verdict = "changes"
            if cid:
                team_board.post_review(cid, f"reviewer_{gid}", gid, verdict, findings=findings,
                                       out_of_scope=out_of_scope, scope_only=scope_only)
                if out_of_scope:
                    # The reviewer's named files are the Lead's widening evidence now, as an
                    # implementer's refusal is (team_standup widens at the next dispatch).
                    file_ownership.bb.post_fact(cid, f"reviewer_{gid}", "session", {
                        "item": gid, "candidate": gid, "source": "review",
                        "deniedWrites": [x["file"] for x in out_of_scope]})
            # A file named without detail is still a change the item needs: with no finding the item
            # fell through to done and the file was never applied (the ba2491ff mechanism, by another door).
            findings = findings + [f"in {x['file']}: {x.get('change') or 'a change this item still needs there'}"
                                   for x in out_of_scope if x.get("file")]
            item = _mapping(items.get(gid))
            amend = _pending_amendment(cid, gid) if verdict != "unavailable" else {}
            if amend and cid:
                # The reviewer judged a proposed amendment of this item's frozen acceptance test(s).
                approve = verdict != "changes" or not findings
                if _decide_amendment(cid, shared_repo, shared_branch, amend, approve, "; ".join(findings)):
                    team_board.set_state(cid, "lead", gid, team_board.DONE,
                                         reason=f"peer review approved amending {', '.join(_list(amend.get('paths')))}")
                    team_board.post_lesson(cid, f"reviewer_{gid}",
                                           f"frozen acceptance test(s) {', '.join(_list(amend.get('paths')))} were "
                                           f"amended for {gid}; the reviewer judged the original wrong",
                                           confidence="high", provenance="reviewed", refs=_list(amend.get("paths")))
                    done.append(gid)
                    continue
                if approve:  # approved but the merge conflicted: rebuild against the current head
                    team_board.set_state(cid, "lead", gid, team_board.REOPENED,
                                         reason="approved acceptance-test amendment conflicted on merge")
                    changes.append(gid)
                    continue
                # Rejected: the fork is discarded; the findings below re-open the item as any review would.
            if verdict == "unavailable":
                n = prior_unavailable.get(gid, 0) + 1
                if n >= max_attempts:
                    mech = (f"reviewer for '{gid}' produced no verdict {n} time(s) (cap {max_attempts}); "
                            f"the item cannot be accepted without review")
                    if _rescue(cid, gid, f"reviewer_unavailable: {mech}", max_rescues=max_rescues):
                        rescued_now.append(gid)
                        continue
                    if cid:
                        team_board.set_state(cid, "lead", gid, team_board.BLOCKED, reason="reviewer_unavailable")
                        team_board.post_exception(cid, "lead", gid, "reviewer_unavailable", mech, severity="high")
                    quarantined.append(gid)
                else:
                    unavailable.append(gid)  # stays in review; re-reviewed next wave
                continue
            if verdict == "changes" and findings:
                # A rejected amendment is final for that test. Sending its findings on as "address these"
                # told the implementer to edit a file its brief calls frozen: it pushed back every round
                # and resp-reader spent both rescues on the contradiction (run 0588dafa).
                lead_in = (f"Your amendment of frozen file(s) {', '.join(_list(amend.get('paths')))} was "
                           f"rejected and discarded. They stay exactly as they are: do not edit them, make the "
                           f"implementation pass them. Peer review: " if amend else "Address the peer-review findings: ")
                n = prior_changes.get(gid, 0) + (0 if scope_only else 1)
                if scope_only or n <= max_rounds:
                    if cid:
                        team_board.post_directive(cid, f"reviewer_{gid}", gid, lead_in + "; ".join(findings),
                                                  authority="agent")
                        team_board.set_state(cid, "lead", gid, team_board.REOPENED,
                                             reason=("peer review needs a file outside the item's scope; the Lead "
                                                     "widens it at the next dispatch") if scope_only
                                             else "peer review requested changes")
                    changes.append(gid)
                elif fields["unresolved"] == [] and prior_changes.get(gid, 0) > 0:
                    # Over the cap, yet every prior finding was addressed: each round the reviewer found a
                    # new gap in the previous round's fix (the architecture doc's stats wiring: three
                    # rounds, three new topics, the one rank-0 item quarantined and the program ended at
                    # 0/6, run a57bf979). The work is not rejected, the review is still finding things.
                    # The Lead accepts the item and carries the new findings to the items that build on
                    # it (its dependents, else the team) as directives — decided and recorded, not silent.
                    dependents = sorted(did for did, it in items.items() if gid in [str(d) for d in _list(it.get("dependsOn"))])
                    mech = (f"peer review requested changes on '{gid}' {n} time(s), over the cap of {max_rounds}, "
                            f"with every prior finding addressed; accepted, latest findings carried to "
                            f"{', '.join(dependents) or 'the team'}: {'; '.join(findings)}")
                    if cid:
                        for target in dependents or ["team"]:
                            team_board.post_directive(
                                cid, "lead", target,
                                f"'{gid}' was accepted at its review cap with these open findings; cover them "
                                f"in your work where they apply: " + "; ".join(findings), authority="agent")
                        team_board.set_state(cid, "lead", gid, team_board.DONE,
                                             reason="accepted at the review cap: every prior finding addressed; "
                                                    "latest findings carried to dependents")
                        team_board.post_exception(cid, "lead", gid, "review_cap", mech, severity="info")
                        team_board.resolve_exception(cid, "lead", gid, "review_cap", mech)
                        team_board.post_lesson(cid, gid, f"item {gid} delivered and peer-reviewed", confidence="high",
                                               provenance="reviewed", refs=_list(item.get("files")))
                    done.append(gid)
                else:
                    if _rescue(cid, gid, f"review_rounds_exhausted: {n} change rounds over the cap of {max_rounds}; "
                                         f"prior findings unaddressed: {'; '.join(fields['unresolved'] or [])}; "
                                         f"latest: {'; '.join(findings)}", max_rescues=max_rescues):
                        team_board.post_directive(cid, f"reviewer_{gid}", gid, lead_in + "; ".join(findings),
                                                  authority="agent")
                        rescued_now.append(gid)
                        continue
                    mech = (f"peer review requested changes on '{gid}' {n} time(s), over the cap of "
                            f"{max_rounds}; prior findings unaddressed: "
                            f"{'; '.join(fields['unresolved'] or ['(not reported)'])}; latest findings: "
                            f"{'; '.join(findings)}")
                    if cid:
                        team_board.set_state(cid, "lead", gid, team_board.BLOCKED,
                                             reason="review_rounds_exhausted")
                        team_board.post_exception(cid, "lead", gid, "review_cap", mech, severity="high")
                    quarantined.append(gid)
                continue
            # lgtm, or a 'changes' with no actionable finding (H8: a round that adds no actionable change
            # is dropped, not repeated).
            if cid:
                team_board.set_state(cid, "lead", gid, team_board.DONE,
                                     reason="peer review passed" if verdict == "lgtm"
                                     else "review returned no actionable finding")
                team_board.post_lesson(cid, gid, f"item {gid} delivered and peer-reviewed", confidence="high",
                                       provenance="reviewed", refs=_list(item.get("files")))
            done.append(gid)

        board = team_board.fold_board(cid) if cid else board
        rollup = team_board.board_rollup(board)
        gate = _final_gate(board, shared_repo, str(i.get("parentCorrelationId") or ""), str(i.get("subsystemId") or ""))
        # The frozen acceptance tests are authored beside the first wave. A board that drains before
        # they have landed is not done: the next standup waits for them and the red ones re-open their
        # items (the calculator's one item was done at 117 s, its test landed at 125 s red, the loop
        # had already exited and a repair agent at the final cycle spent 134 s instead, run 993dce38).
        settled = bool(file_ownership.bb.read_facts(cid, kind=ACCEPTANCE_SETTLED_KIND)) if cid else True
        return ok(task, {"drained": rollup["drained"] and settled, "allDone": rollup["allDone"], "rollup": rollup,
                         "acceptanceSettled": settled,
                         "changes": changes, "changesCount": len(changes), "doneCount": len(done),
                         "quarantined": quarantined, "unavailable": unavailable, "retracted": retracted,
                         "reviewSpendRecorded": recorded, "rescued": rescued_now, **gate},
                  [f"[team_apply_reviews] done={len(done)} changes={len(changes)} "
                   f"quarantined={len(quarantined)} unavailable={len(unavailable)} "
                   f"drained={rollup['drained']} head={gate['finalHead'][:12]} board={_rollup_line(rollup)}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_apply_reviews", e)


# ─────────────────────── acceptance tests: author ≠ check (WP6, H13) ────────────────────
def _acceptance_path(pattern: str, item_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", item_id)
    return (pattern or "tests/acceptance/test_accept_{id}.py").replace("{id}", safe)


def _transitive_deps(items: dict, gid: str) -> set[str]:
    """Every item ``gid`` depends on, directly or through another item (pure)."""
    out: set[str] = set()
    stack = [str(d) for d in _list(_mapping(items.get(gid)).get("dependsOn"))]
    while stack:
        d = stack.pop()
        if d in out or d == gid:
            continue
        out.add(d)
        stack.extend(str(x) for x in _list(_mapping(items.get(d)).get("dependsOn")))
    return out


def _acceptance_isolation(items: dict, gid: str) -> str:
    """What an acceptance-test author may rely on (pure). The tests run on the implementer's fork before
    sibling items land: st-lists' test called Set/Exists/Type from st-strings and st-keys, dispatched in
    the SAME wave, so no item of that wave could pass its own gate alone and the forks' test-cycle repair
    agents implemented each other's files to force it (run a33d7061)."""
    deps = _transitive_deps(items, gid)

    def named(ids):
        return "; ".join(f"{o} ({', '.join(_list(_mapping(items.get(o)).get('files'))) or 'no files'})"
                         for o in ids)
    rely = named(sorted(d for d in deps if d in items))
    others = named(o for o in sorted(items) if o != gid and o not in deps)
    return (
        "These tests run on the implementer's fork BEFORE sibling items land, so they must be satisfiable "
        "by this item's files plus what is already in the repository"
        + (" and this item's dependencies." if rely else ".") + "\n"
        + (f"Dependencies built first (you may rely on them): {rely}\n" if rely else "")
        + (f"Built concurrently or later (their files and APIs do NOT exist when these tests run; do not "
           f"import, call or stub them): {others}\n" if others else "")
        + "A criterion that cannot be checked without them: cover the part that can and leave the rest to "
          "the integrated-branch suite.\n"
        # sv-core's test asserted PING is an unknown command because no sibling had registered it yet;
        # sv-connection then registered PING and the frozen test was red at the integrated head (run
        # 299b4907). Every frozen test is re-run on the shared branch after each wave's merges.
        + "These tests are re-run on the shared branch after every item lands, so never assert that a "
          "sibling's behaviour is ABSENT (a command it will register is unknown, a field it adds is "
          "missing, a name it will claim errors): such a test passes on the fork and fails once the "
          "sibling lands. For 'unknown'/'missing' cases use names and inputs no item will ever claim.\n")


@worker_task(task_definition_name="team_acceptance_build")
def team_acceptance_build(task):
    """Before any implementer is dispatched, a DIFFERENT agent writes each item's acceptance tests from
    its acceptance criteria (H13: the author of the code is never the author of its check). One
    read-mostly ``coding_agent`` per item that has criteria and no frozen tests yet; each may write only
    its own test file. The next tasks commit them, prove them red at the base, and freeze them."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip() or None
        repo = str(i.get("repoPath") or "").strip()
        enabled = _truth(i.get("authorAcceptanceTests"), True)
        pattern = str(i.get("acceptanceTestPattern") or "tests/acceptance/test_accept_{id}.py")
        items = team_board.fold_board(cid)["items"] if cid else {}
        done_section = _done_section(repo)
        if done_section:
            done_section += ("\nYour tests drive the program EXACTLY as these checks do: the same invocation syntax, "
                             "arguments, paths, ports and output format. A test that invents a second syntax beside "
                             "the checks' fails a correct program.\n")
        dynamic_tasks, dynamic_inputs, authored, reused = [], {}, {}, {}
        for gid, it in sorted(items.items()):
            criteria = _list(it.get("acceptanceCriteria"))
            # A documents deliverable has no executable check: its criteria are judged at peer review.
            if not enabled or not criteria or _list(it.get("acceptanceTests")) \
                    or team_board.deliverable_of(it) == "docs":
                continue
            path = _acceptance_path(pattern, gid)
            if repo and os.path.isfile(os.path.join(repo, path)):
                # A program re-runs a team whose first attempt ended INCOMPLETE; that attempt's frozen
                # acceptance test is on the branch, already proven red before its implementation. It stays
                # the item's check. Re-authoring it would pass at once (the code exists), be judged
                # tautological, and be deleted from the branch.
                reused[gid] = path
                continue
            ref = f"accept_{re.sub(r'[^A-Za-z0-9_]', '_', gid)}"
            # Each author writes in ITS OWN worktree: concurrent writers in one worktree revert each
            # other's files at their write-scope check (measured live, run 25fe604e).
            author_wt = git.worktree_add(repo, ref)["worktreePath"] if repo and os.path.isdir(repo) else repo
            prompt_context = {"item": gid, "path": path, "isolation": _acceptance_isolation(items, gid),
                              "description": it.get("description") or it.get("title") or gid,
                              "files": ", ".join(_list(it.get("files"))) or "(unspecified)",
                              "criteria": "\n".join(f"- {c}" for c in criteria), "doneSection": done_section}
            prompt = templating.render_default("team_acceptance_tests", prompt_context)
            dynamic_tasks.append({"name": "coding_agent", "taskReferenceName": ref, "type": "SIMPLE",
                                  "optional": True})
            dynamic_inputs[ref] = {
                "worktreePath": author_wt, "tools": ["Read", "Grep", "Glob", "Write", "Edit"],
                "allowedWriteRoots": [path], "templateKey": "team_acceptance_tests", "prompt": prompt,
                "promptPath": prompt_files.stash(repo, ref, prompt, prompt_context),
                "promptContext": prompt_context, "promptTemplate": "", "promptTemplateSource": "",
                "model": i.get("codeModel") or "", "maxTurns": i.get("authorMaxTurns") or 40,
                "maxBudgetUsd": i.get("authorMaxBudgetUsd") or 2, "includeFileTree": True,
                "modelProfile": i.get("modelProfile") or "", "modelPolicy": i.get("modelPolicy") or {},
                "modelPolicySource": i.get("modelPolicySource") or "",
                "modelPolicySha256": i.get("modelPolicySha256") or "",
                "modelsConfig": i.get("modelsConfig") or "", "modelOverrides": i.get("modelOverrides") or {},
            }
            authored[gid] = path
        if reused and cid:
            for gid, path in reused.items():
                cur = team_board.fold_board(cid)["items"].get(gid, {"id": gid})
                # The command comes from the repository's own test tooling for that file, as a fresh
                # test's does; a hard-coded pytest ran Go acceptance tests under pytest (review 2).
                commands = verification.commands_for_test_paths(repo, [path])
                if not commands:
                    team_board.post_exception(cid, "acceptance", gid, "acceptance_command",
                                              f"no test tooling of this repository runs the reused acceptance "
                                              f"test {path}; the item cannot be proven by it", severity="high")
                team_board.post_item(cid, "acceptance", {**cur, "acceptanceTests": [path], "testCommands": commands})
            cur = contract_event.current_contract(file_ownership.bb.read_facts(cid, kind=contract_event.CONTRACT_KIND))
            frozen = sorted(set(reused.values()))
            file_ownership.bb.post_fact(cid, "lead", contract_event.CONTRACT_KIND, {
                "paths": cur["paths"] + [p for p in frozen if p not in cur["paths"]], "frozen": True,
                "revision": max(cur["revision"], 1), "acceptanceTests": frozen,
                "acceptanceCommit": git.head(repo)})
        if cid and not authored:
            file_ownership.bb.post_fact(cid, "acceptance", ACCEPTANCE_SETTLED_KIND,
                                        {"authored": 0, "reused": sorted(reused)})
        return ok(task, {"dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
                         "authored": authored, "count": len(authored), "reused": reused,
                         "gate": "author" if authored else "skip"},
                  [f"[team_acceptance_build] authors={len(authored)} {sorted(authored)} reused={sorted(reused)}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_acceptance_build", e)


@worker_task(task_definition_name="team_acceptance_collect")
def team_acceptance_collect(task):
    """Gather each author's test file from its isolated worktree into the shared branch and commit them
    together (the commit the raw red run then checks). Author worktrees are removed afterwards."""
    i = task.input_data or {}
    try:
        repo = str(i.get("repoPath") or "").strip()
        authored = {str(k): str(v) for k, v in _mapping(i.get("authored")).items()}
        collected = []
        for gid, path in sorted(authored.items()):
            ref = f"accept_{re.sub(r'[^A-Za-z0-9_]', '_', gid)}"
            src = os.path.join(_subtask_worktree(repo, ref), path)
            if os.path.isfile(src):
                dst = os.path.join(repo, path)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(src, "rb") as fin, open(dst, "wb") as fout:
                    fout.write(fin.read())
                collected.append(path)
            git.worktree_remove(repo, ref)
            git.git(repo, "branch", "-D", git.group_branch(repo, ref), check=False)
        commit = ""
        if collected:
            # The shared worktree is also the integrators' target while the first wave runs: stage and
            # commit under the repository lock, only the authored files.
            out = git.commit(repo, "team: frozen acceptance tests (authored before implementation, H13)",
                             force_add_paths=collected, write_roots=sorted({os.path.dirname(p) or "." for p in collected}))
            commit = str(out.get("commit") or git.head(repo))
        return ok(task, {"commit": commit, "collected": collected, "noOp": not collected},
                  [f"[team_acceptance_collect] collected={collected} commit={commit[:12]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_acceptance_collect", e)


def _file_verdicts(paths: dict, commands: list, overall: str) -> dict:
    """Pure: per acceptance-test file, ``red`` (its command failed — the test bites), ``green`` (it passed
    before the feature exists — tautological) or ``missing``. A command that names the file decides it;
    otherwise the run's overall state does."""
    out = {}
    for gid, path in paths.items():
        naming = [c for c in commands or [] if any(str(path) in str(a) for a in _list(_mapping(c).get("argv")))]
        if naming:
            out[gid] = "green" if all(int(_mapping(c).get("exitCode") or 0) == 0 for c in naming) else "red"
        else:
            out[gid] = "green" if overall == "passed" else ("red" if overall == "failed" else "unknown")
    return out


def _own_argv(argv, others: list[str]) -> list:
    """An item's own green gate: the red-run command with every OTHER item's acceptance test removed.
    Discovery batches all authored files into one command; left as-is, every item's gate ran every item's
    tests, so the first item's repair agent implemented the not-yet-dispatched items to turn them green
    and those items then had nothing left to deliver (run 3e502ddc, store team 908202e9)."""
    def foreign(arg) -> bool:
        a = str(arg)
        return any(a == p or a.startswith(p + "::") for p in others)
    return [a for a in _list(argv) if not foreign(a)]


@worker_task(task_definition_name="team_acceptance_commit")
def team_acceptance_commit(task):
    """Freeze the acceptance tests that bite (H13). Each authored test file is judged from the raw red
    run at the base: ``red`` → kept, added to the frozen contract (so an implementer's edit of it is a
    controlled event, never merged) and recorded on its item with the commands that run it; ``green``
    (passes before the feature exists — tautological) → removed and a lesson posted; never authored →
    the item proceeds without one. Records every author's spend."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip() or None
        repo = str(i.get("repoPath") or "").strip()
        authored = {str(k): str(v) for k, v in _mapping(i.get("authored")).items()}
        run = _mapping(i.get("run"))
        overall = str(run.get("verificationState") or "")
        commands = _list(run.get("commands"))
        present = {p for p in authored.values()
                   if git.git(repo, "ls-files", "--error-unmatch", p, check=False).code == 0}
        verdicts = _file_verdicts({g: p for g, p in authored.items() if p in present}, commands, overall)
        kept, discarded, absent = {}, [], [g for g, p in authored.items() if p not in present]
        board_items = team_board.fold_board(cid)["items"] if cid else {}
        green_before_work: list[str] = []
        for gid, verdict in verdicts.items():
            path = authored[gid]
            files = [str(f) for f in _list(_mapping(board_items.get(gid)).get("files")) if str(f).strip()]
            # Every file the item would create or edit already exists on the branch: the work may have
            # been done before (a continuation, a repository that already holds it). A test that passes
            # there is not a tautology but the item's gate; discarding it left every fork of storage-core
            # with nothing to run and four minutes of discovery fallback per round (run c4a9237d).
            exists = bool(files) and all(git.git(repo, "ls-files", "--error-unmatch", f, check=False).code == 0
                                         for f in files)
            if verdict == "green" and not exists:
                git.git(repo, "rm", "-q", path, check=False)
                discarded.append(gid)
                if cid:
                    team_board.post_lesson(cid, "acceptance", f"acceptance test {path} for '{gid}' passed before "
                                           f"the feature existed (tautological) and was discarded",
                                           confidence="high", provenance="acceptance_test_tautology", refs=[path])
            else:
                if verdict == "green":
                    green_before_work.append(gid)
                    if cid:
                        team_board.post_lesson(cid, "acceptance", f"acceptance test {path} for '{gid}' passed before "
                                               f"any work; the item's files already exist, so it is kept as the gate "
                                               f"and the implementer confirms the criteria are met",
                                               confidence="medium", provenance="acceptance_green_before_work", refs=[path])
                naming = [_mapping(c) for c in commands
                          if any(path in str(a) for a in _list(_mapping(c).get("argv")))]
                others = [p for g, p in authored.items() if g != gid]
                kept[gid] = {"path": path, "commands": [{"argv": _own_argv(c.get("argv"), others)} for c in naming]
                             or [{"argv": ["pytest", "-q", path]}]}
        if discarded:
            git.git(repo, "-c", "user.name=conductor-team", "-c", "user.email=team@conductor.local",
                    "commit", "-q", "-m", "team: discard tautological acceptance tests", check=False)
        head = git.head(repo) if repo else ""
        # Authored beside the first wave, a test can land after its item was delivered and reviewed:
        # red at that head, it re-opens the item now (the same re-dispatch as a red integration check),
        # so the implementer makes it pass before the team calls itself done instead of a final-cycle
        # repair agent (134 s, run 993dce38). An item still open runs it on its fork's gate as usual.
        reopened: list[str] = []
        for gid, verdict in verdicts.items():
            item = _mapping(board_items.get(gid))
            if verdict != "red" or item.get("state") not in (team_board.DONE, team_board.REVIEW) or not cid:
                continue
            path = authored[gid]
            tail = "\n".join(str(_mapping(c).get("output") or "")[-1500:] for c in commands
                             if any(path in str(a) for a in _list(_mapping(c).get("argv"))))
            team_board.set_state(cid, "acceptance", gid, team_board.REOPENED, attempt=int(item.get("attempt") or 0),
                                 reason=f"{CHECK_REOPEN}: frozen acceptance test {path} landed after the item was "
                                        f"delivered and fails at {head[:12]}")
            team_board.post_directive(
                cid, "acceptance", gid,
                f"Its frozen acceptance test {path} (authored beside your first attempt, now on the branch) FAILS "
                f"at {head[:12]} against what you delivered. Make it pass: fix the implementation, or — if an "
                f"assertion contradicts the design — amend it citing the design; the peer reviewer decides the "
                f"amendment. Failing output:\n{tail}", authority="agent")
            team_board.post_exception(cid, "acceptance", gid, CHECK_REOPEN,
                                      f"frozen test {path} red at {head[:12]} after '{gid}' was delivered", severity="info")
            team_board.resolve_exception(cid, "acceptance", gid, CHECK_REOPEN, "re-dispatched with the failing output")
            reopened.append(gid)
        contract_paths = []
        if cid:
            for gid, rec in kept.items():
                cur = team_board.fold_board(cid)["items"].get(gid, {"id": gid})
                team_board.post_item(cid, "acceptance", {**cur, "acceptanceTests": [rec["path"]],
                                                         "testCommands": rec["commands"],
                                                         "greenBeforeWork": gid in green_before_work})
            cur = contract_event.current_contract(file_ownership.bb.read_facts(cid, kind=contract_event.CONTRACT_KIND))
            contract_paths = cur["paths"] + [r["path"] for r in kept.values() if r["path"] not in cur["paths"]]
            if kept:
                file_ownership.bb.post_fact(cid, "lead", contract_event.CONTRACT_KIND, {
                    "paths": contract_paths, "frozen": True, "revision": max(cur["revision"], 1),
                    # Tests reused from a previous attempt of this team stay frozen alongside the new ones.
                    "acceptanceTests": sorted({r["path"] for r in kept.values()}
                                              | set(_frozen_acceptance(cid).get("acceptanceTests") or [])),
                    "acceptanceCommit": head})
            for ref, out in _iter_joined(i.get("joined")):
                _record(cid, "acceptance:" + re.sub(r"__[0-9]+$", "", str(ref)).removeprefix("accept_"), out)
        if cid:
            file_ownership.bb.post_fact(cid, "acceptance", ACCEPTANCE_SETTLED_KIND,
                                        {"authored": len(authored), "kept": sorted(kept), "discarded": discarded,
                                         "acceptanceCommit": head})
        return ok(task, {"kept": kept, "discarded": discarded, "absent": absent, "verdicts": verdicts,
                         "greenBeforeWork": green_before_work, "acceptanceCommit": head, "contractPaths": contract_paths,
                         "reopened": reopened},
                  [f"[team_acceptance_commit] kept={sorted(kept)} discarded={discarded} absent={absent} "
                   f"run={overall} reopened={reopened}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_acceptance_commit", e)


# ─────────────────────────────── team_monitor (Phase 3) ───────────────────────
@worker_task(task_definition_name="team_monitor")
def team_monitor(task):
    """The monitor role (roadmap Phase 3, §4.5): watch the board each wave with BOUNDED authority.
    Applies small corrections autonomously (nudge a thrashing item, re-prioritize it) and ESCALATES
    anything bigger to the exception queue (a blocked item, duplication, a budget breach) for a human
    to drive from the console/TUI. Its primary output is the human-digestible rollup + the exception
    count (H7). Never kills work, expands scope, or spends on its own."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip() or None
        ceiling = _num(i.get("ceilingUsd"))
        stall_attempts = max(int(i.get("stallAttempts") or 2), 1)
        quality_changes = max(int(i.get("qualityChanges") or 2), 1)
        if not cid:
            return ok(task, {"openExceptions": 0, "actionsApplied": 0, "signals": [], "rollup": {}},
                      ["[team_monitor] no correlationId; nothing to watch"])

        board = team_board.fold_board(cid)
        board["owners"] = file_ownership.owner_map(cid)
        board["directives"] = _steering(cid, board)
        spend = budget_gate.read_spend(cid)["usd"]
        found = monitor.detect(board, spend_usd=spend, ceiling_usd=ceiling,
                               stall_attempts=stall_attempts, quality_changes=quality_changes,
                               max_human_constraints=max(int(i.get("maxHumanConstraints") or 3), 1))

        applied = 0
        for act in found["autonomousActions"]:
            op = act.get("op")
            if op == "nudge":
                team_board.post_directive(cid, "monitor", str(act.get("item")), str(act.get("text")),
                                          authority="monitor")
                applied += 1
            elif op == "reprioritize":
                cur = board["items"].get(str(act.get("item")), {"id": act.get("item")})
                team_board.post_item(cid, "monitor", {**cur, "priority": act.get("priority")})
                applied += 1

        # Escalate, de-duplicated against the currently-open queue (don't re-post the same escalation).
        # A 'blocked' escalation is posted once per ENTRY into the blocked state: once a human resolved
        # it, the same blocked spell is not re-escalated (that would re-arm the human gate forever); a
        # later re-block after a reopen is a new spell and is escalated again.
        already = {(str(e.get("item")), str(e.get("kind")))
                   for e in team_board.open_exceptions(board.get("exceptions") or [])}
        spells: dict[str, int] = {}
        for f in file_ownership.bb.read_facts(cid, kind=team_board.STATE_KIND):
            d = _mapping(f.get("data"))
            if d.get("state") == team_board.BLOCKED:
                spells[str(d.get("item"))] = spells.get(str(d.get("item")), 0) + 1
        escalated: dict[str, int] = {}
        for e in board.get("exceptions") or []:
            e = _mapping(e)
            if not e.get("resolved"):
                escalated[str(e.get("item"))] = escalated.get(str(e.get("item")), 0) + 1
        posted = 0
        for exc in found["exceptions"]:
            key = (str(exc.get("item")), str(exc.get("kind")))
            if key in already:
                continue
            if key[1] == "blocked" and escalated.get(key[0], 0) >= spells.get(key[0], 0):
                continue  # this blocked spell was already escalated (by the monitor or the integrator)
            team_board.post_exception(cid, "monitor", str(exc.get("item")), str(exc.get("kind")),
                                      str(exc.get("detail")), severity=str(exc.get("severity") or "warn"))
            posted += 1

        after = team_board.fold_board(cid)
        open_exc = team_board.open_exceptions(after.get("exceptions") or [])
        # End-of-wave liveness (WP1.4, H2): can any work move next wave? If not, the run is starved and
        # exits naming why. It never waits for a human — humans are passive observers of the swarm.
        # Ready work the Lead could not dispatch (held for a file an open item owns) with nothing in
        # flight is the same wave again next time: the store team spun 14 empty waves to maxWaves on
        # one such hold (run a4f1ff1f). Each hold is posted as a scope_wait escalation and the run ends.
        dispatched = i.get("dispatchedCount")
        held = [str(h) for h in _list(i.get("scopeWaits")) if str(h).strip()]
        stuck = team_board.starvation(after["items"], open_exc,
                                      dispatched=int(dispatched) if dispatched not in (None, "") else None,
                                      held=held)
        # A deadlock is the Lead's to break, never a reason to stop: someone is assigned to each item at
        # its root, and an item waiting on work that can never land stops waiting for it.
        max_rescues = max(int(i.get("maxRescues") if i.get("maxRescues") not in (None, "") else 2), 0)
        if stuck["starved"] and max_rescues > 0:
            roots = [h.split(" waits for ", 1)[0] for h in held] if stuck["starvedBy"] == "scope_wait" else []
            roots = roots or sorted(g for g, it in after["items"].items() if it.get("state") == team_board.BLOCKED)
            for gid in roots:
                _rescue(cid, gid, f"deadlock: {stuck['detail']}", max_rescues=max_rescues)
            if not roots:  # a dependency on no real item, or a cycle: drop exactly those edges
                for gid, dead in team_board.dead_dependencies(after["items"]).items():
                    keep = [str(d) for d in _list(after["items"][gid].get("dependsOn")) if str(d) not in dead]
                    team_board.post_item(cid, "lead", {**after["items"][gid], "id": gid, "dependsOn": keep,
                                                       "replaceDependsOn": True})
                    team_board.post_exception(cid, "lead", gid, "deadlock",
                                              f"{gid} no longer waits for {', '.join(dead)}: that dependency can "
                                              f"never land ({stuck['detail'][:300]})", severity="info")
                    team_board.resolve_exception(cid, "lead", gid, "deadlock", "dependency dropped by the Lead")
            after = team_board.fold_board(cid)
            open_exc = team_board.open_exceptions(after.get("exceptions") or [])
            stuck = team_board.starvation(after["items"], open_exc)
        if stuck["starved"] and stuck["starvedBy"] == "scope_wait":
            for h in held:
                team_board.post_exception(cid, "monitor", h.split(" waits for ", 1)[0], "scope_wait", h,
                                          severity="high")
                posted += 1
            open_exc = team_board.open_exceptions(team_board.fold_board(cid).get("exceptions") or [])
        return ok(task, {
            "openExceptions": len(open_exc), "newExceptions": posted, "actionsApplied": applied,
            "signals": found["signals"], "rollup": found["rollup"],
            "starved": stuck["starved"], "starvedBy": stuck["starvedBy"], "starvedDetail": stuck["detail"],
        }, [f"[team_monitor] signals={len(found['signals'])} autoActions={applied} "
            f"newExceptions={posted} openExceptions={len(open_exc)} starved={stuck['starved']} {stuck['detail']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_monitor", e)


# ─────────────────────────────── team_outcome ─────────────────────────────────
@worker_task(task_definition_name="team_outcome")
def team_outcome(task):
    """Fold the whole run into an honest delivered / not-delivered verdict (the reviewed-branch result,
    §5). DELIVERED iff every tracking-board item reached ``done`` AND the shared branch is green under
    test_cycle. Otherwise a precise mechanism: blocked items, a budget halt leaving work undone, or a
    branch that is not green. Counts come from the durable board, so the verdict is an audit."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip() or None
        final_head = str(i.get("finalHead") or "")
        tests_passed = _truth(i.get("testsPassed"), False)
        test_state = str(i.get("testCycleState") or "not_run")

        # The final test cycle ran on its own fork of the shared branch (`tests_fork`), never in the
        # shared worktree: a session there is reset to the head it started from when it ends, and the
        # store team's repair agent so rewrote the program branch from a commit before the resp team's
        # r-reader merge, then left an untracked writer.go that aborted r-writer's merge — the resp
        # team failed on a git error (run a4f1ff1f). A green repair is integrated like any fork
        # (rebase + fast-forward); a red one, or one that no longer applies, is discarded.
        shared_repo = str(i.get("sharedRepo") or i.get("repoPath") or "")
        shared_branch = str(i.get("sharedBranch") or "")
        tests_wt = str(i.get("testsWorktree") or "")
        tests_branch = str(i.get("testsBranch") or "")
        # The verdict is about the branch as it IS: a frozen test or the program's definition of done
        # committed after the last wave's variables were captured moves the head past the recorded
        # one, and a tamper diff from a newer commit to an older head reports the newer commit's own
        # files as "modified" (run 884f7eec, the recorded head predated the acceptance commit).
        if shared_repo and os.path.isdir(shared_repo):
            final_head = git.head(shared_repo) or final_head
        repair_integrated, repair_dropped = False, ""
        if shared_repo and os.path.isdir(shared_repo) and tests_wt and os.path.isdir(tests_wt) and tests_branch:
            ahead = git.git(tests_wt, "rev-list", "--count", f"{shared_branch}..{tests_branch}",
                            check=False).stdout.strip() if shared_branch else "0"
            if ahead not in ("", "0"):
                if tests_passed:
                    res = continuous_integrate.integrate_ready(
                        shared_repo, shared_branch, [{"id": "tests", "worktree": tests_wt, "branch": tests_branch,
                                                      "readyAt": 0}])
                    repair_integrated = bool(res.get("integrated"))
                    if repair_integrated:
                        final_head = str(res.get("final_head") or git.head(shared_repo))
                    else:
                        repair_dropped = "; ".join(str(_mapping(r).get("mechanism") or "rebase conflict")
                                                   for r in _list(res.get("reopened"))) or "rebase conflict"
                else:
                    repair_dropped = f"the final test cycle's repair left the branch red ({test_state})"
            git.worktree_remove_path(shared_repo, tests_wt)
            git.git(shared_repo, "branch", "-D", tests_branch, check=False)

        board = team_board.fold_board(cid) if cid else {"items": {}, "reviews": [], "lessons": []}
        items = board["items"]
        rollup = team_board.board_rollup(board)
        total = rollup["total"]
        done = rollup["done"]
        blocked_ids = [gid for gid, it in items.items() if it.get("state") == team_board.BLOCKED]
        open_ids = [gid for gid, it in items.items()
                    if it.get("state") in team_board.OPEN_STATES]
        open_exc = team_board.open_exceptions(board.get("exceptions") or [])
        if cid:
            # Test-repair agents in the final test_cycle are agents too (H5): record their spend.
            for rep_ in _list(i.get("testRepairReports")):
                _record(cid, "tests", _mapping(rep_))
        spend = budget_gate.read_spend(cid) if cid else {"usd": 0.0, "by_author": {}}
        spend_usd = spend["usd"]
        stuck = team_board.starvation(items, open_exc)

        # H13 tamper guard: the frozen acceptance tests must be byte-identical at the delivered head
        # (no implementer or final test-repair agent may have edited its own check).
        tampered, unproven = [], []
        if cid:
            cur = file_ownership.bb.read_facts(cid, kind=contract_event.CONTRACT_KIND)
            acc = [_mapping(f.get("data")) for f in cur if _mapping(f.get("data")).get("acceptanceCommit")]
            repo = str(i.get("repoPath") or "")
            if acc and repo and final_head and os.path.isdir(repo):
                last = acc[-1]
                amended = _mapping(last.get("amendedAt"))  # reviewer-approved amendments re-freeze per path
                by_base: dict[str, list[str]] = {}
                for p in _list(last.get("acceptanceTests")):
                    by_base.setdefault(str(amended.get(str(p)) or last["acceptanceCommit"]), []).append(str(p))
                for base_commit, paths in by_base.items():
                    diff = git.git(repo, "diff", "--name-only", base_commit, final_head, "--", *paths,
                                   check=False).stdout
                    tampered += [p for p in diff.splitlines() if p.strip()]
            # The final test cycle must have RUN every done item's frozen tests at the delivered head: a
            # green `make test` alone proved nothing about them (2 of 290 red, program delivered, run
            # 299b4907). The commands it ran are the evidence, not a flag.
            ran = [" ".join(str(a) for a in _list(_mapping(c).get("argv")))
                   for c in _list(i.get("testCommandsRan"))]
            unproven = [str(p) for gid, it in sorted(items.items()) if it.get("state") == team_board.DONE
                        for p in _list(it.get("acceptanceTests")) if not any(str(p) in r for r in ran)]
        delivered = (bool(total) and done == total and tests_passed and not tampered and not unproven
                     and not repair_dropped)
        verdict = "DELIVERED" if delivered else "INCOMPLETE"
        rungs_recorded = _record_rung_outcomes(cid, shared_repo, items)
        if delivered:
            report = (f"DELIVERED: all {total} work items reached done on the shared branch "
                      f"(continuous rebase, zero big-bang merge); branch green "
                      f"(test_cycle={test_state}). Head {final_head[:12]}. Peer-reviewed; "
                      f"{rollup['lessons']} lessons shared.")
        else:
            reasons = []
            if blocked_ids:
                reasons.append(f"{len(blocked_ids)} item(s) blocked (" + ", ".join(
                    f"{b}: {items[b].get('reason') or 'terminal failure'}" for b in blocked_ids) + ")")
            if open_ids:
                why = (stuck["detail"] if stuck["starved"] else
                       "budget halt" if str(i.get("budgetState")) == "halt" else "max waves reached")
                reasons.append(f"{len(open_ids)} item(s) left unresolved ({', '.join(open_ids)}) — {why}")
            if open_exc:
                reasons.append(f"{len(open_exc)} escalation(s) left open ("
                               + ", ".join(f"{e.get('item')}:{e.get('kind')}" for e in open_exc) + ")")
            if not tests_passed:
                reasons.append(f"shared branch not green (test_cycle={test_state})")
            if tampered:
                reasons.append(f"frozen acceptance test(s) modified after they were frozen: {', '.join(tampered)}")
            if unproven:
                reasons.append(f"the final test cycle ran no command for frozen acceptance test(s) "
                               f"{', '.join(unproven)}; the delivered head is unproven against its check")
            if repair_dropped:
                reasons.append(f"the final test cycle's repair commit was not integrated ({repair_dropped})")
            report = "INCOMPLETE: " + "; ".join(reasons or [f"only {done}/{total} items done"]) + \
                     f". Head {final_head[:12]}."

        return ok(task, {
            "delivered": delivered, "verdict": verdict, "finalHead": final_head, "rungOutcomes": rungs_recorded,
            "total": total, "doneCount": done, "blockedCount": len(blocked_ids),
            "openCount": len(open_ids), "blocked": blocked_ids, "rollup": rollup,
            "openExceptions": len(open_exc), "exceptions": open_exc,
            "bigBangMerge": False, "testCycleState": test_state, "testsPassed": tests_passed,
            "spendUsd": spend_usd, "spendByAuthor": spend.get("by_author", {}),
            "spendByRole": budget_gate.by_role(spend.get("by_author", {})),
            "starvedBy": stuck["starvedBy"], "acceptanceTampered": tampered, "acceptanceUnproven": unproven,
            "repairIntegrated": repair_integrated, "repairDropped": repair_dropped, "report": report,
        }, [f"[team_outcome] {verdict} — {report} spend=${spend_usd} repairIntegrated={repair_integrated}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "team_outcome", e)
