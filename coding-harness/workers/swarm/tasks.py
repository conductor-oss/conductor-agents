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
            "kind": {"type": "string"}, "rung": {"type": "integer"}},
            "required": ["id", "title", "brief", "kind", "rung"]}},
    },
    "required": ["items"],
}
WORK_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "done": {"type": "boolean"},
                   "blockers": {"type": "array", "items": {"type": "string"}},
                   "lessons": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "done"],
}
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


@worker_task(task_definition_name="swarm_dispatch")
def swarm_dispatch(task):
    """One agent session per item, all in the shared clone."""
    i = task.input_data or {}
    try:
        items = [_mapping(x) for x in _list(i.get("items"))]
        items = [x for x in items if str(x.get("id") or "").strip()][: int(i.get("maxWidth") or 10)]
        rnd = int(i.get("round") or 0)
        run = str(i.get("runId") or "")
        top = _ladder_top(i.get("modelResolution"))
        # Every session gets its share of what is left of the ceiling, so a round can never spend past
        # it (one round of be8fc00e spent $23.67 with nothing between the sessions and the ceiling).
        from common import budget_gate, team_board
        spent = budget_gate.read_spend(run)["usd"] if run else 0.0
        ceiling = float(i.get("ceilingUsd") or 0)
        cap = round(max((ceiling - spent) / max(len(items), 1), 0.5), 2) if ceiling > 0 else None
        known = set(team_board.fold_board(run)["items"]) if run else set()
        tasks, inputs = [], {}
        for it in items:
            rung = min(max(int(it.get("rung") or 0), 0), top)
            if run:
                if it["id"] not in known:
                    team_board.post_item(run, "architect", {"id": it["id"], "title": it.get("title") or it["id"],
                                                            "description": it.get("brief") or "",
                                                            "kind": it.get("kind") or "feature"})
                team_board.set_state(run, "architect", it["id"], team_board.IN_PROGRESS, attempt=rnd,
                                     reason=f"round {rnd}, rung {rung}")
            ref = f"work_{_slug(it['id'])}_r{rnd}"
            tasks.append({"name": "coding_agent", "taskReferenceName": ref, "type": "SIMPLE", "optional": True})
            inputs[ref] = {
                "worktreePath": i["workdir"], "templateKey": "swarm_work", "modelRole": "code",
                "promptContext": {"request": i.get("description") or "", "branch": i.get("branch") or "",
                                  "round": str(rnd), "item": json.dumps(it, indent=1)},
                "schema": WORK_SCHEMA, "sharedTree": True, "allowedWriteRoots": [],
                "allowedTools": SWARM_ALLOWED_TOOLS, "disallowedTools": SWARM_DISALLOWED_TOOLS,
                "feedbackCorrelationId": i.get("runId") or "", "feedbackRunId": i.get("runId") or "",
                "feedbackItem": it["id"],
                "modelProfile": i.get("modelProfile") or "", "modelPolicy": i.get("modelPolicy") or {},
                "modelPolicySource": i.get("modelPolicySource") or "",
                "modelPolicySha256": i.get("modelPolicySha256") or "",
                "modelsConfig": i.get("modelsConfig") or "", "modelOverrides": i.get("modelOverrides") or {},
                "modelResolution": i.get("modelResolution") or {},
                "promptTemplate": "", "promptTemplateSource": "",
                "modelTier": rung, **({"maxBudgetUsd": cap} if cap is not None else {}),
                "spendRunId": run, "spendAuthor": f"agent:{it['id']}",
            }
        return ok(task, {"dynamicTasks": tasks, "dynamicTasksInput": inputs, "count": len(tasks),
                         "sessionCapUsd": cap, "spentUsd": round(spent, 4)},
                  [f"[swarm_dispatch] round {rnd}: {', '.join(x['id'] for x in items) or 'nothing'}"
                   f" (cap ${cap} each, ${spent:.2f} spent)"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_dispatch", e)


@worker_task(task_definition_name="swarm_measure")
def swarm_measure(task):
    """Run each of the architect's checks at the current tree: exit code and output, nothing more."""
    i = task.input_data or {}
    try:
        from common import architect, check_execution
        from common.coding_agent import _tool_cache_env
        work = str(i["workdir"])
        try:
            with open(os.path.join(work, ".cc", "checks.json"), encoding="utf-8") as fh:
                checks = [_mapping(c) for c in _list(json.load(fh))]
        except (OSError, ValueError) as e:
            return ok(task, {"results": [], "allGreen": False, "summary": f"no readable .cc/checks.json: {e}"})
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
        return _measured(task, results)
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


def _measured(task, results):
    green = bool(results) and all(r["exitCode"] == 0 for r in results)
    summary = "\n\n".join(f"- {r['id']} (`{r['command']}`) "
                            + (f"STALLED after {r['seconds']:.0f} s: no output and no CPU, stopped"
                               if r.get("stalled") else f"exit {r['exitCode']} in {r.get('seconds') or 0:.0f} s")
                            + f"\n{r['output']}" for r in results)
    return ok(task, {"results": results, "allGreen": green, "summary": summary or "no checks listed"},
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
        joined = _mapping(i.get("joined"))
        dispatched = [_mapping(x) for x in _list(i.get("items")) if str(_mapping(x).get("id") or "").strip()]
        rnd = str(i.get("round") or "")
        reports = []
        for it in dispatched:
            ref = f"work_{_slug(it['id'])}_r{rnd}"
            out = next((_mapping(v) for k, v in joined.items() if k == ref or k.startswith(ref + "__")), {})
            s = _mapping(out.get("structured"))
            done = s.get("done") is True
            reports.append({"item": it["id"], "rung": it.get("rung"), "status": out.get("status"),
                            **({k: s.get(k) for k in ("summary", "done", "blockers", "lessons")} if s
                               else {"error": out.get("error") or "no report"})})
            if run and out:
                team_board.set_state(run, "agent", it["id"], team_board.REVIEW if done else team_board.REOPENED,
                                     reason=str(s.get("summary") or out.get("error") or "")[:300])
                rung_policy.record(rung_policy.repo_key(str(i.get("repoPath") or "")), role="code",
                                   kind=str(it.get("kind") or "item"), rung=int(it.get("rung") or 0),
                                   ok=done, item=it["id"], run=run)
        # ONE tree, so there is nothing to merge: the round's work, whoever wrote it, is one commit.
        # Agents never commit (Codex's sandbox protects .git; run 18e18490 had every commit refused).
        work = str(i.get("workdir") or "")
        committed = ""
        if work:
            git.git(work, "add", "-A")
            if git.git(work, "diff", "--cached", "--quiet", check=False).code != 0:
                git.git(work, "commit", "--quiet", "--no-verify", "-m", f"swarm round {rnd}".strip())
                committed = git.head(work)
        spend = budget_gate.read_spend(run)["usd"] if run else 0.0
        evaluation = _mapping(i.get("evaluation"))
        verdict = _mapping(evaluation.get("structured"))
        delivered = bool(verdict.get("delivered")) and bool(i.get("allGreen"))
        next_items = _list(verdict.get("items"))
        if run and evaluation:
            board = team_board.fold_board(run)["items"]
            again = {str(_mapping(x).get("id")) for x in next_items}
            for iid, it in board.items():
                if it.get("state") == team_board.REVIEW and (delivered or iid not in again):
                    team_board.set_state(run, "architect", iid, team_board.DONE, reason="the architect accepted it")
        over = float(i.get("ceilingUsd") or 0) > 0 and spend >= float(i["ceilingUsd"])
        return ok(task, {"delivered": delivered, "stop": delivered or over, "overBudget": over, "commit": committed,
                         "items": next_items, "assessment": verdict.get("assessment") or "",
                         "reports": reports, "spentUsd": round(spend, 4)},
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
