"""Workers of the ``swarm`` workflow. Each is plumbing: no policy, no classification of failures."""
from __future__ import annotations

import json
import os
import re

from conductor.client.worker.worker_task import worker_task

from common import git
from common.results import fail, ok

CLONES = ".cc-worktrees"
STALL_AFTER_S = 300

# Swarm agents own their git: they commit, rebase and resolve conflicts themselves. They never push,
# never reach the web, never escalate privileges.
SWARM_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite"]
SWARM_DISALLOWED_TOOLS = ["WebSearch", "WebFetch", "Bash(git push*)", "Bash(sudo *)"]

ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"}, "brief": {"type": "string"},
            "kind": {"type": "string"}, "rung": {"type": "integer"},
            "dependsOn": {"type": "array", "items": {"type": "string"}}},
            "required": ["id", "title", "brief", "kind", "rung", "dependsOn"]}},
    },
    "required": ["items"],
}
WORK_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "done": {"type": "boolean"},
                   "files": {"type": "array", "items": {"type": "string"}},
                   "blockers": {"type": "array", "items": {"type": "string"}},
                   "lessons": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "done", "files"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"accepted": {"type": "boolean"}, "notes": {"type": "string"},
                   "fixed": {"type": "array", "items": {"type": "string"}}},
    "required": ["accepted", "notes", "fixed"],
}
ROUND_KIND, REPORT_KIND, CLAIM_KIND = "swarm_round", "swarm_report", "swarm_claim"
# Integrating a round is the same job whatever the repository: the round review's ladder learns across
# all of them (every run of a request in a fresh folder would otherwise start from nothing).
ROUND_REVIEW_REPO = "*any repository*"
EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {"delivered": {"type": "boolean"}, "assessment": {"type": "string"},
                   **ITEMS_SCHEMA["properties"]},
    "required": ["delivered", "assessment", "items"],
}


def _mapping(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _list(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def _ladder_top(resolution, role: str = "code") -> int:
    """The highest rung of a role's ladder in the resolved model profile (0 when it has one tier)."""
    tiers = ((_mapping(resolution).get("roles") or {}).get(role) or {}).get("tiers") or []
    return max(len(tiers) - 1, 0)


def _ladder_text(resolution, role: str = "code") -> str:
    tiers = ((_mapping(resolution).get("roles") or {}).get(role) or {}).get("tiers") or []
    return "; ".join(f"rung {n}: {t.get('model')} at {t.get('effort') or 'default'} effort"
                     for n, t in enumerate(tiers) if isinstance(t, dict)) or "one rung"


def _history_text(repo: str) -> str:
    """What earlier runs on this repository measured, per kind of item and rung: done or not."""
    from common import rung_policy
    from common import blackboard as bb
    key = rung_policy.repo_key(repo)
    tally: dict[tuple[str, int], list[int]] = {}
    for f in bb.read_facts(rung_policy.OUTCOME_RUN, kind=rung_policy.OUTCOME_KIND):
        d = _mapping(f.get("data"))
        if d.get("repo") == key and d.get("role") == "code":
            done, total = tally.setdefault((str(d.get("kind")), int(d.get("rung") or 0)), [0, 0])
            tally[(str(d.get("kind")), int(d.get("rung") or 0))] = [done + bool(d.get("ok")), total + 1]
    if not tally:
        return "No earlier runs on this repository."
    return "\n".join(f"- {kind} items on rung {rung}: {done} of {total} done in one session"
                     for (kind, rung), (done, total) in sorted(tally.items()))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "work"


@worker_task(task_definition_name="swarm_setup")
def swarm_setup(task):
    """A private clone of the repository for the run, so agents can commit inside their sandbox."""
    i = task.input_data or {}
    try:
        repo = os.path.abspath(str(i["repoPath"]))
        run_id = str(i.get("runId") or task.workflow_instance_id)
        git.ensure_ready(repo)
        git.exclude_path(repo, f"/{CLONES}/")
        work = os.path.join(repo, CLONES, f"swarm-{run_id}")
        # The console names the branch after the request (branchName); the run id keeps runs apart.
        named = str(i.get("branchName") or "").strip().strip("/")
        branch = f"{named}-{run_id[:8]}" if named else f"swarm/{_slug(i.get('description') or 'work')}-{run_id[:8]}"
        original_branch = git.git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
        original_head = git.head(repo)
        if not os.path.isdir(os.path.join(work, ".git")):
            git.git(repo, "clone", "--quiet", "--no-local", repo, work)
            git.git(work, "switch", "-c", branch)
        for key in ("user.name", "user.email"):
            value = git.git(repo, "config", key, check=False).stdout.strip()
            git.git(work, "config", key, value or ("conductor-swarm" if key == "user.name" else "swarm@localhost"))
        resolution = i.get("modelResolution") or {}
        return ok(task, {"workdir": work, "branch": branch, "base": git.head(work),
                         "originalBranch": original_branch, "originalHead": original_head,
                         "ladder": _ladder_text(resolution), "maxRung": _ladder_top(resolution),
                         "history": _history_text(repo), "schemas": {
            "items": ITEMS_SCHEMA, "work": WORK_SCHEMA, "evaluate": EVALUATE_SCHEMA},
            "allowedTools": SWARM_ALLOWED_TOOLS, "disallowedTools": SWARM_DISALLOWED_TOOLS},
            [f"[swarm_setup] {work} on {branch}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_setup", e)


def _graph(items: list[dict], board_items: dict) -> tuple[dict, dict]:
    """``(deps, unstartable)``: each item's dependencies inside the round, and the items that can never
    start this round, with why (an unknown id, a cycle, or depending on one of those). A dependency on an
    item from an earlier round counts as met when that item is done on the board."""
    ids = {it["id"] for it in items}
    deps, unstartable = {}, {}
    for it in items:
        mine = []
        for d in (str(x).strip() for x in _list(it.get("dependsOn"))):
            if not d or d == it["id"]:
                continue
            if d in ids:
                mine.append(d)
            elif d not in board_items:
                unstartable[it["id"]] = f"depends on '{d}', which is not an item of this run"
            elif board_items[d].get("state") != "done":
                unstartable[it["id"]] = f"depends on '{d}' from an earlier round, which is not done"
        deps[it["id"]] = mine
    # A cycle never resolves: every item on one waits forever (Kahn's algorithm leaves them behind).
    indegree = {i: len(d) for i, d in deps.items()}
    order = [i for i, n in indegree.items() if n == 0]
    for i in order:
        for j, d in deps.items():
            if i in d:
                indegree[j] -= 1
                if indegree[j] == 0:
                    order.append(j)
    left = {i for i, n in indegree.items() if n > 0}

    def reaches_itself(start: str) -> bool:
        seen, stack = set(), list(deps[start])
        while stack:
            x = stack.pop()
            if x == start:
                return True
            if x in left and x not in seen:
                seen.add(x)
                stack.extend(deps[x])
        return False
    # Only the items ON a cycle are named as such; what merely depends on one is caught below.
    cycle = sorted(i for i in left if reaches_itself(i))
    for i in cycle:
        unstartable.setdefault(i, "is on a dependency cycle (" + ", ".join(cycle) + ")")
    # Whatever depends on an unstartable item cannot start either.
    changed = True
    while changed:
        changed = False
        for i, d in deps.items():
            if i not in unstartable and any(x in unstartable for x in d):
                unstartable[i] = "depends on " + ", ".join(x for x in d if x in unstartable) + ", which cannot start"
                changed = True
    return deps, unstartable


def _item_flow(it: dict, ctx: dict) -> tuple[dict, dict]:
    """The dynamic-fork entry starting one item's sub-workflow, and its input."""
    ref = f"item_{_slug(it['id'])}_r{ctx['round']}"
    return ({"name": "swarm_item", "taskReferenceName": ref, "type": "SUB_WORKFLOW", "optional": True,
             "subWorkflowParam": {"name": "swarm_item"}},
            {**ctx, "item": it})


_CTX_KEYS = ("round", "runId", "workdir", "branch", "description", "repoPath", "ceilingUsd",
             "modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256", "modelsConfig", "modelOverrides")


@worker_task(task_definition_name="swarm_dispatch")
def swarm_dispatch(task):
    """Start a round: record its item graph, mark which items wait on which, and start every item with
    nothing to wait for as its own ``swarm_item`` sub-workflow. Each item, once reviewed, starts the
    items that were waiting on it (``swarm_item_release``): dependency chains of any depth run as nested
    sub-workflows, every item starting the moment its last dependency is accepted."""
    i = task.input_data or {}
    try:
        from common import team_board
        items = [_mapping(x) for x in _list(i.get("items"))]
        items = [{**x, "id": str(x["id"]).strip()} for x in items if str(x.get("id") or "").strip()]
        items = items[: int(i.get("maxWidth") or 10)]
        rnd = int(i.get("round") or 0)
        run = str(i.get("runId") or "")
        board_items = team_board.fold_board(run)["items"] if run else {}
        deps, unstartable = _graph(items, board_items)
        ctx = {k: i.get(k) for k in _CTX_KEYS}
        ctx["round"] = rnd
        if run:
            from common import blackboard as bb
            bb.post_fact(run, "harness", ROUND_KIND, {"round": rnd, "items": items, "deps": deps,
                                                       "unstartable": unstartable})
            for it in items:
                if it["id"] not in board_items:
                    team_board.post_item(run, "architect", {"id": it["id"], "title": it.get("title") or it["id"],
                                                            "description": it.get("brief") or "",
                                                            "kind": it.get("kind") or "feature",
                                                            "dependsOn": deps[it["id"]]})
                if it["id"] in unstartable:
                    team_board.set_state(run, "harness", it["id"], team_board.BLOCKED, attempt=rnd,
                                         reason=f"cannot start in round {rnd}: it {unstartable[it['id']]}")
                elif deps[it["id"]]:
                    team_board.set_state(run, "harness", it["id"], team_board.BACKLOG, attempt=rnd,
                                         reason="waits for " + ", ".join(deps[it["id"]]))
        roots = [it for it in items if not deps[it["id"]] and it["id"] not in unstartable]
        tasks, inputs = [], {}
        for it in roots:
            if run:
                from common import blackboard as bb
                bb.post_fact(run, "harness", CLAIM_KIND, {"round": rnd, "item": it["id"]})
            t, inp = _item_flow(it, ctx)
            tasks.append(t)
            inputs[t["taskReferenceName"]] = inp
        return ok(task, {"dynamicTasks": tasks, "dynamicTasksInput": inputs, "count": len(tasks),
                         "waiting": {k: v for k, v in deps.items() if v and k not in unstartable},
                         "unstartable": unstartable},
                  [f"[swarm_dispatch] round {rnd}: starting {', '.join(t['id'] for t in roots) or 'nothing'}; "
                   f"{sum(1 for k, v in deps.items() if v and k not in unstartable)} wait on dependencies; "
                   f"{len(unstartable)} cannot start"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_dispatch", e)


def _round_facts(run: str, rnd: int, kind: str) -> list[dict]:
    from common import blackboard as bb
    return [_mapping(f.get("data")) for f in bb.read_facts(run, kind=kind)
            if int(_mapping(f.get("data")).get("round") or -1) == int(rnd)]


@worker_task(task_definition_name="swarm_item_prep")
def swarm_item_prep(task):
    """Everything one item's two sessions need: its implementer on the rung the architect chose, its
    reviewer on the review ladder's rung that history says gets this kind of item reviewed, and each
    session's share of what is left of the ceiling."""
    i = task.input_data or {}
    try:
        from common import budget_gate, rung_policy, team_board
        from common import blackboard as bb
        it = _mapping(i.get("item"))
        run, rnd = str(i.get("runId") or ""), int(i.get("round") or 0)
        resolution = i.get("modelResolution")
        rung = min(max(int(it.get("rung") or 0), 0), _ladder_top(resolution, "code"))
        review_rung, review_reason = rung_policy.start_rung(
            rung_policy.repo_key(str(i.get("repoPath") or "")), role="review", kind=str(it.get("kind") or "item"),
            top=_ladder_top(resolution, "review"))
        # Every session gets its share of what is left of the ceiling: the items of the round not yet
        # started, each with an implementer and a reviewer (a round with nothing between its sessions
        # and the ceiling once spent $23.67, run be8fc00e).
        cap = None
        ceiling = float(i.get("ceilingUsd") or 0)
        if ceiling > 0 and run:
            spent = budget_gate.read_spend(run)["usd"]
            graph = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1]
            reported = {r.get("item") for r in _round_facts(run, rnd, REPORT_KIND)}
            left = [x for x in _list(graph.get("items")) if _mapping(x).get("id") not in reported] or [it]
            cap = round(max((ceiling - spent) / (2 * max(len(left), 1)), 0.5), 2)
        if run:
            team_board.set_state(run, "harness", it["id"], team_board.IN_PROGRESS, attempt=rnd,
                                 reason=f"round {rnd}, rung {rung}")
            # The same record the old dispatch wrote, so the board shows the item's rung and why.
            bb.post_fact(run, "harness", "dispatch", {"item": it["id"], "round": rnd, "rung": rung,
                                                       "kind": it.get("kind") or "item",
                                                       "rungReason": "chosen by the architect",
                                                       "reviewRung": review_rung, "reviewRungReason": review_reason,
                                                       "capUsd": cap})
        item_text = json.dumps(it, indent=1)
        return ok(task, {
            "itemId": it["id"], "rung": rung, "reviewRung": review_rung, "reviewRungReason": review_reason,
            "capUsd": cap, "agentAuthor": f"agent:{it['id']}", "reviewAuthor": f"review:{it['id']}",
            "workSchema": WORK_SCHEMA, "reviewSchema": REVIEW_SCHEMA,
            "allowedTools": SWARM_ALLOWED_TOOLS, "disallowedTools": SWARM_DISALLOWED_TOOLS,
            "promptContext": {"request": i.get("description") or "", "branch": i.get("branch") or "",
                              "round": str(rnd), "item": item_text},
            "reviewContext": {"request": i.get("description") or "", "branch": i.get("branch") or "",
                              "round": str(rnd), "item": item_text},
        }, [f"[swarm_item_prep] {it['id']}: implementer rung {rung}, reviewer rung {review_rung} ({review_reason}), "
            f"cap ${cap}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_item_prep", e)


@worker_task(task_definition_name="swarm_item_reviewing")
def swarm_item_reviewing(task):
    """Mark an item "in review" on the board for the time its reviewer is at work."""
    i = task.input_data or {}
    try:
        from common import team_board
        run, item = str(i.get("runId") or ""), str(i.get("itemId") or "")
        summary = str(_mapping(_mapping(i.get("work")).get("structured")).get("summary") or "")
        if run and item:
            team_board.set_state(run, "harness", item, team_board.REVIEW, attempt=int(i.get("round") or 0),
                                 reason=("its reviewer is checking it" + (f"; the agent reported: {summary}" if summary else ""))[:400])
        return ok(task, {"item": item}, [f"[swarm_item_reviewing] {item} is in review"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_item_reviewing", e)


@worker_task(task_definition_name="swarm_item_release")
def swarm_item_release(task):
    """Record one item's outcome and start what was waiting on it. Accepted by its review: every item of
    the round whose dependencies are now all accepted is claimed (once, under the clone's lock, so two
    dependencies finishing together cannot both start it) and started as its own sub-workflow. Not
    accepted: everything waiting on it, at any depth, is held for the round's review."""
    i = task.input_data or {}
    try:
        from common import team_board
        from common import blackboard as bb
        it = _mapping(i.get("item"))
        run, rnd, work = str(i.get("runId") or ""), int(i.get("round") or 0), str(i.get("workdir") or "")
        out, rev = _mapping(i.get("work")), _mapping(i.get("review"))
        s, r = _mapping(out.get("structured")), _mapping(rev.get("structured"))
        # A review that never answered does not hold a chain of work back; the round's review sees it.
        accepted = bool(r.get("accepted")) if r else True
        report = {"round": rnd, "item": it["id"], "kind": it.get("kind") or "item", "rung": int(i.get("rung") or 0),
                  "reviewRung": int(i.get("reviewRung") or 0), "status": out.get("status"),
                  "summary": s.get("summary") or out.get("error") or "no report", "done": s.get("done") is True,
                  "files": _list(s.get("files")), "blockers": _list(s.get("blockers")), "lessons": _list(s.get("lessons")),
                  "accepted": accepted, "reviewNotes": r.get("notes") or ("" if r else "the review did not answer"),
                  "reviewFixed": _list(r.get("fixed"))}
        ctx = {k: i.get(k) for k in _CTX_KEYS}
        tasks, inputs, held = [], {}, []
        if run:
            bb.post_fact(run, "harness", REPORT_KIND, report)
            # Accepted by its review means done for now (the round's review can still hand it back);
            # "review" is only the time its reviewer is at work. Accepted items sat in "In review" until
            # the round ended and the board read as if nobody was reviewing them (run 746bbce4).
            team_board.set_state(run, "review", it["id"], team_board.DONE if accepted else team_board.REOPENED,
                                 reason=(f"accepted by its review: {report['reviewNotes']}" if accepted
                                         else f"not accepted by its review: {report['reviewNotes']}")[:400])
            with git._repo_lock(work):
                graph = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1]
                deps = {k: _list(v) for k, v in _mapping(graph.get("deps")).items()}
                unstartable = _mapping(graph.get("unstartable"))
                claimed = {c.get("item") for c in _round_facts(run, rnd, CLAIM_KIND)}
                accepted_ids = {x.get("item") for x in _round_facts(run, rnd, REPORT_KIND) if x.get("accepted")}
                items = {(_mapping(x)).get("id"): _mapping(x) for x in _list(graph.get("items"))}
                if accepted:
                    for iid, d in deps.items():
                        if iid in claimed or iid in unstartable or not d or not all(x in accepted_ids for x in d):
                            continue
                        bb.post_fact(run, "harness", CLAIM_KIND, {"round": rnd, "item": iid})
                        t, inp = _item_flow(items[iid], ctx)
                        tasks.append(t)
                        inputs[t["taskReferenceName"]] = inp
                else:
                    blocked, frontier = set(), {it["id"]}
                    while frontier:
                        nxt = {iid for iid, d in deps.items() if set(d) & frontier and iid not in blocked and iid not in claimed}
                        blocked |= nxt
                        frontier = nxt
                    for iid in sorted(blocked):
                        team_board.set_state(run, "harness", iid, team_board.BLOCKED, attempt=rnd,
                                             reason=f"held: it waits for {it['id']}, which its review did not accept")
                        held.append(iid)
        return ok(task, {"accepted": accepted, "dynamicTasks": tasks, "dynamicTasksInput": inputs,
                         "started": [t["taskReferenceName"] for t in tasks], "held": held},
                  [f"[swarm_item_release] {it['id']}: {'accepted' if accepted else 'not accepted'}; "
                   f"starting {len(tasks)} dependent(s); holding {len(held)}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_item_release", e)


@worker_task(task_definition_name="swarm_measure")
def swarm_measure(task):
    """Commit the shared tree, then run each of the architect's checks on it once: exit code and output,
    nothing more. Given ``previous`` (an earlier measurement) of the very same tree, return it instead
    of running anything: the same code and the same commands prove the same thing (run 2bc5d0c6 ran the
    whole-app check 18 times in one round, the architect re-running what had just been run)."""
    i = task.input_data or {}
    try:
        from common import architect, check_execution
        from common.coding_agent import _tool_cache_env
        work = str(i["workdir"])
        # ONE tree, so there is nothing to merge: whoever wrote it, the round's work is one commit.
        # Agents never commit (Codex's sandbox protects .git; run 18e18490 had every commit refused).
        git.git(work, "add", "-A")
        if git.git(work, "diff", "--cached", "--quiet", check=False).code != 0:
            git.git(work, "commit", "--quiet", "--no-verify", "-m", str(i.get("label") or "swarm round").strip())
        commit = git.head(work)
        tree = git.git(work, "rev-parse", "HEAD^{tree}").stdout.strip()
        # What this commit changed, file by file: the architect starts its review here instead of
        # reading every patch of the round (3.7M tokens for a six-item round, run 032b778c).
        changes = git.git(work, "show", "--stat", "--format=", "HEAD", check=False).stdout.strip()[-6000:]
        previous = _mapping(i.get("previous"))
        if previous.get("tree") == tree and previous.get("results") is not None:
            return ok(task, {**{k: previous.get(k) for k in ("results", "allGreen", "summary")},
                             "tree": tree, "commit": commit, "reused": True, "changes": changes},
                      [f"[swarm_measure] tree {tree[:12]} unchanged since it was measured: reusing that result"])
        try:
            with open(os.path.join(work, ".cc", "checks.json"), encoding="utf-8") as fh:
                checks = [_mapping(c) for c in _list(json.load(fh))]
        except (OSError, ValueError) as e:
            return ok(task, {"results": [], "allGreen": False, "summary": f"no readable .cc/checks.json: {e}",
                             "tree": tree, "commit": commit, "reused": False, "changes": changes})
        env = check_execution.inherited_environment(_tool_cache_env(work))
        results = []
        # What the console shows while the checks run: which one, for how long, and how the finished
        # ones went (a run page that said only "measure" for twelve minutes told nobody anything).
        from common.progress import ProgressReporter
        import threading
        import time as _time
        current = {"n": 0, "check": {}, "since": _time.monotonic()}
        reporter = ProgressReporter(task, heartbeat_s=10.0).start()
        stop = threading.Event()

        def _progress() -> None:
            while not stop.wait(5.0):
                c = current["check"]
                done = "; ".join(f"{r['id']}: {'stalled' if r.get('stalled') else 'exit ' + str(r['exitCode'])}"
                                 for r in results)
                reporter.update({"progress": f"check {current['n']} of {len(checks)}: {c.get('id')} "
                                             f"(`{c.get('command')}`), running for "
                                             f"{_time.monotonic() - current['since']:.0f} s"
                                             + (f"\nfinished: {done}" if done else "")})
        threading.Thread(target=_progress, daemon=True).start()
        try:
            _run_checks(checks, results, current, work, env, check_execution, architect)
        finally:
            stop.set()
            reporter.stop()
        return _measured(task, results, tree=tree, commit=commit, changes=changes)
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_measure", e)


def _run_checks(checks, results, current, work, env, check_execution, architect) -> None:
    import time as _time
    for n, c in enumerate(checks, 1):
        current.update(n=n, check=c, since=_time.monotonic())
        try:
            # The progress rule: a check that blocks (a race, a lost reply) is stopped and reported
            # stalled after five quiet minutes instead of holding the run forever; one that works,
            # however slowly, always shows output or CPU (run 169589fd waited 736 s on a dropped reply).
            r = check_execution.execute(architect.check_argv(str(c.get("command") or "")), cwd=work, env=env,
                                        stall_after_s=STALL_AFTER_S)
            results.append({"id": c.get("id"), "command": c.get("command"), "exitCode": r.exit_code,
                            "seconds": r.duration_seconds, "stalled": r.stalled,
                            "output": ((r.stdout or "") + (r.stderr or ""))[-3000:]})
        except (ValueError, OSError) as e:
            results.append({"id": c.get("id"), "command": c.get("command"), "exitCode": None, "output": str(e)})


def _measured(task, results, *, tree: str, commit: str, changes: str = ""):
    green = bool(results) and all(r["exitCode"] == 0 for r in results)
    summary = "\n\n".join(f"- {r['id']} (`{r['command']}`) "
                            + (f"STALLED after {r['seconds']:.0f} s: no output and no CPU, stopped"
                               if r.get("stalled") else f"exit {r['exitCode']} in {r.get('seconds') or 0:.0f} s")
                            + f"\n{r['output']}" for r in results)
    return ok(task, {"results": results, "allGreen": green, "summary": summary or "no checks listed",
                     "tree": tree, "commit": commit, "reused": False, "changes": changes},
              [f"[swarm_measure] {sum(r['exitCode'] == 0 for r in results)}/{len(results)} green"])


@worker_task(task_definition_name="swarm_round")
def swarm_round(task):
    """Fold one round. Without ``evaluation`` (after the agents): their reports, each item's state on the
    board, and what each rung did for each kind of item (measured, for the architect to read next time).
    With it (after the architect): commit the tree, the verdict, the spend, whether to go on."""
    i = task.input_data or {}
    try:
        from common import budget_gate, rung_policy, team_board
        run = str(i.get("runId") or "")
        rnd = int(i.get("round") or 0)
        repo_key = rung_policy.repo_key(str(i.get("repoPath") or ""))
        evaluation = _mapping(i.get("evaluation"))
        reports = _round_facts(run, rnd, REPORT_KIND) if run else []
        if run and not evaluation:
            # After the agents: what each item's session did, measured per kind and rung for the next
            # run's architect (done in one session AND accepted by its review), and what never started.
            graph = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1]
            reported = {r.get("item") for r in reports}
            board_items = team_board.fold_board(run)["items"]
            for r in reports:
                rung_policy.record(repo_key, role="code", kind=str(r.get("kind") or "item"), rung=int(r.get("rung") or 0),
                                   ok=bool(r.get("done") and r.get("accepted")), item=r.get("item"), run=run)
            for x in _list(graph.get("items")):
                iid = _mapping(x).get("id")
                if iid not in reported:
                    reports.append({"item": iid, "notStarted": True,
                                    "reason": (board_items.get(iid) or {}).get("reason") or "not started"})
        # The round review's rung: the cheapest that has delivered rounds before (history across all
        # repositories), one higher for each round of this run that has not delivered. Starting it on
        # the cheapest rung every time cost run 649fa3c8 two extra rounds: terra handed back
        # "verify" items twice before sol fixed the integration itself.
        round_rung, round_reason = 0, ""
        if not evaluation:
            top = _ladder_top(i.get("modelResolution"), "review")
            start, round_reason = rung_policy.start_rung(ROUND_REVIEW_REPO, role="round_review", kind="round", top=top)
            round_rung = min(start + max(rnd - 1, 0), top)
        spend = budget_gate.read_spend(run)["usd"] if run else 0.0
        verdict = _mapping(evaluation.get("structured"))
        delivered = bool(verdict.get("delivered")) and bool(i.get("allGreen"))
        next_items = _list(verdict.get("items"))
        if run and evaluation:
            if i.get("roundReviewRung") not in (None, ""):
                rung_policy.record(ROUND_REVIEW_REPO, role="round_review", kind="round",
                                   rung=int(i["roundReviewRung"]), ok=delivered, item=f"round {rnd}", run=run)
            board = team_board.fold_board(run)["items"]
            again = {str(_mapping(x).get("id")) for x in next_items}
            # How each item review did, for the review ladder: one that accepted an item the architect
            # then handed back missed something, so that kind of item gets a stronger reviewer next time.
            for r in reports:
                if r.get("accepted"):
                    rung_policy.record(repo_key, role="review", kind=str(r.get("kind") or "item"),
                                       rung=int(r.get("reviewRung") or 0), ok=r.get("item") not in again,
                                       item=r.get("item"), run=run)
            # The architect's judgment of the round settles every item it did not hand back: one an agent
            # left unfinished ("reopened") that the architect then completed is done too, or it stayed
            # "reopened" on a delivered run's board (run dc50a7bb, launcher-integration). A delivered run
            # also settles items held or never started: the architect delivered without them.
            settles = {team_board.REVIEW, team_board.REOPENED} | (
                {team_board.BLOCKED, team_board.BACKLOG} if delivered else set())
            for iid, it in board.items():
                if it.get("state") in settles and (delivered or iid not in again):
                    team_board.set_state(run, "architect", iid, team_board.DONE,
                                         reason={team_board.REVIEW: "the architect accepted it",
                                                 team_board.REOPENED: "the architect completed it in review"}.get(
                                             it.get("state"), "the run was delivered without it"))
        over = float(i.get("ceilingUsd") or 0) > 0 and spend >= float(i["ceilingUsd"])
        return ok(task, {"delivered": delivered, "stop": delivered or over, "overBudget": over,
                         "items": next_items, "assessment": verdict.get("assessment") or "",
                         "reports": reports, "spentUsd": round(spend, 4),
                         "roundReviewRung": round_rung, "roundReviewReason": round_reason},
                  [f"[swarm_round] delivered={delivered} spent=${spend:.2f} next={len(next_items)}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_round", e)


@worker_task(task_definition_name="swarm_finish")
def swarm_finish(task):
    """Bring the swarm's branch back into the user's repository."""
    i = task.input_data or {}
    try:
        repo, work, branch = str(i["repoPath"]), str(i["workdir"]), str(i["branch"])
        git.git(repo, "fetch", "--quiet", work, f"+{branch}:{branch}")
        head = git.git(repo, "rev-parse", branch).stdout.strip()
        delivered = str(i.get("delivered")).strip().lower() == "true"
        # The same handoff record every branch-owning workflow reports: the console shows the branch,
        # offers merge and pull request, and continues from it.
        handoff = {"sourceRepoPath": repo, "repoPath": repo, "branch": branch, "commit": head,
                   "originalBranch": str(i.get("originalBranch") or ""), "originalHead": str(i.get("originalHead") or "")}
        return ok(task, {"branch": branch, "head": head, "delivered": delivered,
                         "verdict": "DELIVERED" if delivered else "INCOMPLETE", "sourceHandoff": handoff},
                  [f"[swarm_finish] {branch} is in {repo} ({'delivered' if delivered else 'incomplete'})"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_finish", e)
