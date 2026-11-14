"""The Architect's worker tasks (``cc_program``): thin wrappers over ``common/architect.py``.

Definition phase (after planning, before rank one): ``check_scout_build`` forks one scout per external
check kind (or copies a continuation's contract forward), ``contract_fold`` red-proves the scouts'
candidates against the untouched head and records revision 1 on the program board and the branch,
``contract_review_fold`` applies the reviewer's approve-or-reject (bounded; the bound applies the
reviewer's weights and proceeds). The build starts only once the contract is on the branch: every
team reads it from its checkout (``architect.read_contract_brief``) into its test author's,
implementers' and reviewers' prompts, so one definition of done is what everyone builds against. Every integrated rank and the final head: ``contract_run`` runs every
check and scores the head, the Architect session reads the branch, ``architect_fold`` files its items,
directives and amendments, folds human steer, records the evaluation and decides the ladder rung;
``opinion_fold`` records the second opinion (override, amendment or send-back).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time

from conductor.client.worker.worker_task import worker_task

from common import architect, budget_gate, check_execution, git, program, prompt_files, team_board, templating
from common import blackboard as bb
from common.results import fail, ok
from common.architect import (_num, amendments_of, board_view, contract_of, evaluations_of, needs_facts,  # noqa: F401
                              opinion_of, override_of, pending_amendments, results_of, seen_steer, steer_facts,
                              _facts, _post_contract, _program_fact)
from common.shapes import listed as _list, mapping as _mapping, truth as _truth

CONTINUE_PREFIX = "continue:"
_SCOUT_TOOLS = ["Read", "Grep", "Glob", "Bash", "Write", "Edit"]
# The Architect reads and runs; it never edits. Bash is for running checks and tests; any edit a
# session makes through it is reverted by the fold and recorded.
_ARCHITECT_TOOLS = ["Read", "Grep", "Glob", "Bash"]
_MODEL_KEYS = ("modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256", "modelsConfig",
               "modelOverrides", "modelResolution")
_TEAM_KEYS = ("codeModel", "maxTurns", "maxBudgetUsd", "maxFixAttempts", "maxWidth", "maxWaves", "bestOf",
              "authorAcceptanceTests", "maxRescues",
              "modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256", "modelsConfig",
              "modelOverrides")
_TAIL = 1500


def _cid(i: dict) -> str:
    return str(i.get("correlationId") or "").strip()


def _model(i: dict) -> dict:
    out = {k: i.get(k) for k in _MODEL_KEYS if i.get(k) not in (None, "")}
    if i.get("codeModel") not in (None, ""):
        out["model"] = i.get("codeModel")
    return out


def _agent_out(wrapper) -> dict:
    """A coding_agent's output, whether handed directly or inside a JOIN entry."""
    w = _mapping(wrapper)
    return _mapping(w.get("output", w)) if "output" in w and isinstance(w.get("output"), dict) else w


def _structured(out: dict) -> dict:
    o = _mapping(out)
    s = o.get("structured")
    if isinstance(s, str):
        try:
            s = json.loads(s)
        except (ValueError, TypeError):
            s = {}
    return _mapping(s)


def _spend(cid: str, author: str, out: dict) -> None:
    out = _mapping(out)
    if cid and _num(out.get("costUsd")):
        budget_gate.record_spend(cid, author, usd=float(_num(out.get("costUsd"))),
                                 tokens=int(_num(out.get("tokenUsed"))))


def _revert_edits(wt: str, out: dict) -> list[str]:
    """The Architect never implements: every file a session changed is restored."""
    changed = [str(p) for p in _list(_mapping(out).get("filesChanged")) if str(p).strip()]
    if wt and os.path.isdir(wt):
        for p in changed:
            git.restore_path(wt, p)
    return changed


# ─────────────────────────── running checks ──────────────────────────────────
def _env(wt: str) -> dict:
    from common.coding_agent import _tool_cache_env
    extra = _tool_cache_env(wt)
    cache_bin = os.path.join(wt, architect.CACHE_ROOT, "bin")
    env = check_execution.inherited_environment(extra)
    env["PATH"] = cache_bin + os.pathsep + env.get("PATH", "")
    return env


def _tracked_changes(wt: str) -> list[str]:
    return sorted(p for p, code in git.status_changes(wt).items()
                  if code != "A" and not p.startswith(architect.CACHE_ROOT))


def run_check(wt: str, check: dict) -> dict:
    """Run one check in the worktree through the shared runtime: its setup lines first (idempotent;
    a failing setup blocks the check with the mechanism), then its command. Every run is recorded
    with exit, output tail and wall time; a command that edits the repository tree is reverted and
    blocked. No deadline: a check runs until it completes."""
    check = _mapping(check)
    env = _env(wt)
    started = time.monotonic()
    before = _tracked_changes(wt)
    # The checkout's own dependencies first (a fresh snapshot has no node_modules): the scenario's
    # `npm run dev` died with "vite: command not found" at the final head and the ladder set out to
    # repair code that was fine (run b00146a9). The same installs the verification clone runs.
    from pathlib import Path
    from common import verification
    for prepare in verification._prepare_commands(Path(wt)):
        res = check_execution.execute([str(a) for a in prepare["argv"]], cwd=wt, env=env)
        if res.exit_code != 0:
            return {"state": "blocked", "exitCode": res.exit_code, "tail": (res.stderr or res.stdout)[-_TAIL:],
                    "seconds": round(time.monotonic() - started, 3),
                    "mechanism": f"dependency install {' '.join(prepare['argv'])!r} exited {res.exit_code}: "
                                 f"{(res.stderr or res.stdout).strip()[-300:]}"}
    for line in _list(check.get("setup")):
        try:
            argv = architect.check_argv(str(line))
            res = check_execution.execute(argv, cwd=wt, env=env)
        except (ValueError, OSError) as e:  # noqa: PERF203
            return {"state": "blocked", "exitCode": None, "tail": "", "seconds": round(time.monotonic() - started, 3),
                    "mechanism": f"setup {line!r} could not run: {e}"}
        if res.exit_code != 0:
            return {"state": "blocked", "exitCode": res.exit_code, "tail": (res.stderr or res.stdout)[-_TAIL:],
                    "seconds": round(time.monotonic() - started, 3),
                    "mechanism": f"setup {line!r} exited {res.exit_code}: {(res.stderr or res.stdout).strip()[-300:]}"}
    try:
        argv = architect.check_argv(str(check.get("command") or ""))
        res = check_execution.execute(argv, cwd=wt, env=env)
    except (ValueError, OSError) as e:
        return {"state": "blocked", "exitCode": None, "tail": "", "seconds": round(time.monotonic() - started, 3),
                "mechanism": f"command could not run: {e}"}
    edited = [p for p in _tracked_changes(wt) if p not in before]
    if edited:
        for p in edited:
            git.restore_path(wt, p)
        return {"state": "blocked", "exitCode": res.exit_code, "tail": (res.stdout + res.stderr)[-_TAIL:],
                "seconds": round(time.monotonic() - started, 3),
                "mechanism": f"the check command edited the repository tree ({', '.join(edited[:6])}); reverted"}
    tail = (res.stdout + ("\n" + res.stderr if res.stderr else ""))[-_TAIL:]
    return {"state": "green" if res.exit_code == 0 else "red", "exitCode": res.exit_code, "tail": tail,
            "seconds": round(time.monotonic() - started, 3),
            "mechanism": "" if res.exit_code == 0 else f"exited {res.exit_code}"}


def _run_argv(wt: str, argv: list[str]) -> dict:
    env = _env(wt)
    started = time.monotonic()
    try:
        res = check_execution.execute([str(a) for a in argv], cwd=wt, env=env)
    except (ValueError, OSError) as e:
        return {"state": "blocked", "exitCode": None, "tail": "", "mechanism": f"could not run: {e}",
                "seconds": round(time.monotonic() - started, 3)}
    return {"state": "green" if res.exit_code == 0 else "red", "exitCode": res.exit_code,
            "tail": (res.stdout + ("\n" + res.stderr if res.stderr else ""))[-_TAIL:],
            "mechanism": "" if res.exit_code == 0 else f"{' '.join(argv)[:120]} exited {res.exit_code}",
            "seconds": round(time.monotonic() - started, 3)}


def _same_code(wt: str, prior_head: str, head: str) -> bool:
    """Two heads hold the same code when nothing but the contract directory differs between them."""
    if not prior_head or not head:
        return False
    if prior_head == head:
        return True
    if not wt or not os.path.isdir(wt):
        return False
    res = git.git(wt, "diff", "--name-only", prior_head, head, check=False)
    if res.code != 0:
        return False
    return all(p.startswith(architect.CONTRACT_DIR + "/") for p in res.stdout.split() if p.strip())


def _acceptance_commands(cid: str) -> list[list[str]]:
    """The frozen acceptance tests of every child team's DONE items (child teams post themselves on the
    program board), each as its argv; the acceptance check runs them all."""
    seen, out = set(), []
    for f in bb.read_facts(cid, kind="child"):
        child = str(_mapping(f.get("data")).get("childRun") or "")
        if not child or child in seen:
            continue
        seen.add(child)
        for it in team_board.fold_board(child)["items"].values():
            if it.get("state") != team_board.DONE:
                continue
            for c in _list(it.get("testCommands")):
                argv = [str(a) for a in _list(_mapping(c).get("argv"))]
                if argv and argv not in out:
                    out.append(argv)
    return out


def _every_item_done(cid: str) -> bool:
    """Every subsystem of the program is DONE on its board (nothing is still building)."""
    items = team_board.fold_board(cid)["items"] if cid else {}
    subsystems = [it for it in items.values() if it.get("kind", "subsystem") == "subsystem"]
    return bool(subsystems) and all(it.get("state") == team_board.DONE for it in subsystems)


def run_contract(cid: str, wt: str, contract: dict, *, tests_passed, tests_report: str = "") -> dict:
    """Every check at the current head: the acceptance tests of every done item (blocked while none
    exists), the integration suite (what the final ``test_cycle`` said; blocked before it runs), and each
    external check's own run. Every check runs even after one fails; one ``check_result`` fact each."""
    head = git.head(wt) if wt and os.path.isdir(wt) else ""
    results = {}
    for o in _list(contract.get("checks")):
        o = _mapping(o)
        oid, kind = str(o.get("id")), str(o.get("kind"))
        if o.get("state") == "satisfied":
            r = {"state": "satisfied", "exitCode": 0, "tail": "", "seconds": 0, "mechanism": o.get("mechanism") or ""}
        elif kind == "acceptance":
            cmds = _acceptance_commands(cid)
            if not cmds and _every_item_done(cid):
                # Every team is done and none authored a frozen test (a one-item team authors none: the
                # scouts' checks are the check its builder did not write, and its own tests ran in its
                # fork and in the final cycles): the claim holds vacuously. Left "blocked" it could never
                # pass and the ladder would loop on it (run b00146a9).
                r = {"state": "green", "exitCode": 0, "tail": "", "seconds": 0,
                     "mechanism": "no frozen acceptance test on the branch: the teams' own tests and the external checks are the check"}
            elif not cmds:
                r = {"state": "blocked", "exitCode": None, "tail": "", "seconds": 0,
                     "mechanism": "no frozen acceptance test on the branch yet"}
            else:
                runs = [_run_argv(wt, c) for c in cmds] if wt and os.path.isdir(wt) else []
                red = [x for x in runs if x["state"] != "green"]
                r = {"state": "green" if runs and not red else ("red" if red else "blocked"),
                     "exitCode": 0 if runs and not red else (red[0]["exitCode"] if red else None),
                     "tail": "\n".join(x["tail"][-400:] for x in red)[-_TAIL:] if red else "",
                     "seconds": round(sum(float(x.get("seconds") or 0) for x in runs), 3),
                     "mechanism": "; ".join(x["mechanism"] for x in red)[:600] if red else "",
                     "commands": len(cmds)}
        elif kind == "integration":
            prior = next((p for p in reversed(_facts(cid, architect.RESULT_KIND))
                          if p.get("check") == oid and p.get("state") in ("green", "red")
                          and _same_code(wt, str(p.get("head") or ""), head)), None)
            if tests_passed is None and prior:
                # The suite already ran at this code (the ladder's non-repair passes; a contract commit
                # under .cc/ changes no code): its result stands.
                r = {k: prior.get(k) for k in ("state", "exitCode", "tail", "seconds", "mechanism")}
            elif tests_passed is None:
                r = {"state": "blocked", "exitCode": None, "tail": "", "seconds": 0,
                     "mechanism": "the integration suite has not run at this head"}
            else:
                ok_ = _truth(tests_passed, False)
                r = {"state": "green" if ok_ else "red", "exitCode": 0 if ok_ else 1, "tail": "", "seconds": 0,
                     "mechanism": "" if ok_ else ("the final test cycle did not pass: " + str(tests_report or "")).rstrip(": ")}
        elif wt and os.path.isdir(wt):
            r = run_check(wt, o)
        else:
            r = {"state": "blocked", "exitCode": None, "tail": "", "seconds": 0, "mechanism": "no worktree to run in"}
        r = {**r, "check": oid, "kind": kind, "head": head, "revision": contract.get("revision")}
        bb.post_fact(cid, "architect", architect.RESULT_KIND, r)
        results[oid] = r
    return {"head": head, "results": results, "score": architect.score(contract, results)}


# ─────────────────────────── definition phase ─────────────────────────────────
@worker_task(task_definition_name="check_scout_build")
def check_scout_build(task):
    """Fork one scout per external check kind the plan's contract draft asks for, each in its own
    worktree; or, for a continuation of a run that already defined done, copy that run's contract and
    evaluation history forward and skip definition."""
    i = task.input_data or {}
    try:
        cid = _cid(i)
        wt = str(i.get("workspacePath") or "").strip()
        lineage = str(i.get("lineage") or "")
        external = _truth(i.get("externalChecks"), True)
        max_repair = max(int(_num(i.get("maxRepairRounds"), 2)), 1)
        bound = max_repair * len(architect.RUNGS) + 6
        prior = lineage[len(CONTINUE_PREFIX):].strip() if lineage.startswith(CONTINUE_PREFIX) else ""
        if cid and contract_of(cid) and not contract_of(cid).get("provisional"):
            return ok(task, {"define": "no", "scouts": "no", "copiedFrom": "", "dynamicTasks": [], "dynamicTasksInput": {},
                             "maxConvergenceIterations": bound}, ["[check_scout_build] contract already on this board"])
        if prior and contract_of(prior):
            contract = contract_of(prior)
            _post_contract(cid, {k: v for k, v in contract.items() if k not in ("ts", "by")}, copiedFrom=prior)
            copied = 0
            for e in evaluations_of(prior):
                bb.post_fact(cid, "architect", architect.EVALUATION_KIND,
                             {**{k: v for k, v in e.items() if k not in ("ts", "by")}, "copiedFrom": prior})
                copied += 1
            for kind in (architect.AMENDMENT_KIND, architect.OVERRIDE_KIND, architect.OPINION_KIND):
                for f in _facts(prior, kind):
                    bb.post_fact(cid, str(f.get("by") or "architect"), kind,
                                 {**{k: v for k, v in f.items() if k not in ("ts", "by")}, "copiedFrom": prior})
            _freeze_paths(cid, contract)
            return ok(task, {"define": "no", "scouts": "no", "copiedFrom": prior, "dynamicTasks": [],
                             "dynamicTasksInput": {}, "maxConvergenceIterations": bound},
                      [f"[check_scout_build] contract revision {contract.get('revision')} and {copied} evaluations "
                       f"copied from {prior[:8]}; definition skipped"])
        briefs = architect.scout_briefs(_mapping(i.get("draft")), external=external)
        # The plan is on the program board before the scouts run: a check drives the program the way
        # the plan's criteria say, and decides only the interface the plan leaves open.
        plan_text = architect.plan_text(_list(_program_fact(cid).get("subsystems")))
        dynamic_tasks, dynamic_inputs = [], {}
        for b in briefs:
            ref = f"scout_{b['kind']}"
            scout_wt = git.worktree_add(wt, f"scout-{b['kind']}")["worktreePath"] if wt and os.path.isdir(wt) else wt
            ctx = {"kind": b["kind"], "description": str(i.get("description") or ""),
                   "claims": "\n".join(f"- {c}" for c in b["claims"]) or "(none named; prove what the request claims)",
                   "hints": "\n".join(f"- {h}" for h in b["hints"]) or "(none)", "plan": plan_text,
                   "cacheRoot": architect.CACHE_ROOT, "checkRoot": architect.CHECK_ROOT}
            dynamic_tasks.append({"name": "coding_agent", "taskReferenceName": ref, "type": "SIMPLE", "optional": True})
            dynamic_inputs[ref] = {
                # Every Bash command is allowed (the disallowed list still holds): a scout runs redis-cli,
                # pip, mkdir, a script under .cc/checks — the coding tools' prefix allow-list denied them all.
                "worktreePath": scout_wt, "tools": _SCOUT_TOOLS, "allowedTools": _SCOUT_TOOLS, "templateKey": "check_scout",
                "prompt": templating.render_default("check_scout", ctx), "promptContext": ctx,
                "promptPath": prompt_files.stash(wt, ref, templating.render_default("check_scout", ctx), ctx),
                "promptTemplate": "", "promptTemplateSource": "", "schema": architect.SCOUT_SCHEMA,
                "modelRole": "code", "maxTurns": 50, "maxBudgetUsd": 3,
                "allowedWriteRoots": [architect.CHECK_ROOT, architect.CACHE_ROOT],
                "sandboxChecks": False, "scoutKind": b["kind"], "scoutWeight": b["weight"],
                # Live activity on the program board, so the console shows the scout working.
                "feedbackCorrelationId": cid, "feedbackItem": f"scout:{b['kind']}", **_model(i)}
        return ok(task, {"define": "yes", "scouts": "yes" if dynamic_tasks else "no", "copiedFrom": "",
                         "dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
                         "maxConvergenceIterations": bound, "kinds": [b["kind"] for b in briefs]},
                  [f"[check_scout_build] scouts={[b['kind'] for b in briefs]} external={external}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "check_scout_build", e)


def _write_authored_scripts(wt: str, candidates: list[dict]) -> None:
    """The Architect's own check scripts into the shared worktree (idempotent: the same content again
    is no change). A shell script is made executable."""
    if not wt or not os.path.isdir(wt):
        return
    for c in candidates:
        script = _mapping(_mapping(c).get("script"))
        rel, content = str(script.get("path") or ""), str(script.get("content") or "")
        if not rel.startswith(architect.CHECK_ROOT + "/") or not content.strip():
            continue
        full = os.path.join(wt, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        current = ""
        if os.path.isfile(full):
            with open(full, encoding="utf-8", errors="replace") as fh:
                current = fh.read()
        if current != content:
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content if content.endswith("\n") else content + "\n")
        if rel.endswith((".sh", ".py")):
            os.chmod(full, os.stat(full).st_mode | 0o111)


def seed_authored_contract(cid: str, wt: str, draft: dict, *, external: bool = True) -> dict:
    """Before the first team forks: the checks the Architect wrote itself go on the branch (committed
    under .cc/) and on the board as a PROVISIONAL contract, so the first implementer's brief carries
    them from its first turn. ``contract_fold`` red-proves them against the untouched head beside the
    build and posts the final revision 1. Nothing to seed → {}."""
    candidates = architect.authored_checks(draft)
    if not candidates or not cid:
        return {}
    _write_authored_scripts(wt, candidates)
    runs = {c["command"]: {"exitCode": 1, "output": "authored by the Architect; red-proof against the untouched "
                                                     "head pending"} for c in candidates}
    contract = {**architect.fold_contract(draft, candidates, runs, external=external), "provisional": True}
    _post_contract(cid, contract)
    _freeze_paths(cid, contract)
    commit = _write_contract(wt, contract, scout_worktrees=[])
    return {"checks": [o["id"] for o in contract["checks"] if o.get("kind") in architect.SCOUT_KINDS],
            "dropped": len(contract["dropped"]), "commit": commit}


def _freeze_paths(cid: str, contract: dict) -> list[str]:
    """The contract file and its check scripts become frozen contract paths for every team."""
    prog = _program_fact(cid)
    paths = [str(p) for p in _list(prog.get("contractPaths"))]
    added = [p for p in architect.contract_paths(contract) if p not in paths]
    if added and prog:
        bb.post_fact(cid, "program", "program", {**prog, "contractPaths": paths + added})
        facts = bb.read_facts(cid, kind="contract")
        rev = int(_num(_mapping(facts[-1].get("data")).get("revision"), 1)) + 1 if facts else 1
        bb.post_fact(cid, "architect", "contract", {"paths": paths + added, "frozen": True, "revision": rev})
    return paths + added


def _write_contract(wt: str, contract: dict, *, scout_worktrees: list[str]) -> str:
    """The contract and its check scripts on the program branch (frozen for every team)."""
    if not wt or not os.path.isdir(wt):
        return ""
    for swt in scout_worktrees:
        src = os.path.join(swt, architect.CHECK_ROOT)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(wt, architect.CHECK_ROOT), dirs_exist_ok=True)
    os.makedirs(os.path.join(wt, architect.CONTRACT_DIR), exist_ok=True)
    with open(os.path.join(wt, architect.CONTRACT_FILE), "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in contract.items() if k not in ("ts", "by")}, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")
    # Only the contract directory is staged: whatever else the worktree holds (a scout's worktree, an
    # check's leftovers) is not the Architect's to commit.
    out = git.commit(wt, f"architect: definition of done, revision {contract.get('revision')}",
                     force_add_paths=[architect.CONTRACT_DIR], write_roots=[architect.CONTRACT_DIR])
    return str(out.get("commit") or "")


@worker_task(task_definition_name="contract_fold")
def contract_fold(task):
    """Revision 1 of the contract from the plan's draft and the scouts' candidates, each candidate
    red-proved here against the untouched head (a pass is vacuous and dropped; a failing setup stays
    blocked). The contract and the scouts' scripts are committed to the program branch and frozen for
    every team."""
    i = task.input_data or {}
    try:
        cid = _cid(i)
        wt = str(i.get("workspacePath") or "").strip()
        existing = contract_of(cid)
        if existing and not existing.get("provisional"):
            head = git.head(wt) if wt and os.path.isdir(wt) else ""
            return ok(task, {"contract": existing, "revision": existing.get("revision"), "commit": "",
                             "contractText": architect.contract_text(existing), "droppedText": _dropped_text(existing),
                             "worktree": _snapshot(wt, head, "define"), "exists": True},
                      ["[contract_fold] contract already defined"])
        scouts, runs, scout_wts = [], {}, []
        for ref, wrapper in _mapping(i.get("scouts")).items():
            out = _agent_out(wrapper)
            s = _structured(out)
            kind = re.sub(r"__[0-9]+$", "", str(ref)).replace("scout_", "", 1)
            s = {**s, "kind": str(s.get("kind") or kind)}
            _spend(cid, f"scout:{kind}", out)
            for p in _list(out.get("filesChanged")):
                if str(p).startswith(architect.CHECK_ROOT + "/"):
                    s.setdefault("paths", []).append(str(p))
            swt = os.path.join(wt, git.WORKTREES, f"scout-{kind}") if wt else ""
            if swt and os.path.isdir(swt):
                scout_wts.append(swt)
            scouts.append(s)
        # The Architect's own checks (``seed_authored_contract``) are red-proved here like a scout's: their
        # scripts are on the branch already (written again only if the seed had no worktree).
        authored = architect.authored_checks(_mapping(i.get("draft")))
        if authored:
            _write_authored_scripts(wt, authored)
            scouts.extend(authored)
        # The scripts must be on the shared worktree before the red-proof runs them there; so must the
        # scouts' tool caches (a Playwright install, a client's virtualenv), or the red-proof, every
        # evaluation snapshot and every team fork download them again.
        for swt in scout_wts:
            src = os.path.join(swt, architect.CHECK_ROOT)
            if os.path.isdir(src) and wt:
                shutil.copytree(src, os.path.join(wt, architect.CHECK_ROOT), dirs_exist_ok=True)
            if wt:
                git.seed_cache(os.path.join(swt, architect.CACHE_ROOT), os.path.join(wt, architect.CACHE_ROOT))
        for s in scouts:
            cmd = str(s.get("command") or "").strip()
            if s.get("found") and cmd and not s.get("need") and wt and os.path.isdir(wt):
                r = run_check(wt, {"command": cmd, "setup": s.get("setup")})
                runs[cmd] = {"exitCode": r["exitCode"], "output": r["tail"],
                             "error": r["mechanism"] if r["state"] == "blocked" else ""}
        contract = architect.fold_contract(_mapping(i.get("draft")), scouts, runs,
                                           external=_truth(i.get("externalChecks"), True))
        _post_contract(cid, contract)
        _freeze_paths(cid, contract)
        for swt in scout_wts:
            git.worktree_remove(wt, os.path.basename(swt))
        commit = _write_contract(wt, contract, scout_worktrees=[])
        return ok(task, {"contract": contract, "revision": 1, "commit": commit, "exists": False,
                         "worktree": _snapshot(wt, commit, "define"),
                         "contractText": architect.contract_text(contract), "droppedText": _dropped_text(contract),
                         "checks": len(contract["checks"]), "dropped": len(contract["dropped"]),
                         "absent": contract["absentKinds"]},
                  [f"[contract_fold] revision 1: {len(contract['checks'])} checks, dropped {len(contract['dropped'])}, "
                   f"absent {sorted(contract['absentKinds'])}, commit {commit[:12]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "contract_fold", e)


def _weights(value) -> dict:
    """The reviewer's weights, as ``[{check, weight}]`` (the strict-mode schema) or a map."""
    if isinstance(value, dict):
        return {str(k): int(_num(v, -1)) for k, v in value.items() if int(_num(v, -1)) >= 0}
    out = {}
    for w in _list(value):
        w = _mapping(w)
        if str(w.get("check") or "").strip() and int(_num(w.get("weight"), -1)) >= 0:
            out[str(w["check"]).strip()] = int(_num(w.get("weight")))
    return out


_SNAPSHOT_PREFIXES = ("eval-", "define-")


def _snapshot(wt: str, head: str, label: str) -> str:
    """A detached worktree at ``head`` for the Architect's sessions and check runs. Never the shared
    integration worktree: teams integrate into it while the Architect works (the definition phase and
    the rank evaluation run beside the builds), a session's runner resets the worktree it was given to
    the head it started from, and a check builds and starts servers in it. Earlier snapshots of this
    run are dropped first; the build caches are copied so a check's tools need no reinstall."""
    if not wt or not os.path.isdir(wt) or not head:
        return wt
    git.exclude_worktrees(wt)
    for name in os.listdir(os.path.join(wt, git.WORKTREES)) if os.path.isdir(os.path.join(wt, git.WORKTREES)) else []:
        if name.startswith(_SNAPSHOT_PREFIXES):
            git.worktree_remove(wt, name)
    snap = git.worktree_add_detached(wt, f"{label}-{head[:8]}", head)["worktreePath"]
    git.seed_cache(os.path.join(wt, architect.CACHE_ROOT), os.path.join(snap, architect.CACHE_ROOT))
    return snap


def _dropped_text(contract: dict) -> str:
    return "\n".join(f"- {d.get('id')} ({d.get('kind')}): {d.get('command')} — {d.get('mechanism')}"
                     for d in _list(_mapping(contract).get("dropped"))) or "(none)"


@worker_task(task_definition_name="contract_review_fold")
def contract_review_fold(task):
    """Apply the reviewer's verdict on the contract (``mode: contract``) or on the pending amendments
    (``mode: amendments``): approve or reject with findings. The contract review is one round
    (``round`` = ``maxRounds`` = 1): a reject applies the reviewer's weights as an amendment with
    mechanism ``review bound reached`` and the build starts. A reject-revise-review round cost 2.7 of
    the 4.8 minutes before the first team forked (run a8167484), for weights the reviewer had already
    named."""
    i = task.input_data or {}
    try:
        cid = _cid(i)
        wt = str(i.get("workspacePath") or "").strip()
        out = _agent_out(i.get("review"))
        _spend(cid, "contract_review", out)
        s = _structured(out)
        verdict = "approve" if str(s.get("verdict") or "") == "approve" else "reject"
        findings = [str(f) for f in _list(s.get("findings"))]
        weights = _weights(s.get("weights"))
        contract = contract_of(cid)
        mode = str(i.get("mode") or "contract")
        if mode == "amendments":
            applied, ids = contract, []
            for a in pending_amendments(cid):
                bb.post_fact(cid, "contract_review", architect.AMENDMENT_KIND,
                             {"id": a.get("id"), "decision": verdict, "findings": findings, "decidedBy": "contract_review"})
                if verdict == "approve":
                    applied = architect.apply_amendment(applied, a, "approve", by=str(a.get("by") or "architect"))
                ids.append(str(a.get("id")))
            if verdict == "approve" and int(_num(applied.get("revision"))) != int(_num(contract.get("revision"))):
                applied = {k: v for k, v in applied.items() if k not in ("ts", "by", "copiedFrom")}
                _post_contract(cid, applied)
                _write_contract(wt, applied, scout_worktrees=[])
            return ok(task, {"approved": "yes" if verdict == "approve" else "no", "decided": ids, "findings": findings,
                             "revision": applied.get("revision")},
                      [f"[contract_review_fold] amendments {ids} {verdict}: {findings[:3]}"])
        rnd, max_rounds = int(_num(i.get("round"), 1)), max(int(_num(i.get("maxRounds"), 2)), 1)
        bound = verdict == "reject" and rnd >= max_rounds
        amendment = {"id": f"review-{rnd}", "op": "weights", "weights": weights, "reason": "; ".join(findings)[:600],
                     "by": "contract_review", "round": rnd}
        if verdict == "approve":
            bb.post_fact(cid, "contract_review", architect.AMENDMENT_KIND, {**amendment, "op": "approve", "decision": "approve"})
            return ok(task, {"approved": "yes", "findings": findings, "revision": contract.get("revision"), "bound": False},
                      [f"[contract_review_fold] approved revision {contract.get('revision')}"])
        if bound:
            applied = architect.apply_amendment(contract, amendment, "approve", by="contract_review")
            applied = {k: v for k, v in applied.items() if k not in ("ts", "by", "copiedFrom")}
            applied["mechanism"] = f"review bound reached after {rnd} rounds; the reviewer's weights applied"
            bb.post_fact(cid, "contract_review", architect.AMENDMENT_KIND,
                         {**amendment, "decision": "approve", "mechanism": "review bound reached"})
            _post_contract(cid, applied)
            _write_contract(wt, applied, scout_worktrees=[])
            return ok(task, {"approved": "yes", "findings": findings, "revision": applied.get("revision"), "bound": True},
                      [f"[contract_review_fold] review bound reached; weights {weights} applied as revision "
                       f"{applied.get('revision')}"])
        bb.post_fact(cid, "contract_review", architect.AMENDMENT_KIND, {**amendment, "decision": "reject"})
        return ok(task, {"approved": "no", "findings": findings, "weights": weights, "revision": contract.get("revision"),
                         "bound": False, "findingsText": "\n".join(f"- {f}" for f in findings) or "(none given)",
                         "weightsText": json.dumps(weights)},
                  [f"[contract_review_fold] rejected round {rnd}: {findings[:3]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "contract_review_fold", e)


# ─────────────────────────── evaluation ───────────────────────────────────────
def _fold_repair(cid: str, joined: dict) -> list[dict]:
    """A repair team's outcome: its spend on the program budget, its item on the program board."""
    out = []
    for ref, wrapper in _mapping(joined).items():
        o = _agent_out(wrapper)
        if not o:
            continue
        sid = re.sub(r"__[0-9]+$", "", str(ref)).replace("team_", "", 1)
        delivered = o.get("delivered") is True
        if _num(o.get("spendUsd")):
            budget_gate.record_spend(cid, f"child:{sid}", usd=float(_num(o.get("spendUsd"))))
        team_board.set_state(cid, "architect", sid, team_board.DONE if delivered else team_board.REVIEW,
                             reason=f"repair team: {o.get('verdict')}" + ("" if delivered else f" — {str(o.get('report') or '')[:300]}"))
        bb.post_fact(cid, "architect", "rollup", {"subsystem": sid, "childRun": str(o.get("subWorkflowId") or ""),
                                                  "verdict": str(o.get("verdict") or ""), "rollup": _mapping(o.get("rollup")),
                                                  "spendUsd": float(_num(o.get("spendUsd"))), "delivered": delivered, "repair": True})
        out.append({"subsystem": sid, "delivered": delivered, "verdict": o.get("verdict")})
    return out


def _subsystems_text(prog: dict, board_items: dict) -> str:
    rows = []
    for s in _list(prog.get("subsystems")):
        s = _mapping(s)
        sid = str(s.get("id"))
        st = _mapping(board_items.get(sid)).get("state") or "backlog"
        rows.append({"id": sid, "state": st, "deliverable": s.get("deliverable") or "code", "files": s.get("files"),
                     "description": str(s.get("description") or "")[:400],
                     "items": [{"id": _mapping(it).get("id"), "files": _mapping(it).get("files"),
                                "acceptanceCriteria": _mapping(it).get("acceptanceCriteria")} for it in _list(s.get("items"))]})
    return json.dumps(rows, default=str)[:16000]


@worker_task(task_definition_name="contract_run")
def contract_run(task):
    """Run every check at the current head and score it; fold a repair team's outcome from the
    previous ladder round; decide the provisional rung; build the Architect's evaluation prompt."""
    i = task.input_data or {}
    try:
        cid = _cid(i)
        wt = str(i.get("workspacePath") or "").strip()
        stage = "final" if str(i.get("stage") or "") == "final" else "rank"
        repaired = _fold_repair(cid, _mapping(i.get("repairJoined")))
        if str(i.get("mode") or "") == "head":
            # After a repair team: fold its outcome and name the head the integration suite must verify.
            head = git.head(wt) if wt and os.path.isdir(wt) else ""
            return ok(task, {"head": head, "repaired": repaired, "stage": stage},
                      [f"[contract_run] repair folded {repaired}; head {head[:12]}"])
        contract = contract_of(cid)
        if stage == "rank" and not _list(i.get("rank")):
            return ok(task, {"evaluate": "no", "score": None, "rung": "", "stage": stage, "repaired": repaired,
                             "head": git.head(wt) if wt and os.path.isdir(wt) else "", "worktree": wt},
                      ["[contract_run] no rank integrated yet; nothing to evaluate"])
        if not contract and stage == "rank":
            # The scouts define done beside the first rank; a rank that finishes before the contract is
            # on the board is evaluated with the next one.
            return ok(task, {"evaluate": "no", "score": None, "rung": "", "stage": stage, "repaired": repaired,
                             "head": git.head(wt) if wt and os.path.isdir(wt) else "", "worktree": wt},
                      ["[contract_run] no contract yet (definition still running); this rank's evaluation waits"])
        if not contract:
            raise ValueError("no outcome contract on the program board; definition did not run")
        tests = i.get("testsPassed")
        # The checks run, and the Architect reads, on a snapshot of the integrated head: the shared
        # worktree keeps changing under the next rank's teams.
        head_now = git.head(wt) if wt and os.path.isdir(wt) else ""
        snap = _snapshot(wt, head_now, "eval")
        run = run_contract(cid, snap, contract, tests_passed=None if tests in (None, "") else tests,
                           tests_report=str(i.get("testReport") or ""))
        revision = int(_num(contract.get("revision"), 1))
        ceiling = _num(i.get("ceilingUsd"), 0)
        spent = budget_gate.read_spend(cid)["usd"] if cid else 0.0
        budget_spent = bool(ceiling) and spent >= ceiling
        finals = evaluations_of(cid, "final")
        history = [{"rung": e.get("rung"), "score": e.get("score"), "revision": e.get("revision")} for e in finals]
        opinion = opinion_of(cid, revision)
        last_repair_ts = max([float(_num(r.get("ts"))) for r in _facts(cid, "rollup") if r.get("repair")] or [0.0])
        forced = "change_approach" if (opinion.get("verdict") == "short" and float(_num(opinion.get("ts"))) > last_repair_ts) else ""
        needs = architect.needs_of(contract, run["results"], budget_spent=budget_spent, spent=spent, ceiling=ceiling)
        needs = [n for n in needs if n["kind"] != "budget"] if not budget_spent else needs
        rung = ""
        if stage == "final":
            rung = architect.next_rung(history, run["score"], _num(contract.get("threshold"), 1.0),
                                       max(int(_num(i.get("maxRepairRounds"), 2)), 1),
                                       overridden=bool(override_of(cid, revision)), budget_spent=budget_spent,
                                       needs=[n for n in needs if n["kind"] != "budget"], opinion_used=bool(opinion),
                                       forced=forced, pending_amendment=bool(pending_amendments(cid)))
        tier = architect.tier_for(history, rung) if rung in architect.RUNGS else 0
        tier += sum(1 for e in finals if e.get("rung") == architect.REPLAN)
        prog = _program_fact(cid)
        board_items = team_board.fold_board(cid)["items"] if cid else {}
        unseen = architect.fold_steer(steer_facts(cid), seen_steer(cid))
        red = architect.red_checks(contract, run["results"])
        prev_items = _list((finals[-1] if finals else {}).get("items"))
        ladder = ""
        if stage == "final":
            ladder = f"The convergence ladder: the score is under the threshold; the rung now is '{rung}'."
            if rung == "change_approach":
                ladder += (" The previous approach did not raise the score. Propose a DIFFERENT plan for the red "
                           "checks (a different decomposition, a different design, or a spike to find out why they "
                           "are red); the same items again are rejected. The previous plan's items:\n"
                           + architect.items_text(prev_items))
            if rung == "rework":
                ladder += " File the items a repair team must do; include the red checks' real causes."
            if forced:
                ladder += "\nThe second opinion judged the work short and proposed:\n" + architect.items_text(opinion.get("items"))
            if rung in (architect.DELIVER,):
                ladder = "The score is at the threshold: this is the sign-off. File only what would be wrong to deliver."
            if rung == architect.NEEDS:
                ladder = "The ladder is exhausted or the budget is spent: record what is left; the run ends naming its needs."
        unbuilt = [s for s in _list(prog.get("subsystems")) if _mapping(board_items.get(str(_mapping(s).get("id")))).get("state") != team_board.DONE]
        notes = ""
        if red:
            notes += "Red checks and their output:\n" + "\n".join(
                f"- {o['id']}: {o.get('result', {}).get('mechanism') or ''}\n{str(o.get('result', {}).get('tail') or '')[-900:]}" for o in red)
        if unbuilt and stage == "final":
            notes += "\n\nSubsystems not built (their items are still open): " + ", ".join(str(_mapping(s).get("id")) for s in unbuilt)
        steer_text = ""
        if unseen["feedback"]:
            steer_text = "Human feedback since the last evaluation (take it seriously; it is advisory, never blocking):\n" + \
                "\n".join(f"- {f['by']}: {f['text']}" for f in unseen["feedback"])
        # At the threshold the sign-off is the score: every check is green, the rung is deliver, and a
        # session could file only advisory items. It ran one to two minutes at the end of every delivered
        # run (runs 993dce38 to 14b40b1a); it runs now only when a human left feedback to be read.
        if stage == "final" and rung == architect.DELIVER and not unseen["feedback"]:
            return ok(task, {"evaluate": "no", "score": run["score"], "threshold": contract.get("threshold"), "revision": revision,
                             "head": run["head"], "worktree": snap, "rung": rung, "tier": tier, "stage": stage,
                             "redCount": 0, "needs": needs, "budgetSpent": budget_spent, "spentUsd": spent, "repaired": repaired,
                             "results": {k: {"state": v.get("state"), "mechanism": v.get("mechanism")} for k, v in run["results"].items()}},
                      [f"[contract_run] final score={run['score']:.2f} at the threshold: sign-off without a session; "
                       f"head={run['head'][:12]} spent=${spent:.2f}"])
        ctx = {"stage": (f"This is the evaluation after rank {i.get('rank')} was integrated. Keep it bounded: judge "
                         f"what this rank added and how it fits the subsystems already built, read only the files of "
                         f"this rank's subsystems and the red checks' output, and do not re-survey the repository; "
                         f"the final evaluation covers the whole system." if stage == "rank"
                         else "This is the evaluation of the final integrated head."),
               "description": str(i.get("description") or ""), "head": run["head"][:12],
               "contract": architect.contract_text(contract, run["results"]), "score": f"{run['score']:.2f}",
               "threshold": f"{_num(contract.get('threshold'), 1.0):.2f}", "subsystems": _subsystems_text(prog, board_items),
               "evaluations": architect.evaluations_text(evaluations_of(cid)), "ladder": ladder, "steer": steer_text,
               "notes": notes}
        return ok(task, {"evaluate": "yes", "score": run["score"], "threshold": contract.get("threshold"), "revision": revision,
                         "head": run["head"], "worktree": snap, "rung": rung, "tier": tier, "stage": stage,
                         "redCount": len(red), "needs": needs,
                         "budgetSpent": budget_spent, "spentUsd": spent, "repaired": repaired,
                         "results": {k: {"state": v.get("state"), "mechanism": v.get("mechanism")} for k, v in run["results"].items()},
                         "evaluatePrompt": templating.render_default("architect_evaluate", ctx), "evaluateContext": ctx,
                         "evaluateSchema": architect.EVALUATE_SCHEMA, "opinionSchema": architect.OPINION_SCHEMA},
                  [f"[contract_run] {stage} score={run['score']:.2f}/{_num(contract.get('threshold'), 1.0):.2f} rung={rung or '-'} "
                   f"red={[o['id'] for o in red]} head={run['head'][:12]} spent=${spent:.2f}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "contract_run", e)


def _apply_human_amendments(cid: str, contract: dict, unseen: dict, wt: str) -> dict:
    """Human steer that changes the contract: ``accept`` (approved on the spot, by the human) and
    ``amend`` (a decision on a pending amendment)."""
    applied = contract
    for a in unseen["accepts"]:
        aid = f"accept-{int(time.time() * 1000)}"
        bb.post_fact(cid, a["by"], architect.AMENDMENT_KIND, {"id": aid, **a, "decision": "approve", "decidedBy": a["by"]})
        applied = architect.apply_amendment(applied, a, "approve", by=a["by"])
    pend = {str(a.get("id")): a for a in pending_amendments(cid)}
    for aid, d in unseen["decisions"].items():
        if aid in pend:
            bb.post_fact(cid, d["by"], architect.AMENDMENT_KIND, {"id": aid, "decision": d["decision"], "decidedBy": d["by"]})
            if d["decision"] == "approve":
                applied = architect.apply_amendment(applied, pend[aid], "approve", by=str(pend[aid].get("by") or "architect"))
    if int(_num(applied.get("revision"))) != int(_num(contract.get("revision"))):
        applied = {k: v for k, v in applied.items() if k not in ("ts", "by", "copiedFrom")}
        _post_contract(cid, applied)
        _write_contract(wt, applied, scout_worktrees=[])
    return applied


def _repair_items(cid: str, items: list, red: list, prog: dict, board_items: dict) -> list[dict]:
    """What a repair team builds: the Architect's items, the still-open items of unbuilt subsystems,
    and — when nothing names the red checks — one item per red check spanning the program's files."""
    out = [dict(it) for it in items]
    ids = {it["id"] for it in out}
    for s in _list(prog.get("subsystems")):
        s = _mapping(s)
        if _mapping(board_items.get(str(s.get("id")))).get("state") != team_board.DONE and _mapping(board_items.get(str(s.get("id")))).get("kind", "subsystem") == "subsystem":
            for it in program.child_items(s):
                if it["id"] not in ids:
                    out.append({**it, "subsystem": str(s.get("id"))})
                    ids.add(it["id"])
    if not out:
        files = sorted({str(f) for s in _list(prog.get("subsystems")) for f in _list(_mapping(s).get("files"))})
        for o in red:
            oid = f"repair-{o.get('id')}"
            if oid not in ids:
                out.append({"id": oid, "description": f"Make the check '{o.get('id')}' pass: {o.get('claim')}. "
                            f"Its latest output: {str(_mapping(o.get('result')).get('tail') or '')[-600:]}",
                            "files": files or ["."], "acceptanceCriteria": [f"the command `{o.get('command') or o.get('kind')}` exits 0 at the integrated head"],
                            "dependsOn": []})
                ids.add(oid)
    for it in out:
        it["dependsOn"] = [d for d in _list(it.get("dependsOn")) if d in ids and d != it["id"]]
    return out


@worker_task(task_definition_name="architect_fold")
def architect_fold(task):
    """Fold the Architect's evaluation: revert any edit, file its items (on the remaining subsystems, or
    for a repair team), post its directives, record its amendments, fold human steer, record the
    evaluation fact, and decide the ladder: a repair team's fork, the second opinion, an amendment
    review, or the end (delivered, or the run's needs)."""
    i = task.input_data or {}
    try:
        cid = _cid(i)
        wt = str(i.get("workspacePath") or "").strip()
        run = _mapping(i.get("run"))
        stage = str(run.get("stage") or i.get("stage") or "rank")
        out = _agent_out(i.get("evaluation"))
        reverted = _revert_edits(str(run.get("worktree") or wt), out)
        _spend(cid, "architect", out)
        s = _structured(out)
        contract = contract_of(cid)
        prog = _program_fact(cid)
        board_items = team_board.fold_board(cid)["items"] if cid else {}
        unseen = architect.fold_steer(steer_facts(cid), seen_steer(cid))
        contract = _apply_human_amendments(cid, contract, unseen, wt)
        revision = int(_num(contract.get("revision"), 1))
        results = results_of(cid)
        score = architect.score(contract, results)
        items, rejected = architect.fold_findings(_list(s.get("items")), subsystems=_list(prog.get("subsystems")))
        for d in _list(s.get("directives")):
            d = _mapping(d)
            if str(d.get("text") or "").strip():
                # Targeted at one item when named: the team's standup hands it to that implementer alone.
                target = f"subsystem:{d.get('subsystem')}" + (f":{d.get('item')}" if str(d.get("item") or "").strip() else "")
                team_board.post_directive(cid, "architect", target, str(d["text"]), authority="agent")
        amendments = []
        decided = [_mapping(f.get("data")) for f in bb.read_facts(cid, kind=architect.AMENDMENT_KIND)]
        for n, a in enumerate(_list(s.get("amendments"))):
            a = _mapping(a)
            why = architect.amendment_problems(a, contract, decided)
            if why:
                rejected.append(f"amendment {n + 1}: {why}")
                continue
            aid = f"arch-{len(evaluations_of(cid)) + 1}-{n + 1}"
            bb.post_fact(cid, "architect", architect.AMENDMENT_KIND, {"id": aid, **a, "by": "architect", "revision": revision})
            amendments.append(aid)
        # Built, being built (the next rank is already forked while this evaluation runs), or handed to
        # the ladder: none of these can take a new item now; such items wait for the final repair.
        done_ids = {sid for sid, it in board_items.items()
                    if it.get("state") in (team_board.DONE, team_board.IN_PROGRESS, team_board.REVIEW, team_board.BLOCKED)}
        filed, deferred = [], []
        if stage == "rank":
            # Items for subsystems still to build join those subsystems' work; the rest wait for the end.
            subs = [_mapping(x) for x in _list(prog.get("subsystems"))]
            by_id = {str(x.get("id")): x for x in subs}
            changed = set()
            for it in items:
                sid = str(it.get("subsystem"))
                if sid in by_id and sid not in done_ids:
                    target = by_id[sid]
                    cur = [_mapping(x) for x in _list(target.get("items"))] or program.child_items(target)
                    if it["id"] not in {str(x.get("id")) for x in cur}:
                        cur.append({k: v for k, v in it.items() if k != "subsystem"})
                        target["items"] = cur
                        target["files"] = sorted({str(f) for x in cur for f in _list(x.get("files"))})
                        changed.add(sid)
                        filed.append(it)
                else:
                    deferred.append(it)
            if changed:
                bb.post_fact(cid, "program", "program", {**prog, "subsystems": subs})
                for sid in changed:
                    team_board.post_item(cid, "architect", {**by_id[sid], "kind": "subsystem"})
        red = architect.red_checks(contract, results)
        ceiling = _num(i.get("ceilingUsd"), 0)
        spent = budget_gate.read_spend(cid)["usd"] if cid else 0.0
        budget_spent = bool(ceiling) and spent >= ceiling
        finals = evaluations_of(cid, "final")
        history = [{"rung": e.get("rung"), "score": e.get("score"), "revision": e.get("revision")} for e in finals]
        rung, act, dynamic_tasks, dynamic_inputs, needs = "", "none", [], {}, []
        if stage == "final":
            opinion = opinion_of(cid, revision)
            last_repair_ts = max([float(_num(r.get("ts"))) for r in _facts(cid, "rollup") if r.get("repair")] or [0.0])
            forced = "change_approach" if (opinion.get("verdict") == "short" and float(_num(opinion.get("ts"))) > last_repair_ts) else ""
            outside = [n for n in architect.needs_of(contract, results, budget_spent=False, spent=spent, ceiling=ceiling)]
            rung = architect.next_rung(history, score, _num(contract.get("threshold"), 1.0),
                                       max(int(_num(i.get("maxRepairRounds"), 2)), 1),
                                       overridden=bool(override_of(cid, revision)), budget_spent=budget_spent,
                                       needs=outside, opinion_used=bool(opinion), forced=forced,
                                       pending_amendment=bool(pending_amendments(cid)))
            prev_items = _list((finals[-1] if finals else {}).get("items"))
            if rung == "change_approach" and items and architect.same_approach(prev_items, items):
                rung = architect.REPLAN
                rejected.append("same approach as before: the items re-state the previous plan; the Architect "
                                "runs again one tier up")
            if rung in architect.RUNGS and rung != "second_opinion":
                repair = _repair_items(cid, items if rung == "change_approach" or items else [], red, prog, board_items)
                if not items and rung == "rework":
                    repair = _repair_items(cid, [], red, prog, board_items)
                n = sum(1 for it in board_items.values() if it.get("kind") == "repair") + 1
                sid = f"repair-{n}"
                tier = architect.tier_for(history, rung) + sum(1 for e in finals if e.get("rung") == architect.REPLAN)
                share = round(max(ceiling - spent, 0.0), 4) if ceiling else 0
                team_board.post_item(cid, "architect", {"id": sid, "kind": "repair", "title": f"repair round {n} ({rung})",
                                                        "description": f"{rung}: {', '.join(o['id'] for o in red)}",
                                                        "files": sorted({f for it in repair for f in _list(it.get("files"))})})
                team_board.set_state(cid, "architect", sid, team_board.IN_PROGRESS, assignee=f"team_{sid}")
                for it in repair:
                    it["subsystem"] = sid
                common_in = {**{k: i.get(k) for k in _TEAM_KEYS if i.get(k) not in (None, "")},
                             "repoPath": i.get("repoPath"), "workspacePath": wt, "contractPaths": prog.get("contractPaths") or [],
                             "ceilingUsd": share, "parentCorrelationId": cid, "subsystemId": sid, "baseModelTier": tier,
                             "subtasks": [{k: v for k, v in it.items() if k != "subsystem"} for it in repair], "forceTeam": True}
                team_board.post_directive(
                    cid, "architect", f"subsystem:{sid}",
                    f"The program's definition of done is red ({', '.join(o['id'] for o in red)}). " + "\n".join(
                        f"{o['id']}: {_mapping(o.get('result')).get('mechanism') or ''} {str(_mapping(o.get('result')).get('tail') or '')[-500:]}"
                        for o in red) + f"\nThe Architect's assessment: {str(s.get('assessment') or '')[:1200]}", authority="agent")
                dynamic_tasks = [{"name": "team_build", "taskReferenceName": f"team_{sid}", "type": "SUB_WORKFLOW",
                                  "optional": True, "subWorkflowParam": {"name": "team_build"}}]
                dynamic_inputs = {f"team_{sid}": common_in}
                act = "repair"
                filed = repair
            elif rung == "second_opinion":
                act = "second_opinion"
            elif rung == architect.AMEND:
                act = "amend"
            elif rung == architect.NEEDS:
                needs = architect.needs_of(contract, results, budget_spent=budget_spent, spent=spent, ceiling=ceiling,
                                           owner_items=red if not budget_spent and not outside else None)
                for n_ in needs:
                    bb.post_fact(cid, "architect", architect.NEED_KIND, {**n_, "evaluation": len(evaluations_of(cid)) + 1,
                                                                        "revision": revision})
        bb.post_fact(cid, "architect", architect.EVALUATION_KIND, {
            "stage": stage, "rank": i.get("rank") if stage == "rank" else "final", "revision": revision, "score": score,
            "threshold": contract.get("threshold"), "head": run.get("head") or "", "rung": rung, "act": act,
            "items": filed, "deferred": deferred, "rejected": rejected, "amendments": amendments,
            "assessment": str(s.get("assessment") or "")[:3000], "steerSeen": unseen["seen"],
            "reverted": reverted, "needs": needs, "tier": run.get("tier"),
            "results": {k: {"state": v.get("state"), "mechanism": str(v.get("mechanism") or "")[:200]} for k, v in results.items()}})
        if reverted:
            team_board.post_exception(cid, "architect", "architect", "architect_wrote",
                                      f"architect wrote to the repository ({', '.join(reverted[:5])}); reverted", severity="info")
            team_board.resolve_exception(cid, "architect", "architect", "architect_wrote", "edits reverted")
        # The prompts of the rungs that need one: the second opinion (it needs this evaluation's
        # assessment) and the amendment review (the amendments just proposed).
        steer_text = ("Human feedback since the last evaluation:\n" + "\n".join(f"- {f['by']}: {f['text']}" for f in unseen["feedback"])
                      if unseen["feedback"] else "")
        opinion_ctx = {"description": str(i.get("description") or ""), "head": str(run.get("head") or "")[:12],
                       "contract": architect.contract_text(contract, results), "score": f"{score:.2f}",
                       "threshold": f"{_num(contract.get('threshold'), 1.0):.2f}",
                       "evaluations": architect.evaluations_text(evaluations_of(cid)),
                       "assessment": str(s.get("assessment") or "")[:3000], "steer": steer_text}
        pend = pending_amendments(cid)
        amend_text = "\n".join(f"- {a.get('id')} by {a.get('by')}: {a.get('op')} {a.get('check') or ''} "
                               f"{('weight ' + str(a.get('weight'))) if a.get('weight') is not None else ''} "
                               f"{('threshold ' + str(a.get('threshold'))) if a.get('threshold') is not None else ''} — {a.get('reason')}"
                               for a in pend) or "(none)"
        amend_ctx = {"description": str(i.get("description") or ""), "revision": revision,
                     "contract": architect.contract_text(contract, results), "dropped": _dropped_text(contract),
                     "feedback": "Proposed amendments to decide (approve applies them all; reject applies none; a "
                                 "rejected check stays red and the ladder continues):\n" + amend_text}
        return ok(task, {"rung": rung, "act": act, "score": score, "revision": revision, "continue": rung not in (architect.DELIVER, architect.NEEDS, ""),
                         "delivered": rung == architect.DELIVER, "dynamicTasks": dynamic_tasks, "dynamicTasksInput": dynamic_inputs,
                         "itemsFiled": [it.get("id") for it in filed], "deferred": [it.get("id") for it in deferred],
                         "rejected": rejected, "amendments": amendments, "needs": needs, "reverted": reverted,
                         "head": str(run.get("head") or ""), "assessment": str(s.get("assessment") or "")[:3000],
                         "opinionPrompt": templating.render_default("second_opinion", opinion_ctx), "opinionContext": opinion_ctx,
                         "amendText": amend_text, "amendContext": amend_ctx},
                  [f"[architect_fold] {stage} score={score:.2f} rung={rung or '-'} act={act} filed={[it.get('id') for it in filed]} "
                   f"deferred={len(deferred)} rejected={len(rejected)} amendments={amendments} reverted={reverted}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "architect_fold", e)


@worker_task(task_definition_name="opinion_fold")
def opinion_fold(task):
    """Record the second opinion: ``meets`` overrides (the program delivers on its word, once per
    revision), ``contract_wrong`` proposes amendments for the reviewer, ``short`` sends the ladder back
    to a change of approach with its items."""
    i = task.input_data or {}
    try:
        cid = _cid(i)
        wt = str(i.get("workspacePath") or "").strip()
        out = _agent_out(i.get("opinion"))
        reverted = _revert_edits(str(i.get("worktree") or wt), out)
        _spend(cid, "second_opinion", out)
        s = _structured(out)
        contract = contract_of(cid)
        revision = int(_num(contract.get("revision"), 1))
        verdict = str(s.get("verdict") or "short")
        if verdict not in ("meets", "contract_wrong", "short"):
            verdict = "short"
        items, rejected = architect.fold_findings(_list(s.get("items")), subsystems=_list(_program_fact(cid).get("subsystems")))
        amendments = []
        if verdict == "contract_wrong":
            for n, a in enumerate(_list(s.get("amendments"))):
                a = _mapping(a)
                why = architect.amendment_problems(a)
                if why:
                    rejected.append(f"amendment {n + 1}: {why}")
                    continue
                aid = f"opinion-{revision}-{n + 1}"
                bb.post_fact(cid, "second_opinion", architect.AMENDMENT_KIND, {"id": aid, **a, "by": "second_opinion"})
                amendments.append(aid)
            if not amendments:
                verdict = "short"
                rejected.append("contract_wrong without a recordable amendment is judged short")
        bb.post_fact(cid, "second_opinion", architect.OPINION_KIND,
                     {"revision": revision, "verdict": verdict, "reasoning": str(s.get("reasoning") or "")[:3000],
                      "items": items, "amendments": amendments, "rejected": rejected, "reverted": reverted})
        if verdict == "meets":
            bb.post_fact(cid, "second_opinion", architect.OVERRIDE_KIND,
                         {"revision": revision, "reasoning": str(s.get("reasoning") or "")[:3000], "by": "second_opinion"})
        return ok(task, {"verdict": verdict, "items": [it.get("id") for it in items], "amendments": amendments,
                         "rejected": rejected, "revision": revision},
                  [f"[opinion_fold] {verdict} revision {revision}: items={len(items)} amendments={amendments}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "opinion_fold", e)
