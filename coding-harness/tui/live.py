"""What a run is doing right now, from the orchestrator: every agent session of the run and its child
runs (role, item, elapsed, the last commands it ran, tokens, cost) and every step with its timing.

The board records what agents decide (items, directives, lessons); it does not record what a session
is doing minute by minute, and a Codex session posts no per-tool activity at all, so the console showed
"Agents working: 0" through a four-minute build and the page read as done-or-not-done (run f83293f5).
Conductor holds the live detail: the agent runner reports each session's turns into its task output as
it works, and every step has a start and an end. This module reads that, through the sub-workflow tree,
into one structure the run page renders as "working now" and as a timeline.
"""

from __future__ import annotations

import time

# Task types that are control flow, not work: never a step on the timeline.
_CONTROL = {"SWITCH", "JOIN", "SET_VARIABLE", "DECISION", "INLINE", "TERMINATE", "FORK_JOIN", "FORK_JOIN_DYNAMIC",
            "DO_WHILE", "START_WORKFLOW", "EXCLUSIVE_JOIN", "EVENT", "WAIT"}
_RUNNING = {"IN_PROGRESS", "SCHEDULED"}
_FAILED = {"FAILED", "FAILED_WITH_TERMINAL_ERROR", "TIMED_OUT"}
_MAX_DEPTH = 10  # a swarm's dependency chain nests one sub-workflow per link
_MAX_STEPS = 400

# The prompt key a session was dispatched with names its role.
ROLES = {
    "program_plan": "planner", "program_breakdown": "planner", "plan": "planner", "design": "designer",
    "intake_classify": "classifier", "check_scout": "scout", "contract_review": "contract reviewer",
    "architect_evaluate": "architect", "second_opinion": "second opinion", "team_implement": "implementer",
    "team_rescue": "helper", "team_acceptance_tests": "test author", "team_review": "reviewer", "code": "implementer",
    "test_repair": "test repairer",
    "delivery_repair": "delivery repairer", "retrospective": "retrospective", "merge_conflict": "merge resolver",
    "bug_repro": "reproducer", "local_review": "reviewer", "pr_review": "reviewer", "address_pr": "implementer",
    # The swarm: the architect plans and, after every round, reviews the whole tree; implementers build.
    "swarm_plan": "architect (planning)", "swarm_evaluate": "architect (reviewing the round)",
    "swarm_work": "implementer", "swarm_review_item": "reviewer (item)",
}
# Steps whose reference names say what they are, for the timeline's labels.
_STEP_LABELS = (
    # The swarm's harness steps, in what-it-is-doing words.
    ("setup", "preparing the run's clone"), ("seed_round", "starting round 1"), ("dispatch", "dispatching the round"),
    ("work_join", "waiting for the round's agents"), ("fold", "collecting the round"),
    ("measure", "running the checks on the committed tree"), ("decide", "committing and deciding"),
    ("finish", "handing the branch back"), ("prep", "preparing the item"),
    ("release", "starting what waited on the item"), ("dependents", "dependent items"),
    ("program_plan", "planning"), ("scout_", "scout"), ("contract_fold", "red-proof and fold"),
    ("contract_review", "contract review"), ("rank_build", "forking the rank's teams"),
    ("team_", "team"), ("accept_", "test author"), ("acceptance_", "acceptance tests"),
    ("standup", "standup"), ("integrate", "integration"), ("review_build", "review dispatch"),
    ("review_", "reviewer"), ("replan", "replan"), ("monitor", "monitor"), ("wave_budget", "budget"),
    ("tests", "test cycle"), ("subtask_tests", "fork test cycle"), ("code", "implementer"),
    ("initial_delivery_audit", "delivery audit"), ("delivery_repair", "delivery repair"), ("cmt", "commit"),
    ("wt", "worktree"), ("final_contract_run", "running the checks"), ("architect_evaluate", "architect"),
    ("architect_fold", "architect fold"), ("repair_", "repair team"), ("outcome", "verdict"),
    ("retrospective", "retrospective"), ("gate_refresh", "final gate"), ("workspace", "workspace"),
    ("memory_", "memory"), ("model_policy", "model policy"), ("rank_init", "ranks"),
)


def _ms(v) -> float:
    try:
        return float(v or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _structured(o: dict) -> dict:
    s = o.get("structured")
    if isinstance(s, str):
        import json
        try:
            s = json.loads(s)
        except ValueError:
            s = {}
    return s if isinstance(s, dict) else {}


def _status(t: dict) -> str:
    st = str(t.get("status") or "")
    if st in _RUNNING:
        return "running"
    if st in _FAILED:
        return "failed"
    if st == "CANCELED":
        return "canceled"
    if st.startswith("COMPLETED"):
        return "done"
    return st.lower() or "unknown"


def _label(ref: str, kind: str) -> str:
    base = ref.split("__")[0]
    for prefix, label in _STEP_LABELS:
        if base.startswith(prefix):
            return label
    return base.replace("_", " ")


def _session(t: dict, *, run: str, item_hint: str) -> dict:
    i, o = t.get("inputData") or {}, t.get("outputData") or {}
    key = str(i.get("templateKey") or "")
    ref = str(t.get("referenceTaskName") or "")
    role = ROLES.get(key) or ROLES.get(ref.split("__")[0]) or (("scout " + ref.replace("scout_", "")) if ref.startswith("scout_") else "") \
        or _label(ref, "coding_agent")
    ctx = i.get("promptContext") if isinstance(i.get("promptContext"), dict) else {}
    item = str(i.get("feedbackItem") or ctx.get("item") or item_hint or "")
    turns = o.get("turns") if isinstance(o.get("turns"), list) else []
    commands = [str(c) for tr in turns if isinstance(tr, dict) for c in (tr.get("commands") or [])]
    text = ""
    for tr in reversed(turns):
        if isinstance(tr, dict) and str(tr.get("text") or "").strip():
            text = str(tr.get("text"))
            break
    s = _structured(o)
    started, ended = _ms(t.get("startTime")), _ms(t.get("endTime"))
    status = _status(t)
    now = time.time()
    return {
        "ref": ref, "role": role, "item": item, "run": run, "status": status,
        "startedAt": started or None, "endedAt": ended or None,
        "seconds": round(((ended or now) - started)) if started else 0,
        "updatedAt": _ms(t.get("updateTime")) or None,
        "turns": int(o.get("numTurns") or len(turns) or 0),
        "tokens": int(o.get("tokenUsed") or 0), "costUsd": float(o.get("costUsd") or 0.0),
        "model": str(o.get("model") or ""),
        "commands": len(commands), "lastCommands": [c[:200] for c in commands[-4:]],
        "lastText": text[:400],
        "filesChanged": int(o.get("filesChangedTotal") or len(o.get("filesChanged") or [])),
        "summary": str(s.get("summary") or o.get("result") or "")[:400],
        "pushback": str(s.get("pushback") or "")[:300],
    }


def collect(wf: dict, fetch, *, _depth: int = 0, _label_hint: str = "", _item_hint: str = "",
            _acc: dict | None = None) -> dict:
    """Walk a run and its child runs (``fetch(workflow_id)`` returns an execution with tasks). Returns
    ``{"sessions": [...], "steps": [...]}``, both in start order; a child run's steps are nested one
    level deeper and name their run."""
    acc = _acc if _acc is not None else {"sessions": [], "steps": []}
    run = str(wf.get("workflowId") or "")
    kind = str(wf.get("workflowName") or wf.get("workflowType") or "")
    inp = wf.get("input") if isinstance(wf.get("input"), dict) else {}
    item_hint = (str(inp.get("name") or inp.get("subsystemId") or "") if kind in ("code_subtask", "team_build")
                 else str((inp.get("item") or {}).get("id") or "") if kind == "swarm_item" and isinstance(inp.get("item"), dict)
                 else _item_hint)
    for t in wf.get("tasks") or []:
        if not isinstance(t, dict) or t.get("taskType") in _CONTROL:
            continue
        ref = str(t.get("referenceTaskName") or "")
        started, ended = _ms(t.get("startTime")), _ms(t.get("endTime"))
        step = {"ref": ref, "type": str(t.get("taskType") or ""), "label": _label(ref, str(t.get("taskType") or "")),
                "status": _status(t), "startedAt": started or None, "endedAt": ended or None,
                "seconds": round(((ended or time.time()) - started)) if started else 0,
                "depth": _depth, "run": run, "item": item_hint, "runKind": kind,
                "reason": str(t.get("reasonForIncompletion") or "")[:300],
                # What a harness step reports while it runs (``progress``): which check, for how long.
                "detail": str((t.get("outputData") or {}).get("progress") or "")[:400]
                          if _status(t) == "running" else ""}
        if t.get("taskType") == "coding_agent":
            s = _session(t, run=run, item_hint=item_hint)
            step.update({"role": s["role"], "item": s["item"], "costUsd": s["costUsd"], "tokens": s["tokens"]})
            acc["sessions"].append(s)
        if len(acc["steps"]) < _MAX_STEPS:
            acc["steps"].append(step)
        child = str(t.get("subWorkflowId") or "")
        if t.get("taskType") == "SUB_WORKFLOW" and child and _depth + 1 <= _MAX_DEPTH:
            try:
                cw = fetch(child)
            except Exception:  # noqa: BLE001 - one unreadable child never hides the rest
                cw = None
            if isinstance(cw, dict) and cw.get("tasks") is not None:
                collect({**cw, "workflowId": child}, fetch, _depth=_depth + 1, _item_hint=item_hint, _acc=acc)
    if _acc is None:
        acc["sessions"].sort(key=lambda s: s["startedAt"] or 0)
        acc["steps"].sort(key=lambda s: (s["startedAt"] or 0, s["depth"]))
        acc["running"] = [s for s in acc["sessions"] if s["status"] == "running"]
        acc["roles"] = _roles(acc["sessions"])
    return acc


def _roles(sessions: list[dict]) -> list[dict]:
    """How many sessions of each role ran, are running, failed: the swarm's roster at a glance."""
    out: dict[str, dict] = {}
    for s in sessions:
        r = out.setdefault(s["role"], {"role": s["role"], "done": 0, "running": 0, "failed": 0, "costUsd": 0.0, "seconds": 0})
        r["running" if s["status"] == "running" else "failed" if s["status"] == "failed" else "done"] += 1
        r["costUsd"] = round(r["costUsd"] + s["costUsd"], 4)
        r["seconds"] += s["seconds"]
    return sorted(out.values(), key=lambda r: -(r["running"] * 1000 + r["seconds"]))


def phrase(live: dict) -> str:
    """One line for the run page's phase: who is working on what, and for how long."""
    running = list(live.get("running") or [])
    if not running:
        return ""
    parts = []
    for s in running[:3]:
        what = s["lastText"].splitlines()[0][:90] if s["lastText"] else (s["lastCommands"][-1][:90] if s["lastCommands"] else "")
        parts.append(f"{s['role']}{' on ' + s['item'] if s['item'] else ''} for {s['seconds'] // 60} min"
                     + (f", {s['commands']} commands" if s["commands"] else "") + (f": {what}" if what else ""))
    more = f" and {len(running) - 3} more" if len(running) > 3 else ""
    return "; ".join(parts) + more
