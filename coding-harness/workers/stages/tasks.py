"""Stage front-door & bug-depth workers (roadmap Phase 4): sdlc_intake (G1) and bug_fix (G2).

Thin wrappers over the pure logic in ``common/intake.py`` (classification→route) and
``common/bug_fix.py`` (reproduction-first red/green gate + verdict). Classification itself is an LLM
judgment done by a ``coding_agent`` task in the workflow; everything decidable is pure and here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from conductor.client.worker.worker_task import worker_task

from common import (architect, bootstrap, budget_gate, bug_fix, decomposition, git, intake, program, repo_memory,
                    retrospective, team_board)
from common import blackboard as bb
from common.results import fail, ok
from common.architect import _num


def _list(v):
    return v if isinstance(v, list) else ([v] if v else [])


def _mapping(v):
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _truth(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _structured(out: dict) -> dict:
    """Extract a coding_agent's structured JSON output (under ``structured`` / ``result`` / top level)."""
    out = _mapping(out)
    for src in (out.get("structured"), out.get("result"), out):
        obj = src
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except (ValueError, TypeError):
                continue
        if isinstance(obj, dict) and ("kind" in obj or "instruction" in obj or "subtasks" in obj):
            return obj
    return {}


# ─────────────────────────────── sdlc_intake (G1) ─────────────────────────────
@worker_task(task_definition_name="intake_route")
def intake_route(task):
    """Fold the classifier's output into the routing decision the intake SWITCH acts on (G1), by kind
    and decomposition mode. Humans are passive observers: low confidence never pauses the run."""
    i = task.input_data or {}
    try:
        classification = _structured(i.get("classification")) or _mapping(i.get("classification"))
        plan = intake.plan_route(
            classification,
            threshold=float(i.get("confidenceThreshold") or intake.DEFAULT_CONFIDENCE_THRESHOLD),
            force_single=_truth(i.get("forceSingle")), force_team=_truth(i.get("forceTeam")),
            require_code=_truth(i.get("requireCode")))
        # The planner is an agent (H5): carry its spend — summed over replan rounds — to the engine.
        raw, prior = _mapping(i.get("classification")), _mapping(i.get("priorPlanUsage"))
        num = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0  # noqa: E731
        plan["planUsage"] = {"costUsd": round(num(prior.get("costUsd")) + num(raw.get("costUsd")), 6),
                             "tokenUsed": int(num(prior.get("tokenUsed")) + num(raw.get("tokenUsed")))}
        # The Architect's draft of the definition of done travels with the plan (cc_program), and a
        # placeholder claim rejects the plan the way a placeholder item does.
        draft = _mapping(classification.get("contract"))
        plan["contractDraft"] = draft
        problems = architect.draft_problems(draft) if _truth(i.get("requireCode")) else []
        if problems:
            plan["route"] = "replan"
            plan["replanReasons"] = [*_list(plan.get("replanReasons")), *problems]
            plan["replanPrompt"] = decomposition.replan_prompt(plan["replanReasons"])
        return ok(task, plan, [f"[intake_route] kind={plan['kind']} route={plan['route']} mode={plan['mode']} "
                               f"conf={plan['confidence']} items={plan['itemCount']} "
                               f"decomposition={plan['decompositionVerdict']} {plan['replanReasons']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "intake_route", e)


# ─────────────────────────────── cc_program + repo_memory (G4/G5) ─────────────
@worker_task(task_definition_name="repo_memory_read")
def repo_memory_read(task):
    """Read on-branch repo_memory, decay stale lessons (H10 — a lesson whose referenced files are gone
    is invalidated, never trusted), and return a prompt-ready digest of the trustworthy knowledge for
    this run's planner. The cross-run face of the learning board (G5)."""
    i = task.input_data or {}
    try:
        repo = str(i.get("repoPath") or "").strip()
        # A program may start in a fresh directory (greenfield). Every later step needs a git repository
        # with a HEAD; without one the planner failed four times on `git rev-parse HEAD` and the run
        # ended before planning (run edc1c893, an empty directory). Initialize it here, the program's
        # first step on the repo: an empty base commit, the directory's files left as they are.
        prepared = git.ensure_repo(repo)
        mem = repo_memory.read_memory(repo)
        decayed = repo_memory.decay_lessons(mem, repo)
        mem = decayed["memory"]
        live = repo_memory.live_lessons(mem)
        # Learnings from earlier runs' retrospectives (this repo, this host, its languages, process).
        learned = retrospective.relevant(repo=retrospective.repo_key(repo),
                                         languages=retrospective.detect_languages(repo),
                                         host_name=retrospective.host())
        digest = "\n".join(x for x in (repo_memory.digest(mem), retrospective.digest(learned)) if x)
        return ok(task, {"memory": mem, "digest": digest, "learnings": len(learned),
                         "liveLessons": len(live), "invalidated": decayed["invalidated"],
                         "checked": decayed["checked"], "hasMemory": bool(mem.get("lessons") or mem.get("invariants")),
                         "initializedRepo": bool(prepared)},
                  [f"[repo_memory_read] live={len(live)} invalidated={decayed['invalidated']} "
                   f"checked={decayed['checked']}" + (f" initialized git repo {repo}" if prepared else "")])
    except Exception as e:  # noqa: BLE001
        return fail(task, "repo_memory_read", e)


@worker_task(task_definition_name="repo_memory_write")
def repo_memory_write(task):
    """Persist this run's lessons onto the branch and decay stale ones (G5/H10). Folds the durable
    learning board (the run's lessons) into repo_memory, re-runs decay, writes and commits the file so
    later runs read it natively. Lessons may be passed directly or read from the run's board."""
    i = task.input_data or {}
    try:
        repo = str(i.get("repoPath") or "").strip()
        cid = str(i.get("correlationId") or "").strip() or None
        new_lessons = _list(i.get("lessons"))
        if cid and not new_lessons:
            from common import team_board
            new_lessons = team_board.fold_board(cid).get("lessons", [])
        invariants = _list(i.get("invariants"))
        mem = repo_memory.read_memory(repo)
        if invariants:
            existing = {str(x) for x in mem.get("invariants", [])}
            mem["invariants"] = list(mem.get("invariants", [])) + [str(x) for x in invariants
                                                                   if str(x) not in existing]
        mem = repo_memory.merge_lessons(mem, new_lessons)
        decayed = repo_memory.decay_lessons(mem, repo)
        mem = decayed["memory"]
        repo_memory.write_memory(repo, mem)
        committed = ""
        try:
            out = git.commit(repo, "repo_memory: persist lessons + decay stale knowledge")
            committed = out.get("commit", "")
        except Exception:  # noqa: BLE001
            pass
        return ok(task, {"written": True, "lessons": len(mem.get("lessons", [])),
                         "invalidated": decayed["invalidated"], "commit": committed},
                  [f"[repo_memory_write] lessons={len(mem.get('lessons', []))} "
                   f"invalidated={decayed['invalidated']} commit={committed[:12]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "repo_memory_write", e)


@worker_task(task_definition_name="program_outcome")
def program_outcome(task):
    """Fold a program build into an honest report (G4): the dependency ranks, whether every subsystem
    landed in order, and whether cross-subsystem integration is green."""
    i = task.input_data or {}
    try:
        subsystems = _list(i.get("subsystems"))
        cid = str(i.get("correlationId") or "").strip()
        if cid:
            # Teams of teams (WP5): the verdict is folded from the PROGRAM board — every subsystem's child
            # team delivered — plus the cross-subsystem integration test on the program branch.
            board = team_board.fold_board(cid)
            subs = {s: it for s, it in board["items"].items() if it.get("kind") == "subsystem"}
            done = sum(1 for it in subs.values() if it.get("state") == team_board.DONE)
            tests_ok = _truth(i.get("testsPassed"))
            i = {**i, "delivered": bool(subs) and done == len(subs) and tests_ok, "doneCount": done,
                 "total": len(subs),
                 "verdict": "; ".join(f"{s}: {it.get('reason')}" for s, it in sorted(subs.items())
                                      if it.get("state") != team_board.DONE) or "all subsystems delivered"}
        arch = architect
        contract = arch.contract_of(cid) if cid else {}
        if contract:
            # The Architect's verdict: delivered when its final evaluation reached the threshold (or an
            # override stands); otherwise the run ends naming its needs, never a graded failure.
            finals = arch.evaluations_of(cid, "final")
            last = finals[-1] if finals else {}
            results = arch.results_of(cid)
            s = float(last.get("score")) if last.get("score") is not None else architect.score(contract, results)
            delivered = last.get("rung") == architect.DELIVER
            needs = _list(last.get("needs"))
            if not delivered and not needs:
                red = architect.red_checks(contract, results)
                needs = architect.needs_of(contract, results, budget_spent=False, spent=0.0, ceiling=0.0,
                                           owner_items=red or None) or [
                    {"kind": "owner decision", "detail": "the program ended before its final evaluation; continue it"}]
            ranks = program.topo_ranks(subsystems)["ranks"]
            out = architect.program_report(
                delivered=delivered, contract=contract, results=results, s=s, evaluations=arch.evaluations_of(cid),
                needs=needs, override=arch.override_of(cid, int(contract.get("revision") or 1)) or None,
                done_count=int(i.get("doneCount") or 0), total=int(i.get("total") or 0),
                final_head=str(i.get("finalHead") or ""), ranks=ranks)
            out["ranks"], out["cyclic"], out["rung"] = ranks, [], str(last.get("rung") or "")
        else:
            out = program.program_report(
                subsystems=subsystems, delivered=_truth(i.get("delivered")),
                verdict=i.get("verdict"), done_count=i.get("doneCount") or 0, total=i.get("total") or 0,
                tests_passed=_truth(i.get("testsPassed")) if i.get("testsPassed") is not None else None,
                final_head=str(i.get("finalHead") or ""))
        # Decayable lessons for repo_memory (G5): one per subsystem, refs = its files (so a later run
        # invalidates the lesson if those files are gone, H10).
        lessons = []
        for s in subsystems:
            s = _mapping(s)
            sid = str(s.get("id") or "").strip()
            files = [str(f) for f in _list(s.get("files")) if str(f).strip()]
            if sid:
                lessons.append({"text": f"subsystem '{sid}': {s.get('description') or s.get('title') or ''}".strip(),
                                "refs": files, "confidence": "high", "provenance": "cc_program"})
        out["lessons"] = lessons
        if cid:
            out["children"] = [_mapping(f.get("data")) for f in bb.read_facts(cid, kind=_ROLLUP_KIND)]
            out["spendUsd"] = budget_gate.read_spend(cid)["usd"]
        return ok(task, out, [f"[program_outcome] {out['verdict']} — {out['report']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "program_outcome", e)


# ─────────────────────── teams of teams: the program board (WP5, H3) ─────────────────────
_PROGRAM_KIND = "program"   # {subsystems, ranks, contractPaths, depth}
_CHILD_KIND = "child"       # {subsystem, childRun, depth} — posted by each child team on the program board
_ROLLUP_KIND = "rollup"     # {subsystem, childRun, verdict, rollup, spendUsd}
_BREAKDOWN_KIND = "breakdown"  # {sessionId, subsystems, designDocs, costUsd} — a re-slicing from the design


def _program(cid: str) -> dict:
    facts = bb.read_facts(cid, kind=_PROGRAM_KIND)
    return _mapping(facts[-1].get("data")) if facts else {}


@worker_task(task_definition_name="program_rank_init")
def program_rank_init(task):
    """Seed the PROGRAM board (H3): one item per subsystem (its files = the union of its team's items),
    the frozen program contract, and the build ranks — topological, then split so rank-mates share no
    file (concurrent child teams are file-disjoint by construction). A rejected or cyclic plan is
    reported, never built."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip()
        plan = _mapping(i.get("plan"))
        subsystems = [_mapping(x) for x in _list(plan.get("subtasks")) if _mapping(x).get("id")]
        contract = [str(p) for p in _list(plan.get("contractPaths")) if str(p).strip()]
        if str(plan.get("route") or "") == "replan":
            return ok(task, {"gate": "rejected", "reasons": _list(plan.get("replanReasons")), "rankCount": 0},
                      [f"[program_rank_init] plan rejected: {plan.get('replanReasons')}"])
        ranked = program.rank_plan(subsystems, contract_paths=contract)
        if ranked["cyclic"] or not subsystems:
            return ok(task, {"gate": "cyclic" if ranked["cyclic"] else "rejected", "cyclic": ranked["cyclic"],
                             "reasons": ["no subsystems planned"] if not subsystems else [], "rankCount": 0},
                      [f"[program_rank_init] cyclic={ranked['cyclic']} subsystems={len(subsystems)}"])
        parent = str(i.get("parentCorrelationId") or "").strip()
        if cid and parent and not [f for f in bb.read_facts(parent, kind=_CHILD_KIND)
                                   if _mapping(f.get("data")).get("childRun") == cid]:
            bb.post_fact(parent, cid, _CHILD_KIND, {"subsystem": str(i.get("subsystemId") or ""), "childRun": cid,
                                                    "kind": "program"})
        if cid and not bb.read_facts(cid, kind=_PROGRAM_KIND):
            for sub in subsystems:
                files = sorted({str(f) for it in program.child_items(sub) for f in _list(it.get("files"))})
                team_board.post_item(cid, "program", {**sub, "files": files, "kind": "subsystem"})
            bb.post_fact(cid, "program", _PROGRAM_KIND, {
                "subsystems": subsystems, "ranks": ranked["ranks"], "split": ranked["split"],
                "contractPaths": contract, "depth": int(i.get("depth") or 0)})
            bb.post_fact(cid, "lead", "contract", {"paths": contract, "frozen": True, "revision": 1})
            if _mapping(i.get("planUsage")):
                u = _mapping(i.get("planUsage"))
                budget_gate.record_spend(cid, "planner", usd=float(u.get("costUsd") or 0.0),
                                         tokens=int(u.get("tokenUsed") or 0))
        seeded = {}
        if cid and not architect.contract_of(cid):
            # The checks the Architect wrote itself are on the branch before the first team forks.
            from architect.tasks import seed_authored_contract
            seeded = seed_authored_contract(cid, str(i.get("workspacePath") or "").strip(),
                                            _mapping(plan.get("contractDraft")),
                                            external=_truth(i.get("externalChecks"), True))
        return ok(task, {"gate": "build", "ranks": ranked["ranks"], "rankCount": len(ranked["ranks"]), "seeded": seeded,
                         "maxRankIterations": len(ranked["ranks"]) * max(int(i.get("maxTeamAttempts") or 2), 1),
                         "split": ranked["split"], "subsystems": subsystems, "contractPaths": contract},
                  [f"[program_rank_init] ranks={ranked['ranks']} split={ranked['split']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "program_rank_init", e)


@worker_task(task_definition_name="program_rank_build")
def program_rank_build(task):
    """Fork the next unbuilt rank: one child TEAM per subsystem, built concurrently on the shared
    program branch, each with its own board (H3). A subsystem the planner decomposed into its own
    ``subsystems`` is built by a child ``cc_program`` (recursion, bounded by ``maxDepth``). Each child
    gets an equal share of the program's remaining budget (H5) and the program run as its parent, so
    file ownership is program-scoped and program directives reach its standups."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip()
        prog = _program(cid)
        resliced = _apply_breakdown(cid, prog, _mapping(i.get("breakdown")))
        if resliced:
            prog = _program(cid)
        items = team_board.fold_board(cid)["items"]
        # Built, or handed to the Architect's ladder (REVIEW): never re-forked by the rank loop.
        built = {sid for sid, it in items.items()
                 if it.get("state") in (team_board.DONE, team_board.BLOCKED, team_board.REVIEW)}
        rank = next((r for r in prog.get("ranks") or [] if not set(r) <= built), [])
        depth = int(prog.get("depth") or 0)
        max_depth = max(int(i.get("maxDepth") or 2), 1)
        ceiling = float(i.get("ceilingUsd") or 0) or 0.0
        spent = budget_gate.read_spend(cid)["usd"]
        share = round(max(ceiling - spent, 0.0) / max(len([x for x in rank if x not in built]), 1), 4) if ceiling else 0
        subs = {str(_mapping(s).get("id")): _mapping(s) for s in prog.get("subsystems") or []}
        passthrough = {k: i.get(k) for k in (
            "codeModel", "maxTurns", "maxBudgetUsd", "maxFixAttempts", "maxWidth", "maxWaves", "bestOf",
            "authorAcceptanceTests",
            "modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256", "modelsConfig",
            "modelOverrides") if i.get(k) not in (None, "")}
        dynamic_tasks, dynamic_inputs = [], {}
        # A rank re-run after a team retry forks only its unfinished members.
        for sid in [x for x in rank if x not in built]:
            sub = subs.get(sid, {"id": sid})
            ref = "team_" + re.sub(r"[^A-Za-z0-9_]", "_", sid)
            nested = bool(_list(sub.get("subsystems"))) and depth + 1 < max_depth
            common_in = {**passthrough, "repoPath": i.get("repoPath"), "workspacePath": i.get("workspacePath"),
                         "contractPaths": prog.get("contractPaths") or [], "ceilingUsd": share,
                         "parentCorrelationId": cid, "subsystemId": sid,
                         # A team the program re-runs (it ended INCOMPLETE) starts its agents one tier up.
                         "baseModelTier": int(_mapping(items.get(sid)).get("attempt") or 0)}
            # The team's swarm size: the user's `teamSize` when given, else the Architect's `agents` for
            # this subsystem (sized by complexity), else the program's width cap.
            team_size = int(_num(i.get("teamSize"), 0)) if str(i.get("teamSize") or "").strip() not in ("", "0") else 0
            agents = int(_num(sub.get("agents"), 0))
            if team_size or agents:
                common_in["maxWidth"] = max(team_size or agents, 1)
            if nested:
                ref = "program_" + ref[5:]
                dynamic_tasks.append({"name": "cc_program", "taskReferenceName": ref, "type": "SUB_WORKFLOW",
                                      "optional": True, "subWorkflowParam": {"name": "cc_program"}})
                dynamic_inputs[ref] = {**common_in, "subsystems": _list(sub.get("subsystems")),
                                       "depth": depth + 1, "maxDepth": max_depth,
                                       "description": str(sub.get("description") or sid)}
            else:
                items_ = program.child_items(sub)
                dynamic_tasks.append({"name": "team_build", "taskReferenceName": ref, "type": "SUB_WORKFLOW",
                                      "optional": True, "subWorkflowParam": {"name": "team_build"}})
                # A one-item team authors no separate acceptance test: the scouts' checks are the check the
                # builder did not write, the implementer's own tests and the peer review are the rest. The
                # author cost 1.2-1.7 min before the build and its test only re-wrapped the scenario check
                # (runs 0f829d58, f83293f5); a request that one session builds in minutes was taking twelve.
                dynamic_inputs[ref] = {**common_in, "subtasks": items_, "forceTeam": True,
                                       "authorAcceptanceTests": _truth(common_in.get("authorAcceptanceTests"), True) and len(items_) > 1}
            team_board.set_state(cid, "program", sid, team_board.IN_PROGRESS, assignee=ref)
        return ok(task, {"dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs, "rank": rank,
                         "childBudgetUsd": share, "depth": depth, "resliced": resliced},
                  [f"[program_rank_build] rank={rank} children={len(rank)} budget/child=${share} depth={depth}"
                   + (f" resliced from the design: {resliced}" if resliced else "")])
    except Exception as e:  # noqa: BLE001
        return fail(task, "program_rank_build", e)


def _apply_breakdown(cid: str, prog: dict, agent_out: dict) -> list[str]:
    """Fold the work breakdown the planner derived from the delivered design documents (the
    ``rank_breakdown`` agent, see ``program_apply_rank``) into the program: the re-sliced subsystems
    replace their pre-design items in the program fact and on the program board, the planner's spend
    lands on the program budget, and a resolved ``breakdown`` notice per subsystem records it. Folded
    once per agent session: the loop hands the same output to every later rank build."""
    structured = _mapping(agent_out.get("structured"))
    if not cid or not structured.get("subsystems"):
        return []
    session = str(agent_out.get("sessionId") or "") or hashlib.sha256(
        json.dumps(structured, sort_keys=True, default=str).encode()).hexdigest()
    if any(_mapping(f.get("data")).get("sessionId") == session for f in bb.read_facts(cid, kind=_BREAKDOWN_KIND)):
        return []
    subsystems, changed = program.apply_breakdown(prog.get("subsystems") or [], structured)
    if changed:
        bb.post_fact(cid, "program", _PROGRAM_KIND, {**prog, "subsystems": subsystems})
        for s in subsystems:
            if s.get("id") in changed:
                team_board.post_item(cid, "program", {**s, "kind": "subsystem"})
                mech = (f"re-sliced {s['id']} from the design documents into "
                        f"{', '.join(str(it.get('id')) for it in program.child_items(s))}")
                team_board.post_exception(cid, "program", str(s["id"]), "breakdown", mech, severity="info")
                team_board.resolve_exception(cid, "program", str(s["id"]), "breakdown", mech)
    bb.post_fact(cid, "program", _BREAKDOWN_KIND, {"sessionId": session, "subsystems": changed,
                                                    "costUsd": float(agent_out.get("costUsd") or 0.0)})
    if agent_out.get("costUsd"):
        budget_gate.record_spend(cid, "planner", usd=float(agent_out.get("costUsd") or 0.0),
                                 tokens=int(agent_out.get("tokenUsed") or 0))
    return changed


@worker_task(task_definition_name="program_apply_rank")
def program_apply_rank(task):
    """Fold the rank's child outcomes into the program board: a delivered child marks its subsystem
    ``done``; anything else ``blocked`` with the child's own verdict as the mechanism. Each child's
    rollup is mirrored onto the program board (the program Lead reads rollups, not items — H3/H7), its
    spend is recorded on the program budget (``child:<subsystem>``), and the program continues to the
    next rank only when every subsystem of this one delivered."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or "").strip()
        repo = str(i.get("repoPath") or "").strip()
        rank = [str(x) for x in _list(i.get("rank"))]
        max_attempts = max(int(i.get("maxTeamAttempts") or 2), 1)
        ceiling = float(i.get("ceilingUsd") or 0) or 0.0
        prior = team_board.fold_board(cid)["items"]
        joined = {}
        for ref, wrapper in _mapping(i.get("joined")).items():
            out = _mapping(wrapper).get("output", wrapper) if isinstance(wrapper, dict) else {}
            joined[re.sub(r"__[0-9]+$", "", str(ref))] = _mapping(out)
        children = {str(_mapping(f.get("data")).get("subsystem")): _mapping(f.get("data"))
                    for f in bb.read_facts(cid, kind=_CHILD_KIND)}
        results = []
        for sid in rank:
            safe = re.sub(r"[^A-Za-z0-9_]", "_", sid)
            out = joined.get(f"team_{safe}") or joined.get(f"program_{safe}") or {}
            delivered = out.get("delivered") is True or out.get("programDelivered") is True
            verdict = str(out.get("verdict") or "no child outcome (child team failed or was not run)")
            spend = float(out.get("spendUsd") or 0.0)
            child_run = str(children.get(sid, {}).get("childRun") or out.get("subWorkflowId") or "")
            if not out and _mapping(prior.get(sid)).get("state") == team_board.DONE:
                continue  # delivered on an earlier attempt of this rank; not re-forked this time
            attempt = int(_mapping(prior.get(sid)).get("attempt") or 0)
            report = str(out.get("report") or "")
            if spend:
                budget_gate.record_spend(cid, f"child:{sid}", usd=spend)
            spent = budget_gate.read_spend(cid)["usd"]
            if not delivered and ceiling and spent >= ceiling and attempt + 1 < max_attempts:
                # Nothing left to re-run it with: a re-run team would get a zero share and end at its
                # not-startable gate, five times over, each with a retrospective (maxTeamAttempts 5).
                # The subsystem is handed to the Architect, whose ladder names the need (budget) the run
                # ends on and a continuation resumes from; it is never a terminal block.
                mech = (f"program budget exhausted (${spent:.2f} of ${ceiling:.2f} spent) after child team "
                        f"{child_run[:8]} ended {verdict}; handed to the architect — {report[:300]}")
                team_board.set_state(cid, "program", sid, team_board.REVIEW, reason=mech)
                team_board.post_exception(cid, "program", sid, "budget", mech, severity="high")
                bb.post_fact(cid, "program", architect.NEED_KIND, {"kind": "budget", "subsystem": sid,
                                                                  "detail": f"${spent:.2f} of ${ceiling:.2f} spent; raise the "
                                                                            f"ceiling and continue", "spentUsd": spent,
                                                                  "ceilingUsd": ceiling, "evaluation": -1})
                bb.post_fact(cid, "program", _ROLLUP_KIND, {"subsystem": sid, "childRun": child_run, "verdict": verdict,
                                                           "rollup": _mapping(out.get("rollup")), "spendUsd": spend,
                                                           "delivered": False})
                results.append({"subsystem": sid, "childRun": child_run, "delivered": False, "verdict": verdict,
                                "budgetExhausted": True})
                continue
            if not delivered and attempt + 1 < max_attempts:
                # The program recovers instead of ending: the team is re-run once its whole rank has
                # finished, with its own diagnosis as a directive. pgtype was refused at its final gate
                # for the parser team's tests while that team was still building in the same rank; its
                # repair agent had said so, and the program simply ended at 2/9 (run 037e1cbe). By the
                # time the rank is done such interference is gone, and a real defect gets a second,
                # informed attempt. The work already on the branch stays; the new team builds on it.
                team_board.set_state(cid, "program", sid, team_board.REOPENED, attempt=attempt + 1,
                                     reason=f"child team {child_run[:8]}: {verdict}; re-running it with its "
                                            f"diagnosis (attempt {attempt + 2} of {max_attempts})")
                team_board.post_directive(
                    cid, "program", f"subsystem:{sid}",
                    f"A previous team for '{sid}' ended {verdict}: {report[:1200]} Its work is already on the "
                    f"branch: verify it, fix what that report names (or report alreadySatisfied with evidence "
                    f"when it is not yours to fix), and do not redo what is done.", authority="agent")
                team_board.post_exception(cid, "program", sid, "team_retry",
                                          f"{sid} ended {verdict}; re-run with its diagnosis", severity="info")
                team_board.resolve_exception(cid, "program", sid, "team_retry",
                                             f"program re-runs {sid} (attempt {attempt + 2} of {max_attempts})")
                bb.post_fact(cid, "program", _ROLLUP_KIND, {"subsystem": sid, "childRun": child_run, "verdict": verdict,
                                                           "rollup": _mapping(out.get("rollup")), "spendUsd": spend,
                                                           "delivered": False, "retrying": True})
                results.append({"subsystem": sid, "childRun": child_run, "delivered": False, "verdict": verdict,
                                "retrying": True})
                continue
            # A team still INCOMPLETE after its last attempt is handed to the Architect (REVIEW): its open
            # items go to the ladder's change of approach at the end, never to a terminal block.
            team_board.set_state(cid, "program", sid, team_board.DONE if delivered else team_board.REVIEW,
                                 reason=f"child team {child_run[:8]}: {verdict}"
                                        + ("" if delivered else f" — handed to the architect — {report[:300]}"))
            if not delivered:
                team_board.post_exception(cid, "program", sid, "handed_to_architect",
                                          f"{sid} ended {verdict} after {attempt + 1} attempt(s); the Architect's ladder "
                                          f"takes its open items", severity="info")
                team_board.resolve_exception(cid, "program", sid, "handed_to_architect", "handed to the architect")
            bb.post_fact(cid, "program", _ROLLUP_KIND, {"subsystem": sid, "childRun": child_run,
                                                       "verdict": verdict, "rollup": _mapping(out.get("rollup")),
                                                       "spendUsd": spend, "delivered": delivered})
            results.append({"subsystem": sid, "childRun": child_run, "delivered": delivered, "verdict": verdict})
        prog = _program(cid)
        board = team_board.fold_board(cid)
        done = {s for s, it in board["items"].items() if it.get("state") == team_board.DONE}
        remaining = [r for r in prog.get("ranks") or [] if not set(r) <= done]
        all_ok = all(r["delivered"] for r in results)
        retrying = [r["subsystem"] for r in results if r.get("retrying")]
        # Design documents landed in this rank: the subsystems still to build were sliced before they
        # existed, so the planner re-derives their work items from the documents (``rank_breakdown``)
        # before the next rank is built. Only documents actually on the branch count.
        subsystems = [_mapping(s) for s in prog.get("subsystems") or []]
        docs = [d for d in program.design_docs(subsystems, [r["subsystem"] for r in results if r["delivered"]])
                if repo and os.path.isfile(os.path.join(repo, d))]
        left = program.remaining_subsystems(subsystems, done)
        breakdown = "yes" if all_ok and docs and left else "no"
        breakdown_args = {"description": str(i.get("description") or ""), "docs": docs, "remaining": left,
                          "contract_paths": prog.get("contractPaths") or [], "prior": str(i.get("memoryDigest") or "")}
        prompt = program.breakdown_prompt(**breakdown_args) if breakdown == "yes" else ""
        return ok(task, {"results": results, "continue": bool((all_ok and remaining) or retrying),
                         "rankDelivered": all_ok, "retrying": retrying,
                         "remainingRanks": len(remaining), "head": git.head(repo) if repo and os.path.isdir(repo) else "",
                         "rollup": team_board.board_rollup(board), "breakdown": breakdown, "designDocs": docs,
                         "breakdownPrompt": prompt, "breakdownSchema": program.BREAKDOWN_SCHEMA,
                         "breakdownContext": program.breakdown_context(**breakdown_args) if prompt else {}},
                  [f"[program_apply_rank] {[(r['subsystem'], r['delivered']) for r in results]} retrying={retrying} "
                   f"remaining={len(remaining)} breakdown={breakdown}"
                   + (f" from {docs} for {[s.get('id') for s in left]}" if breakdown == "yes" else "")])
    except Exception as e:  # noqa: BLE001
        return fail(task, "program_apply_rank", e)


# ─────────────────────────────── project_bootstrap (G3) ───────────────────────
@worker_task(task_definition_name="bootstrap_init")
def bootstrap_init(task):
    """Make a greenfield dir git-ready (roadmap Phase 5, G3). An empty/new directory becomes a git repo
    with an empty base commit (preserving any files already there), so the scaffold agent can commit the
    skeleton on top and workspace_prepare/test_cycle have a base from run one."""
    i = task.input_data or {}
    try:
        repo = str(i.get("repoPath") or "").strip()
        out = git.ensure_ready(repo, preserve_worktree=True)
        head = git.head(out["repoPath"])
        has_feature = bool(str(i.get("firstFeature") or "").strip())
        return ok(task, {"repoPath": out["repoPath"], "initialized": out.get("initialized"),
                         "baseCommit": head, "hasFeature": has_feature},
                  [f"[bootstrap_init] repo={out['repoPath']} initialized={out.get('initialized')} "
                   f"base={head[:12]} hasFeature={has_feature}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "bootstrap_init", e)


@worker_task(task_definition_name="bootstrap_outcome")
def bootstrap_outcome(task):
    """Fold the greenfield run into an honest verdict (G3): the skeleton must be green before the team
    hand-off counts; then the first feature's own delivery decides the rest."""
    i = task.input_data or {}
    try:
        fd = i.get("featureDelivered")
        feature_delivered = None if fd in (None, "", "none") else _truth(fd)
        feature_verdict = i.get("featureVerdict")
        # code_parallel's ``verified`` means delivered and merged, and stays true when its targeted tests
        # could not run (run 1117790e reported "verified green" with none run). The project's own test
        # command on the final commit decides.
        if feature_delivered is True and "featureTestsPassed" in i and not _truth(i.get("featureTestsPassed")):
            feature_delivered, feature_verdict = False, i.get("featureTestState") or "tests_not_passed"
        out = bootstrap.bootstrap_outcome(
            skeleton_green=_truth(i.get("skeletonGreen")), feature_delivered=feature_delivered,
            feature_verdict=feature_verdict, final_head=str(i.get("finalHead") or ""))
        return ok(task, out, [f"[bootstrap_outcome] {out['verdict']} — {out['report']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "bootstrap_outcome", e)


# ─────────────────────────────── bug_fix (G2) ─────────────────────────────────
@worker_task(task_definition_name="bug_adopt")
def bug_adopt(task):
    """Fast-forward the workspace branch onto a code_subtask's group branch so the next phase builds on
    it (the repro test must be on the branch before the fix agent runs; the fix must be on the branch
    before the green gate). A pure ff advance — no merge commit — consistent with the H1 invariant."""
    i = task.input_data or {}
    try:
        repo = str(i.get("sharedRepo") or "").strip()
        gid = str(i.get("groupId") or "").strip()
        branch = git.group_branch(repo, gid)
        res = git.git(repo, "merge", "--ff-only", branch, check=False)
        head = git.head(repo)
        adopted = res.code == 0
        try:
            git.worktree_remove(repo, gid)  # worktree_remove prefixes the worktrees directory itself
        except Exception:  # noqa: BLE001
            pass
        return ok(task, {"adopted": adopted, "head": head, "branch": branch},
                  [f"[bug_adopt] group={gid} adopted={adopted} head={head[:12]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "bug_adopt", e)


@worker_task(task_definition_name="bug_repro_gate")
def bug_repro_gate(task):
    """Gate the fix on a genuine reproduction: the repro test must be RED at the parent commit."""
    i = task.input_data or {}
    try:
        out = bug_fix.repro_gate(i.get("verificationState"))
        return ok(task, out, [f"[bug_repro_gate] {out['outcome']}: {out['report']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "bug_repro_gate", e)


@worker_task(task_definition_name="bug_outcome")
def bug_outcome(task):
    """Fold the run into an honest FIXED / NOT_FIXED / CANNOT_REPRODUCE verdict (G2). Also enforces the
    check-integrity check (H13): if the repro test changed between the commit where it was proven red
    and the fix head, the fix tampered with its own check and a green does not count as a fix."""
    i = task.input_data or {}
    try:
        tampered = False
        repo = str(i.get("repoPath") or "").strip()
        repro_commit = str(i.get("reproCommit") or "").strip()
        final_head = str(i.get("finalHead") or "").strip()
        test_paths = [str(p).strip() for p in _list(i.get("reproTestPaths")) if str(p).strip()]
        if repo and repro_commit and final_head and test_paths and _truth(i.get("reproduced")):
            diff = git.git(repo, "diff", "--name-only", f"{repro_commit}..{final_head}", "--", *test_paths,
                           check=False).stdout
            tampered = bool([n for n in diff.split("\n") if n.strip()])
        out = bug_fix.fix_outcome(
            reproduced=_truth(i.get("reproduced")), tests_passed=_truth(i.get("testsPassed")),
            test_state=i.get("testCycleState"), final_head=final_head, repro_test_tampered=tampered)
        out["reproTestTampered"] = tampered
        return ok(task, out, [f"[bug_outcome] {out['verdict']} tampered={tampered} — {out['report']}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "bug_outcome", e)


# ─────────────────────────────── retrospective ────────────────────────────────
def _conductor_workflow(workflow_id: str, *, tasks: bool = False) -> dict:
    """The run's execution from Conductor (for a failure-workflow retrospective, which is handed only the
    failed run's id). {} when unreachable."""
    import urllib.request
    base = (os.environ.get("CONDUCTOR_SERVER_URL") or "http://localhost:8080/api").rstrip("/")
    token = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
    req = urllib.request.Request(f"{base}/workflow/{workflow_id}?includeTasks={'true' if tasks else 'false'}",
                                 headers={**({"X-Authorization": token} if token else {})})
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 - operator-configured Conductor URL
            return json.loads(resp.read() or b"{}")
    except Exception:  # noqa: BLE001
        return {}


def _task_failures(workflow_id: str, *, limit: int = 15, depth: int = 4) -> list[dict]:
    """Why tasks actually failed, across the run's whole sub-workflow tree. The board records outcomes
    ("fork produced no commit"); the mechanism (a git error, a missing toolchain, a sandbox denial) is
    only in the failed task's reason — measured live: a retrospective without this misdiagnosed a
    fork-branch collision as an agent that forgot to commit (run 6d8af3af)."""
    seen, out, stack = set(), [], [(workflow_id, 0)]
    while stack and len(out) < limit:
        wid, level = stack.pop()
        for t in _conductor_workflow(wid, tasks=True).get("tasks") or []:
            reason = str(t.get("reasonForIncompletion") or "").strip()
            if t.get("status") in ("FAILED", "FAILED_WITH_TERMINAL_ERROR", "COMPLETED_WITH_ERRORS", "TIMED_OUT") \
                    and reason:
                core = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<id>", reason)[:400]
                if core not in seen:
                    seen.add(core)
                    out.append({"task": t.get("referenceTaskName"), "type": t.get("taskType"), "reason": core})
            if t.get("taskType") == "SUB_WORKFLOW" and t.get("subWorkflowId") and level < depth:
                stack.append((t["subWorkflowId"], level + 1))
    return out[:limit]


@worker_task(task_definition_name="retro_evidence")
def retro_evidence(task):
    """Gather what actually happened in a finished run (its board and its child teams' boards) and build
    the retrospective prompt. Runs at the end of every run, and as the failure workflow of a failed or
    terminated one. A child team of a program skips: the program's retrospective covers every team."""
    i = task.input_data or {}
    try:
        cid = str(i.get("correlationId") or i.get("workflowId") or "").strip()
        repo = str(i.get("repoPath") or "").strip()
        # The run's workspace (its branch, with everything the teams merged) is what the retrospective
        # agent reads; the source checkout is still on its original branch and shows none of the run's
        # work (run a062184f's agent saw "only the scaffold"). A failed run's failure workflow is not
        # handed the workspace, so it is read back from the run's own workspace task.
        workspace = str(i.get("workspacePath") or "").strip()
        verdict, report, status = str(i.get("verdict") or ""), str(i.get("report") or ""), ""
        parent = str(i.get("parentCorrelationId") or "").strip()
        if cid and (not repo or not verdict or not workspace):
            wf = _conductor_workflow(cid, tasks=not workspace)
            wf_in, wf_out = _mapping(wf.get("input")), _mapping(wf.get("output"))
            repo = repo or str(wf_in.get("repoPath") or "")
            parent = parent or str(wf_in.get("parentCorrelationId") or "")
            status = str(wf.get("status") or "")
            verdict = verdict or str(wf_out.get("verdict") or status)
            report = report or str(wf_out.get("report") or wf.get("reasonForIncompletion") or "")
            for t in _list(wf.get("tasks")):
                if not workspace and _mapping(t).get("referenceTaskName") == "workspace":
                    workspace = str(_mapping(_mapping(t).get("outputData")).get("worktreePath") or "")
        worktree = workspace if workspace and os.path.isdir(workspace) else repo
        if not cid or parent:
            return ok(task, {"skip": "yes", "reason": "child team (covered by the program)" if parent else "no run"},
                      [f"[retro_evidence] skip cid={cid or '-'} parent={parent or '-'}"])
        ev = retrospective.evidence(cid, verdict=verdict, report=report, status=status)
        ev["taskFailures"] = _task_failures(cid)
        if not any(t["items"] for t in ev["teams"]) and not report:
            return ok(task, {"skip": "yes", "reason": "nothing happened on the board"},
                      [f"[retro_evidence] skip cid={cid}: empty board"])
        langs = retrospective.detect_languages(repo)
        key, host_name = retrospective.repo_key(repo), retrospective.host()
        prior = retrospective.digest(retrospective.relevant(repo=key, languages=langs, host_name=host_name))
        return ok(task, {"skip": "no", "correlationId": cid, "repoPath": repo, "worktreePath": worktree,
                         "repoKey": key, "languages": langs, "host": host_name, "verdict": verdict,
                         "prompt": retrospective.prompt(ev, languages=langs, host_name=host_name, prior=prior),
                         "promptContext": retrospective.prompt_context(ev, languages=langs, host_name=host_name,
                                                                       prior=prior),
                         "schema": retrospective.SCHEMA},
                  [f"[retro_evidence] cid={cid} verdict={verdict} teams={len(ev['teams'])} langs={langs}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "retro_evidence", e)


@worker_task(task_definition_name="retro_record")
def retro_record(task):
    """Record the retrospective agent's learnings in the shared store, scoped (repo / environment /
    language / process) so later runs on this repo, host or language start from them."""
    i = task.input_data or {}
    try:
        out = _mapping(i.get("retro"))
        candidates = []
        for src in (out.get("structured"), out.get("result"), out):
            obj = src
            if isinstance(obj, str):
                try:
                    obj = json.loads(obj)
                except (ValueError, TypeError):
                    continue
            if isinstance(obj, dict) and "learnings" in obj:
                candidates.append(obj)
        # The payloads the validator rejected, newest first: the real retrospective when the accepted
        # one is the placeholder the model sent to get past the validator (run a062184f).
        candidates += [a for a in reversed(_list(out.get("structuredAttempts"))) if isinstance(a, dict)]
        body = retrospective.salvage(candidates)
        cid = str(i.get("correlationId") or "")
        written = retrospective.record(_list(body.get("learnings")), repo=str(i.get("repoKey") or ""),
                                       languages=[str(x) for x in _list(i.get("languages"))],
                                       host_name=str(i.get("host") or retrospective.host()), run=cid)
        if cid:
            bb.post_fact(cid, "retrospective", "retrospective",
                         {"summary": str(body.get("summary") or "")[:2000], "learnings": written})
        by_scope = {}
        for x in written:
            by_scope[x["scope"]] = by_scope.get(x["scope"], 0) + 1
        return ok(task, {"summary": str(body.get("summary") or ""), "recorded": len(written), "byScope": by_scope,
                         "learnings": written},
                  [f"[retro_record] cid={cid} recorded={len(written)} {by_scope}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "retro_record", e)
