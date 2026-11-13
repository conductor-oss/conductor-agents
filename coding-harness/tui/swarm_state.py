"""State on demand (§4.8): anyone — a human, the UI, an API client, another agent — can ask a running
swarm where it is, at any moment, and get an answer.

Two layers, both views over the boards (no state of their own):

- ``snapshot(run)`` — a structured, machine-readable state: lifecycle status and phase, progress, the
  teams of a program, what every agent is doing RIGHT NOW (the ``activity`` heartbeat implementers
  post from their tool hook), open escalations, recent events, spend against the ceiling. This is what
  the UI renders and what ``GET /api/state`` returns to other agents.
- ``ask(run, question)`` — a plain-language answer from Claude grounded in that snapshot (and the full
  timeline of any item the question names). This is what the message box, ``POST /api/ask`` and the
  TUI chat use.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

ACTIVE_WINDOW_S = 120.0     # an agent whose last tool call is older than this is not shown as active
MODEL = os.environ.get("SWARM_ASK_MODEL") or "claude-opus-5-5"
_TERMINAL = {"COMPLETED", "FAILED", "TERMINATED", "TIMED_OUT"}


def _console():
    try:
        from . import console as console_mod
    except ImportError:  # loaded as a top-level module next to console.py
        import console as console_mod
    return console_mod


def _trace():
    try:
        from . import board_trace
    except ImportError:
        import board_trace
    return board_trace


def _workflow(run: str, server_url: str | None = None, opener=None) -> dict:
    """The run's Conductor execution (status, name, input, running tasks); {} when unreachable."""
    base = (server_url or os.environ.get("CONDUCTOR_SERVER_URL") or "http://localhost:8080/api").rstrip("/")
    token = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
    req = urllib.request.Request(f"{base}/workflow/{run}?includeTasks=true",
                                 headers={**({"X-Authorization": token} if token else {})})
    try:
        with (opener or urllib.request.urlopen)(req) as resp:  # noqa: S310 - operator-configured URL
            return json.loads(resp.read() or b"{}")
    except Exception:  # noqa: BLE001 - state still renders from the board alone
        return {}


def _phase(wf: dict, view: dict) -> str:
    status = str(wf.get("status") or "")
    if status in _TERMINAL:
        verdict = (wf.get("output") or {}).get("verdict")
        return f"finished ({status.lower()}{', ' + str(verdict) if verdict else ''})"
    running = [str(t.get("referenceTaskName") or "") for t in wf.get("tasks") or []
               if t.get("status") in ("IN_PROGRESS", "SCHEDULED")]
    if any(r.startswith("retrospective") for r in running):
        return "retrospective (recording learnings)"
    building = any(r.startswith(("team_", "rank_fan_out", "rank_join")) for r in running)
    if any(r.startswith(("scout_", "scouts", "contract_")) for r in running):
        return ("building rank one while defining done (scouts, review)" if building
                else "defining done (scouting and red-proving the checks)")
    if any(r.startswith(("final_contract_run", "architect_", "repair_", "second_opinion", "opinion_fold", "amendment_"))
           for r in running):
        return "converging (the Architect's ladder)"
    if any(r.startswith("rank_contract_run") or r.startswith("rank_architect") for r in running):
        return "architect evaluating the integrated rank"
    if any(r.startswith(("tests", "final_tests")) for r in running):
        return "final verification (full test suite)"
    if any(r.startswith(("plan", "program_plan", "classify")) for r in running) and not view["items"]:
        return "planning"
    r = view["rollup"]
    if not r.get("total"):
        return "starting" if status else "unknown (no board yet)"
    return f"building ({r.get('done', 0)}/{r.get('total', 0)} done)"


def _activity(runs: list[str]) -> list[dict]:
    from common import blackboard as bb
    now, latest = time.time(), {}
    for run in runs:
        for f in bb.read_facts(run, kind="activity", since=now - ACTIVE_WINDOW_S):
            d = f.get("data") or {}
            key = (run, str(d.get("item")))
            if key not in latest or float(f.get("ts") or 0) > latest[key]["ts"]:
                latest[key] = {"run": run, "item": d.get("item"), "tool": d.get("tool"),
                               "toolCalls": d.get("toolCalls"), "ts": float(f.get("ts") or 0)}
    out = []
    for a in latest.values():
        out.append({**a, "secondsAgo": round(now - a["ts"]), "ts": a["ts"]})
    return sorted(out, key=lambda a: a["secondsAgo"])


def _recent(runs: list[str], limit: int = 12) -> list[dict]:
    from common import blackboard as bb
    trace = _trace()
    events = []
    for run in runs:
        for kind in ("state", "directive", "exception", "delivery", "review", "contract_event",
                     "lesson", "decision", "decision_request"):
            for f in bb.read_facts(run, kind=kind)[-limit:]:
                d = f.get("data") or {}
                if kind == "lesson":
                    summary = f"lesson: {d.get('text') or ''}"
                elif kind == "decision":
                    summary = f"ARCHITECT DECISION: {d.get('decision') or ''}"
                elif kind == "decision_request":
                    summary = f"asked the Architect to decide: {d.get('question') or ''}"
                else:
                    summary = trace._summary(kind, d)
                events.append({"ts": f.get("ts"), "run": run, "kind": kind, "by": d.get("by") or f.get("author"),
                               "summary": summary})
    events.sort(key=lambda e: e["ts"] or 0)
    return events[-limit:]


def _retrospective(run: str) -> dict | None:
    """What the run's retrospective concluded, once it ran: the summary and how many learnings it
    recorded on the shared learning board (``_swarm_learnings``)."""
    from common import blackboard as bb
    facts = bb.read_facts(run, kind="retrospective")
    if not facts:
        return None
    d = facts[-1].get("data") or {}
    return {"summary": str(d.get("summary") or ""), "recorded": len(d.get("learnings") or []),
            "learnings": [{"scope": x.get("scope"), "text": x.get("text")} for x in d.get("learnings") or []
                          if isinstance(x, dict)]}


def snapshot(run: str, *, blackboard_dir: str | None = None, server_url: str | None = None,
             opener=None, wf: dict | None = None, view: dict | None = None, live: dict | None = None) -> dict:
    """The run's current state, structured for UIs, APIs and other agents. ``wf``/``view`` let a caller
    that already fetched the execution and folded the board (the console's run view) pass them in;
    ``live`` (``live.collect``) adds the sessions the orchestrator says are running, so an agent that
    posts no per-tool activity (a Codex session) still shows as working, with what it is doing."""
    console = _console()
    view = view if view is not None else console.board_json(run, blackboard_dir=blackboard_dir)
    wf = wf if wf is not None else _workflow(run, server_url, opener)
    runs = [run] + [str(c["run"]) for c in view.get("children") or [] if c.get("run")]
    ceiling = (wf.get("input") or {}).get("ceilingUsd")
    spend = float(view["spend"].get("usd", 0) or 0)
    active = _activity(runs)
    seen = {(a["run"], str(a["item"])) for a in active}
    for s in (live or {}).get("running") or []:
        if (s["run"], str(s["item"])) in seen:
            continue
        last = s["lastCommands"][-1] if s["lastCommands"] else ""
        active.append({"run": s["run"], "item": s["item"] or s["role"], "role": s["role"],
                       "tool": (last[:120] or (s["lastText"][:120] if s["lastText"] else "") or "working"),
                       "toolCalls": s["commands"], "seconds": s["seconds"], "tokens": s["tokens"], "costUsd": s["costUsd"],
                       "lastText": s["lastText"], "ts": s["updatedAt"] or s["startedAt"] or time.time(),
                       "secondsAgo": round(time.time() - float(s["updatedAt"] or s["startedAt"] or time.time()))})
    teams = [{"subsystem": c.get("subsystem"), "run": c.get("run"), "verdict": c.get("verdict") or "running",
              "done": c["rollup"].get("done", 0), "total": c["rollup"].get("total", 0),
              "escalations": c.get("openExceptions", 0)} for c in view.get("children") or []]
    phase = _phase(wf, view)
    if live and str(wf.get("status") or "") == "RUNNING":
        import live as live_mod
        doing = live_mod.phrase(live)
        if doing:
            phase = f"{phase} — {doing}"
    snap = {
        "run": run, "workflow": wf.get("workflowName") or "", "status": wf.get("status") or "unknown",
        "phase": phase, "progress": {"done": view["rollup"].get("done", 0),
                                               "total": view["rollup"].get("total", 0)},
        "items": [{"id": it.get("id"), "state": it.get("state"), "deliverable": it.get("deliverable") or "code",
                   "reason": it.get("reason") or ""} for it in view["items"]],
        "teams": teams, "activeAgents": active,
        "escalations": [{"item": e.get("item"), "kind": e.get("kind"), "detail": e.get("detail")}
                        for e in view["exceptions"]],
        "constraints": [{"id": d.get("id"), "target": d.get("target"), "text": d.get("text"), "by": d.get("by")}
                        for d in view["constraints"]],
        "recentEvents": _recent(runs),
        "spend": {"usd": round(spend, 4), "ceilingUsd": ceiling},
        "retrospective": _retrospective(run),
        "failure": _failure(wf),
        "updatedAt": time.time(),
    }
    snap["summary"] = summarize(snap)
    return snap


def _failure(wf: dict) -> dict | None:
    """Why a run failed or was stopped, from Conductor: the run's reason and each failed task's, once
    per distinct reason. The board records nothing for a run that died before planning (run edc1c893
    failed on `git rev-parse HEAD` in a directory that was not a repository), so without this a question
    "why did it fail?" was answered "the snapshot does not say"."""
    status = str(wf.get("status") or "")
    if status not in ("FAILED", "TERMINATED", "TIMED_OUT"):
        return None
    tasks, seen = [], set()
    for t in wf.get("tasks") or []:
        if t.get("status") in ("FAILED", "FAILED_WITH_TERMINAL_ERROR", "TIMED_OUT"):
            reason = str(t.get("reasonForIncompletion") or "").strip()
            key = (t.get("referenceTaskName"), reason)
            if reason and key not in seen:
                seen.add(key)
                tasks.append({"task": t.get("referenceTaskName"), "type": t.get("taskType"),
                              "reason": reason[:1500], "attempts": sum(
                                  1 for x in wf.get("tasks") or [] if x.get("referenceTaskName") == t.get("referenceTaskName"))})
    return {"status": status, "reason": str(wf.get("reasonForIncompletion") or "").strip()[:1500], "failedTasks": tasks}


def summarize(s: dict) -> str:
    """One line a human or an agent can read at a glance."""
    parts = [f"{s['workflow'] or 'run'} {s['run'][:8]}: {s['phase']}"]
    if s["teams"]:
        done = sum(1 for t in s["teams"] if t["verdict"] == "DELIVERED")
        parts.append(f"{done}/{len(s['teams'])} teams delivered")
    if s["activeAgents"]:
        parts.append(f"{len(s['activeAgents'])} agent(s) working: "
                     + ", ".join(f"{a['item']} ({a['tool'] or 'thinking'})" for a in s["activeAgents"][:4]))
    if s["escalations"]:
        parts.append(f"{len(s['escalations'])} escalation(s)")
    ceiling = s["spend"].get("ceilingUsd")
    parts.append(f"spend ${s['spend']['usd']:.2f}" + (f" of ${ceiling}" if ceiling else ""))
    if s.get("failure"):
        f = s["failure"]
        cause = (f["failedTasks"][0]["reason"] if f.get("failedTasks") else f.get("reason")) or "no reason recorded"
        parts.append(f"{f['status'].lower()}: {cause.splitlines()[0][:200]}")
    if s.get("retrospective"):
        parts.append(f"retrospective recorded {s['retrospective']['recorded']} learning(s)")
    return " · ".join(parts)


_ASK_SYSTEM = """You answer questions about a running AI software-construction team (a swarm of coding
agents organized as teams, possibly teams of teams) from a live state snapshot of its shared board. Be
direct and specific: name items, teams and agents; say what is done, in progress, stuck and why; quote
numbers from the snapshot. Say plainly when the snapshot does not contain something. Do not invent
progress. Keep it short unless asked for detail. When the run failed or was stopped, "failure" holds the
cause the orchestrator recorded: lead with it in plain words, say what to change, and say the run has
ended (a fix needs a new run). Humans are observers: never ask them to approve anything; if they want a
change to a running team, tell them to say it in the message box."""


def ask(run: str, question: str, *, blackboard_dir: str | None = None, client=None, snap: dict | None = None) -> dict:
    """Answer a plain-language question about the run's current state."""
    snap = snap or snapshot(run, blackboard_dir=blackboard_dir)
    trace = _trace()
    named = [it["id"] for it in snap["items"] if it["id"] and str(it["id"]).lower() in question.lower()][:3]
    timelines = {iid: [{"kind": e["kind"], "by": e["author"], "summary": e["summary"]}
                       for e in trace.item_timeline(run, iid)[-25:]] for iid in named}
    import anthropic
    client = client or anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "low"}, system=_ASK_SYSTEM,
        messages=[{"role": "user", "content":
                   f"State snapshot:\n{json.dumps(snap, default=str)}\n\n"
                   + (f"Item timelines:\n{json.dumps(timelines, default=str)}\n\n" if timelines else "")
                   + f"<question>\n{question}\n</question>"}])
    if response.stop_reason == "refusal":
        return {"ok": False, "answer": "", "error": "question declined", "summary": snap["summary"]}
    answer = "".join(b.text for b in response.content if b.type == "text").strip()
    return {"ok": True, "answer": answer, "summary": snap["summary"]}
