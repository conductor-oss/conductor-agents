"""Web Swarm Console (roadmap Phase 3, §4.8) — the always-on, shareable surface for a long run.

A days-long, many-agents run needs a visual surface a terminal can't give: the **rollup + exception
queue** (the default landing surface, H7), a kanban, a cost-burn bar, and agent drill-down. Per §4.8
the console holds **no state of its own** — it is a *view + editor over the boards*: every GET reads
the blackboard (the same fold the Lead/monitor use) and every POST steer is the same authored board
write an agent makes, so there is no second source of truth.

Stdlib only (``http.server``) — no framework, consistent with a view-over-boards that must not become
a second system. The request logic (``handle_get`` / ``handle_post``) is separated from the HTTP
transport so it is unit-testable without a socket.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_TUI = Path(__file__).resolve().parent
if str(_TUI) not in sys.path:
    sys.path.insert(0, str(_TUI))

import board as board_mod  # noqa: E402
from board import _WORKERS  # noqa: E402

from common import actors, retrospective, team_board  # noqa: E402

try:
    from . import notify as _notify
    from .api import coerce_map as _coerce_map
except Exception:  # noqa: BLE001 - allow running as a plain script
    import notify as _notify  # type: ignore
    from tui.api import coerce_map as _coerce_map  # type: ignore


def _bb_dir(blackboard_dir: str | None) -> str:
    return blackboard_dir or os.environ.get("SWARM_BLACKBOARD_DIR") or str(_WORKERS / "state" / "blackboard")


# ─────────────────────────────── data (read) ──────────────────────────────────
def list_runs(blackboard_dir: str | None = None) -> list[str]:
    """Run ids present on the board, newest first — from whichever backend the workers use (a local
    directory, or Postgres when ``SWARM_BLACKBOARD_PG_DSN`` is set, roadmap H6)."""
    from swarm_substrate import blackboard as bb
    if os.environ.get("SWARM_BLACKBOARD_PG_DSN"):
        return bb.list_runs()
    return bb.list_runs(backend=bb._LocalBackend(_bb_dir(blackboard_dir)))


def board_json(run: str, *, ceiling_usd: float | None = None, blackboard_dir: str | None = None) -> dict:
    """The full console projection for one run: the fold + the open exception queue + a cost-burn."""
    view = board_mod.project_board(run, blackboard_dir=blackboard_dir)
    board = team_board.fold_board(run)
    view["exceptions"] = team_board.open_exceptions(board.get("exceptions") or [])
    if run == retrospective.LEARNINGS_RUN:
        # The cross-run learning board: every retrospective's durable learnings (kind ``learning``,
        # not a team's ``lesson``), so selecting this run shows what the swarm has learned.
        learned = retrospective.everything()
        view["lessons"] = [{"text": retrospective.label(x), "confidence": x.get("confidence") or "medium",
                            "ts": x.get("ts"), "run": x.get("run"), "evidence": x.get("evidence") or ""}
                           for x in learned]
        view["rollup"]["lessons"] = len(learned)
    spend = float(view["spend"].get("usd", 0) or 0)
    view["burn"] = {"spentUsd": spend, "ceilingUsd": ceiling_usd,
                    "percent": (spend / ceiling_usd * 100) if ceiling_usd else None}
    # Teams of teams (H3): a program run lists its child team runs with their latest rollup, so a human
    # reads team-level status and drills into one team's board on demand — never the whole firehose.
    from swarm_substrate import blackboard as bb
    rollups = {}
    for f in bb.read_facts(run, kind="rollup"):
        rollups[str((f.get("data") or {}).get("childRun"))] = f.get("data") or {}
    # Constraints in force, with their id and authority derived from the author (H16) — what a human
    # lifts or replaces.
    waves = [f.get("ts") for f in bb.read_facts(run, kind="wave")]
    view["constraints"] = team_board.with_authority(
        team_board.active_directives(team_board.fold_board(run).get("directives") or [], wave_ts=waves))
    view["children"] = []
    for f in bb.read_facts(run, kind="child"):
        d = f.get("data") or {}
        child = str(d.get("childRun") or "")
        r = rollups.get(child) or {}
        live = team_board.board_rollup(team_board.fold_board(child)) if child else {}
        view["children"].append({"subsystem": d.get("subsystem"), "run": child, "kind": d.get("kind") or "team",
                                 "rollup": live, "verdict": r.get("verdict"), "delivered": r.get("delivered"),
                                 "openExceptions": live.get("openExceptions", 0)})
    return view


# ─────────────────────────────── Conductor (runs, launch) ─────────────────────
# The console lists, launches and drills into swarm runs. Their lifecycle (status, start/end, input,
# verdict) lives in Conductor; their working state lives on the boards. Both are read live — the
# console still keeps no state of its own.
SWARM_TYPES = ("cc_program", "team_build", "sdlc_intake")
LAUNCH_TYPES = ("cc_program", "sdlc_intake")
_TERMINAL = {"COMPLETED", "FAILED", "TERMINATED", "TIMED_OUT"}


def _conductor(method: str, path: str, body: dict | None = None, *, opener=None,
               server_url: str | None = None):
    """One Conductor REST call: (status, parsed body). A network failure is (0, {"error": ...})."""
    import urllib.error
    import urllib.request
    base = (server_url or os.environ.get("CONDUCTOR_SERVER_URL") or "http://localhost:8080/api").rstrip("/")
    token = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
    headers = {"Content-Type": "application/json", **({"X-Authorization": token} if token else {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(req) as resp:  # noqa: S310 - operator-configured URL
            raw = resp.read() if hasattr(resp, "read") else b""
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception as e:  # noqa: BLE001 - the console degrades to the boards alone
        return 0, {"error": f"Conductor unreachable at {base}: {e}"}
    text = (raw or b"").decode("utf-8", "replace").strip()
    try:
        return status, json.loads(text) if text else {}
    except ValueError:
        return status, text.strip('"')


def _title(wf_name: str, inp: dict) -> str:
    """What a human calls the run: the first line of what was asked for."""
    for key in ("description", "request", "instruction"):
        text = str(inp.get(key) or "").strip()
        if text:
            line = text.splitlines()[0].strip()
            return line if len(line) <= 120 else line[:117].rstrip() + "…"
    sub = str(inp.get("subsystemId") or "").strip()
    return f"{sub} team" if sub else wf_name


def _run_row(w: dict) -> dict:
    # The search index returns input/output as a Java Map toString(); /workflow/{id} returns JSON.
    inp, out = _coerce_map(w.get("input")), _coerce_map(w.get("output"))
    status = str(w.get("status") or "")
    spend = out.get("spendUsd")
    if spend is None and status not in _TERMINAL:
        try:
            from common import budget_gate
            spend = budget_gate.read_spend(str(w.get("workflowId")))["usd"]
        except Exception:  # noqa: BLE001
            spend = None
    repo = str(inp.get("repoPath") or "")
    return {
        "id": w.get("workflowId"), "workflow": w.get("workflowType") or w.get("workflowName"),
        "status": status, "startTime": w.get("startTime"), "endTime": w.get("endTime") or None,
        "title": _title(str(w.get("workflowType") or w.get("workflowName") or "run"), inp),
        "repo": repo, "repoName": os.path.basename(repo.rstrip("/")) if repo else "",
        "verdict": out.get("verdict") or "", "spendUsd": spend, "ceilingUsd": inp.get("ceilingUsd"),
        "parent": inp.get("parentCorrelationId") or w.get("parentWorkflowId") or "",
        "subsystem": inp.get("subsystemId") or "",
        "continues": str(w.get("correlationId") or "").removeprefix(CONTINUE_PREFIX)
                     if str(w.get("correlationId") or "").startswith(CONTINUE_PREFIX) else "",
    }


def list_swarm_runs(limit: int = 80, *, opener=None, server_url: str | None = None) -> dict:
    """Swarm runs, newest first, with what a list needs: status, title, repo, verdict, spend. Child
    teams of a program carry their parent so the UI nests them; ``error`` is set when Conductor is
    unreachable (the list then falls back to the run ids on the board)."""
    from urllib.parse import quote
    query = "workflowType IN (" + ",".join(SWARM_TYPES) + ")"
    status, body = _conductor(
        "GET", f"/workflow/search?start=0&size={int(limit)}&sort=startTime:DESC&freeText=*&query={quote(query)}",
        opener=opener, server_url=server_url)
    if status != 200 or not isinstance(body, dict):
        return {"items": [], "error": (body or {}).get("error") if isinstance(body, dict) else str(body)}
    return {"items": [_run_row(w) for w in body.get("results") or []], "error": ""}


def _ranks(wf: dict) -> list[list[dict]]:
    """A program's dependency ranks with each subsystem's planned items, from the run's own tasks."""
    tasks = wf.get("tasks") or []
    init = next((t for t in tasks if str(t.get("referenceTaskName") or "") == "rank_init"), None)
    od = (init or {}).get("outputData") or {}
    subs = {str(s.get("id")): s for s in od.get("subsystems") or [] if isinstance(s, dict)}
    # The breakdown re-slices subsystems after the design rank; its items are the ones built.
    for t in tasks:
        if str(t.get("referenceTaskName") or "").startswith("rank_breakdown"):
            for s in ((t.get("outputData") or {}).get("structured") or {}).get("subsystems") or []:
                if isinstance(s, dict) and str(s.get("id")) in subs:
                    subs[str(s.get("id"))] = {**subs[str(s.get("id"))], "items": s.get("items") or []}
    ranks = od.get("ranks") or (wf.get("output") or {}).get("ranks") or []
    return [[{"id": sid, "deliverable": (subs.get(sid) or {}).get("deliverable") or "code",
              "planned": len((subs.get(sid) or {}).get("items") or []) or 1} for sid in rank]
            for rank in ranks if isinstance(rank, list)]


def run_view(run: str, *, blackboard_dir: str | None = None, opener=None, server_url: str | None = None) -> dict:
    """Everything the console's run page shows, in one read: lifecycle from Conductor, the board, the
    live snapshot (phase, agents working now, recent events), and a program's rank strip."""
    import swarm_state
    status, wf = _conductor("GET", f"/workflow/{run}?includeTasks=true", opener=opener, server_url=server_url)
    wf = wf if status == 200 and isinstance(wf, dict) else {}
    inp = wf.get("input") or {}
    view = board_json(run, ceiling_usd=inp.get("ceilingUsd"), blackboard_dir=blackboard_dir)
    snap = swarm_state.snapshot(run, blackboard_dir=blackboard_dir, wf=wf, view=view)
    row = _run_row({**wf, "workflowId": run}) if wf else {"id": run, "title": run, "status": "unknown"}
    try:
        result = run_result({**wf, "workflowId": run}, opener=opener, server_url=server_url) if wf else None
    except Exception as e:  # noqa: BLE001 - a broken result card must not take the run page down
        result = {"error": str(e), "actions": {}}
    return {"run": row, "snapshot": snap, "board": view, "ranks": _ranks(wf) if wf else [], "result": result,
            "actions": lifecycle_actions(wf) if wf else {"retry": False, "startOver": False, "editAndRun": False},
            "workflowInput": inp if wf and lifecycle_actions(wf)["editAndRun"] else {},
            "parent": inp.get("parentCorrelationId") or "", "input": {k: inp.get(k) for k in (
                "repoPath", "description", "request", "ceilingUsd", "maxWidth", "subsystemId") if k in inp},
            "conductor": "" if wf else "Conductor did not return this run; showing the board only."}


def _required(spec, name: str) -> set[str]:
    """Fields a console launch must fill: the catalog's must-fill fields, plus — for an "at least one of"
    group where the form shows only one member (cc_program's description; its subsystems input is for
    nested programs) — that member."""
    import catalog
    names = {f.name for f in spec.fields}
    out = {f.name for f in spec.fields if f.must_fill}
    for group in catalog.AT_LEAST_ONE_OF_GROUPS.get(name, ()):
        shown = [g for g in group if g in names]
        if len(shown) == 1:
            out.add(shown[0])
    return out


def launchers() -> list[dict]:
    """The launch forms, generated from the same catalog the TUI uses (one definition of each form)."""
    import catalog
    out = []
    for name in LAUNCH_TYPES:
        spec = catalog.CATALOG[name]
        out.append({"workflow": name, "action": spec.action, "blurb": spec.blurb, "fields": [
            {"name": f.name, "label": f.label, "kind": f.kind, "default": f.form_default, "help": f.help,
             "choices": list(f.choices), "advanced": f.advanced, "required": f.name in _required(spec, name)}
            for f in spec.fields]})
    return out


def launch(payload: dict, *, author: str, opener=None, server_url: str | None = None) -> dict:
    """Start a swarm run from the launch form. Required fields are checked here; optional fields left at
    their default are not sent (the workflow applies its own). Never passes a version (one version)."""
    import catalog
    name = str(payload.get("workflow") or "")
    if name not in LAUNCH_TYPES:
        return {"ok": False, "error": f"cannot launch {name!r} from the console"}
    spec = catalog.CATALOG[name]
    values = catalog.normalize_local_paths(dict(payload.get("values") or {}))
    required = _required(spec, name)
    missing = [f.label for f in spec.fields if f.name in required and not str(values.get(f.name) or "").strip()]
    if missing:
        return {"ok": False, "error": "Fill in " + ", ".join(missing) + "."}
    prior = str(payload.get("continueFrom") or "").strip()
    asked = str(values.get(_TEXT_FIELD.get(name, "description")) or "")
    if prior:
        # A follow-up run builds on a finished run's branch (its repoPath is a checkout of that branch).
        try:
            values = prepare_continuation(prior, values, name, opener=opener, server_url=server_url)
        except Exception as e:  # noqa: BLE001 - tell the human exactly what stands in the way
            return {"ok": False, "error": str(e)}
    repo = str(values.get("repoPath") or "")
    if repo and not os.path.isdir(repo):
        return {"ok": False, "error": f"Repo path {repo} is not a directory on this machine."}
    body = spec.build_payload(values)
    # The same model-profile snapshot a TUI launch attaches (the policy edited under Settings), so a
    # run started from either UI uses the same models and records which policy chose them.
    model_profiles = _tui("model_profiles")
    try:
        body = model_profiles.apply_profile_snapshot(name, body)
    except model_profiles.ProfileError as e:
        return {"ok": False, "error": f"Fix the model settings first: {e}"}
    if name in _BRANCHED and not str(body.get("branchName") or "").strip():
        # The result branch reads as what was asked for: swarm/<words of the request>-<run id>.
        from common import git
        body["branchName"] = git.branch_slug(asked or _title(name, values))
    start = {"name": name, "input": body, **({"correlationId": CONTINUE_PREFIX + prior} if prior else {})}
    status, wid = _conductor("POST", "/workflow", start, opener=opener, server_url=server_url)
    if status != 200 or not isinstance(wid, str) or not wid:
        detail = wid.get("message") or wid.get("error") if isinstance(wid, dict) else wid
        return {"ok": False, "error": f"Conductor refused the start: {detail or status}"}
    return {"ok": True, "run": wid, "workflow": name, "author": author,
            **({"continues": prior, "repoPath": repo} if prior else {})}


RETRYABLE = {"FAILED", "TIMED_OUT", "TERMINATED"}


def lifecycle_actions(wf: dict) -> dict:
    """What can be done with a finished run, from its Conductor state (pure). ``retry`` resumes the same
    run from the step that failed — what completed is kept, and the board stays the run's own. ``startOver``
    starts a NEW run with the same input: Conductor's own restart reuses the run id, and the swarm's
    board is keyed by it, so the restarted run would read the old run's teams, done items and spend (spend
    counted twice against the ceiling). A child team of a program is driven by its program: neither
    applies to it alone."""
    status = str(wf.get("status") or "")
    inp = wf.get("input") or {}
    top = not inp.get("parentCorrelationId") and not wf.get("parentWorkflowId")
    name = str(wf.get("workflowName") or wf.get("workflowType") or "")
    finished = status in _TERMINAL
    return {"retry": top and status in RETRYABLE, "startOver": top and finished and name in SWARM_TYPES,
            "editAndRun": top and finished and name in LAUNCH_TYPES}


def retry_run(run: str, *, opener=None, server_url: str | None = None) -> dict:
    """Resume a failed, timed-out or stopped run from the step that ended it."""
    status, wf = _conductor("GET", f"/workflow/{run}?includeTasks=false", opener=opener, server_url=server_url)
    if status != 200 or not isinstance(wf, dict):
        return {"ok": False, "error": f"Conductor does not know run {run}."}
    if not lifecycle_actions(wf)["retry"]:
        return {"ok": False, "error": f"Only a failed, timed-out or stopped run can be retried (this one is "
                                      f"{str(wf.get('status') or 'unknown').lower()})."}
    # A failed child team is started afresh by its program's retry (its SUB_WORKFLOW task re-runs).
    status, body = _conductor("POST", f"/workflow/{run}/retry?resumeSubworkflowTasks=false",
                              opener=opener, server_url=server_url)
    if status not in (200, 204):
        detail = body.get("message") or body.get("error") if isinstance(body, dict) else body
        return {"ok": False, "error": f"Conductor refused the retry: {detail or status}"}
    return {"ok": True, "run": run}


def start_over(run: str, *, opener=None, server_url: str | None = None) -> dict:
    """Start a new run of the same workflow with the same input; the original stays as it was."""
    status, wf = _conductor("GET", f"/workflow/{run}?includeTasks=false", opener=opener, server_url=server_url)
    if status != 200 or not isinstance(wf, dict):
        return {"ok": False, "error": f"Conductor does not know run {run}."}
    if not lifecycle_actions(wf)["startOver"]:
        return {"ok": False, "error": "Only a finished top-level swarm run can be started over."}
    name = str(wf.get("workflowName") or wf.get("workflowType"))
    repo = str((wf.get("input") or {}).get("repoPath") or "")
    if repo and not os.path.isdir(repo):
        return {"ok": False, "error": f"Repo path {repo} is no longer a directory on this machine."}
    # A started-over follow-up run still continues the run it continued.
    cid = str(wf.get("correlationId") or "")
    start = {"name": name, "input": dict(wf.get("input") or {}),
             **({"correlationId": cid} if cid.startswith(CONTINUE_PREFIX) else {})}
    status, wid = _conductor("POST", "/workflow", start, opener=opener, server_url=server_url)
    if status != 200 or not isinstance(wid, str) or not wid:
        detail = wid.get("message") or wid.get("error") if isinstance(wid, dict) else wid
        return {"ok": False, "error": f"Conductor refused the start: {detail or status}"}
    return {"ok": True, "run": wid, "from": run, "workflow": name}


# ─────────────────────────────── a run's result: branch, continue, ship ───────
# Every swarm run delivers onto its own branch in the repository it was pointed at. The console shows
# that branch as the run's result and offers what comes next: continue the work with a follow-up run,
# open a pull request (a GitHub remote is configured), or merge it into the branch it started from.
CONTINUE_PREFIX = "continue:"
SHIP_PREFIX = "ship:"
_TEXT_FIELD = {"cc_program": "description", "sdlc_intake": "request"}
_BRANCHED = ("cc_program", "sdlc_intake", "team_build", "bug_fix")


def _handoff(wf: dict) -> dict:
    """Where a run's work landed (``sourceHandoff``): its own output, or — for the front door, which
    routes to a stage workflow — the output of the stage it ran."""
    found = (wf.get("output") or {}).get("sourceHandoff")
    if isinstance(found, dict) and found.get("branch"):
        return found
    for t in reversed(wf.get("tasks") or []):
        h = (t.get("outputData") or {}).get("sourceHandoff")
        if t.get("taskType") == "SUB_WORKFLOW" and isinstance(h, dict) and h.get("branch"):
            return h
    return {}


def _get_run(run: str, *, tasks: bool, opener=None, server_url: str | None = None) -> dict:
    status, wf = _conductor("GET", f"/workflow/{run}?includeTasks={'true' if tasks else 'false'}",
                            opener=opener, server_url=server_url)
    return wf if status == 200 and isinstance(wf, dict) else {}


def _lineage(wf: dict, *, opener=None, server_url: str | None = None) -> list[dict]:
    """The runs this one continues, nearest first: each follow-up run's correlation id names the run
    it continued (``continue:<run>``)."""
    out, seen, cur = [], set(), wf
    while len(out) < 20:
        prior = str(cur.get("correlationId") or "")
        if not prior.startswith(CONTINUE_PREFIX):
            break
        pid = prior[len(CONTINUE_PREFIX):]
        if pid in seen:
            break
        seen.add(pid)
        cur = _get_run(pid, tasks=True, opener=opener, server_url=server_url)
        if not cur:
            break
        out.append({"id": pid, "title": _title(str(cur.get("workflowName") or ""), cur.get("input") or {}),
                    "branch": _handoff(cur).get("branch") or "", "base": _handoff(cur).get("originalBranch") or "",
                    "status": cur.get("status")})
    return out


def _latest_ship(run: str, *, opener=None, server_url: str | None = None) -> dict:
    from urllib.parse import quote
    query = quote(f"workflowType IN (ship_run) AND correlationId = '{SHIP_PREFIX}{run}'")
    status, body = _conductor("GET", f"/workflow/search?start=0&size=1&sort=startTime:DESC&freeText=*&query={query}",
                              opener=opener, server_url=server_url)
    hits = (body or {}).get("results") or [] if status == 200 and isinstance(body, dict) else []
    hits = [h for h in hits if (h.get("workflowType") or h.get("workflowName")) == "ship_run"]
    if not hits:
        return {}
    h = hits[0]
    out = _coerce_map(h.get("output"))
    return {"run": h.get("workflowId"), "status": h.get("status"), "url": out.get("prUrl") or "",
            "number": out.get("prNumber") or 0, "state": out.get("publicationState") or "",
            "reason": out.get("reason") or h.get("reasonForIncompletion") or ""}


def run_result(wf: dict, *, opener=None, server_url: str | None = None) -> dict | None:
    """A finished run's result: the branch its work is on, what it adds over the branch the work
    started from, and which next steps apply. None while the run has not reported a branch."""
    from common import git
    h = _handoff(wf)
    branch, repo = str(h.get("branch") or ""), str(h.get("sourceRepoPath") or "")
    if not branch or not repo or not os.path.isdir(repo):
        return None
    status = str(wf.get("status") or "")
    lineage = _lineage(wf, opener=opener, server_url=server_url)
    # A follow-up run starts from the branch it continues; what it is merged into is the branch the
    # first run of the chain started from.
    base = str((lineage[-1]["base"] if lineage else "") or h.get("originalBranch") or "")
    in_place = branch == str(h.get("originalBranch") or "")
    try:
        summary = git.branch_summary(repo, branch, base if not in_place else "")
        remote = git.remote_info(repo)
    except Exception as e:  # noqa: BLE001 - the repository moved or is unreadable: show why
        return {"branch": branch, "repo": repo, "error": str(e), "actions": {}}
    finished = status in _TERMINAL
    exists = summary.get("exists", False)
    merged = bool(summary.get("merged"))
    has_work = exists and (in_place or summary.get("commits", 0) > 0)
    ship = _latest_ship(str(wf.get("workflowId") or ""), opener=opener, server_url=server_url) if finished else {}
    target = (remote or {}).get("defaultBranch") or base
    return {
        "branch": branch, "repo": repo, "base": base, "inPlace": in_place, "head": summary.get("head", ""),
        "commits": summary.get("commits", 0), "files": summary.get("files", 0),
        "insertions": summary.get("insertions", 0), "deletions": summary.get("deletions", 0),
        "exists": exists, "merged": merged, "remote": remote, "pullRequest": ship, "lineage": lineage,
        "prBase": target,
        "actions": {
            "continue": finished and exists and wf.get("workflowName") in LAUNCH_TYPES,
            "pullRequest": finished and has_work and not merged and bool((remote or {}).get("github"))
                           and str(ship.get("status") or "") not in ("RUNNING",) and not ship.get("url"),
            "merge": finished and has_work and not merged and not in_place and bool(base),
        },
    }


def _follow_up(text: str, prior: dict, branch: str) -> str:
    title = _title(str(prior.get("workflowName") or ""), prior.get("input") or {})
    return (f"Follow-up to an earlier swarm run (\"{title}\"). Its delivered work is already in this "
            f"repository on branch {branch}: build on it, keep what works, and do not rebuild what exists.\n\n"
            f"{text.strip()}")


def prepare_continuation(prior_run: str, values: dict, workflow: str, *, opener=None,
                         server_url: str | None = None) -> dict:
    """Point a launch at a finished run's work: a checkout of its branch becomes the new run's
    repository, and the request says what it builds on. Raises ValueError naming what is missing."""
    from common import git
    prior = _get_run(prior_run, tasks=True, opener=opener, server_url=server_url)
    if not prior:
        raise ValueError(f"Conductor does not know run {prior_run}.")
    if str(prior.get("status") or "") not in _TERMINAL:
        raise ValueError("That run is still working; continue it once it has finished.")
    h = _handoff(prior)
    branch, repo = str(h.get("branch") or ""), str(h.get("sourceRepoPath") or "")
    if not branch or not repo:
        raise ValueError(f"Run {prior_run} reported no result branch to continue from.")
    values = dict(values)
    values["repoPath"] = git.continuation_checkout(repo, branch)
    field = _TEXT_FIELD.get(workflow, "description")
    values[field] = _follow_up(str(values.get(field) or ""), prior, branch)
    return values


def ship_run(run: str, how: str, *, author: str, opener=None, server_url: str | None = None) -> dict:
    """Deliver a finished run's branch: ``pr`` starts ``ship_run`` (push + pull request); ``merge``
    merges it locally into the branch the work started from, only when that is safe."""
    from common import git
    wf = _get_run(run, tasks=True, opener=opener, server_url=server_url)
    if not wf:
        return {"ok": False, "error": f"Conductor does not know run {run}."}
    result = run_result({**wf, "workflowId": run}, opener=opener, server_url=server_url)
    if not result or result.get("error"):
        return {"ok": False, "error": (result or {}).get("error") or "This run has no result branch."}
    if how == "merge":
        if not result["actions"].get("merge"):
            return {"ok": False, "error": "This run's branch has nothing left to merge."}
        try:
            merged = git.merge_branch(result["repo"], result["branch"], result["base"])
        except Exception as e:  # noqa: BLE001 - say exactly why, and change nothing
            return {"ok": False, "error": str(e)}
        return {"ok": True, "how": "merge", "author": author, **merged}
    if how != "pr":
        return {"ok": False, "error": f"unknown way to ship: {how!r}"}
    if not result["actions"].get("pullRequest"):
        return {"ok": False, "error": "A pull request needs a GitHub remote and unmerged work on the branch."}
    title = _title(str(wf.get("workflowName") or ""), wf.get("input") or {})
    report = str((wf.get("output") or {}).get("report") or "").strip()
    summary = (report.split("\n\n")[0] if report else title)[:480]
    body = {"name": "ship_run", "correlationId": SHIP_PREFIX + run, "input": {
        "repoPath": result["repo"], "branch": result["branch"], "head": result["head"], "base": result["prBase"],
        "title": title, "summary": summary, "remote": (result["remote"] or {}).get("name") or "origin"}}
    status, wid = _conductor("POST", "/workflow", body, opener=opener, server_url=server_url)
    if status != 200 or not isinstance(wid, str) or not wid:
        detail = wid.get("message") or wid.get("error") if isinstance(wid, dict) else wid
        return {"ok": False, "error": f"Conductor refused the start: {detail or status}"}
    return {"ok": True, "how": "pr", "run": wid, "author": author}


# ─────────────────────────────── settings: model policy and prompt templates ────
# The same files the TUI's /models and /templates screens edit (~/.conductor-harness): one policy
# chooses the agent and model for every role of every run either UI starts; templates replace the
# built-in prompts of the review and coding workflows.
def _tui(name: str):
    """A TUI package module (they use package-relative imports, so never import them bare)."""
    import importlib
    root = str(_TUI.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(f"tui.{name}")


def settings_view() -> dict:
    model_profiles, templates = _tui("model_profiles"), _tui("templates")
    try:
        from common.model_policy import BACKENDS, ROLES
    except Exception:  # noqa: BLE001
        BACKENDS, ROLES = {"claude", "codex"}, {"design", "plan", "code", "review", "judge", "scribe"}
    models = model_profiles.user_policy()
    data = models.get("data") or {}
    known = set((data.get("prices") or {})) | {r["model"] for r in model_profiles.role_rows(data) if r["model"]}
    known |= set(model_profiles.DEFAULT_MODEL_PROFILES["prices"])
    return {
        "models": models, "roles": sorted(ROLES), "backends": sorted(BACKENDS), "knownModels": sorted(known),
        "templates": [{"path": str(e.path), "file": e.path.name, "name": e.name, "description": e.description,
                       "workflows": list(e.workflows), "repos": list(e.repos), "fields": list(e.fields),
                       "body": e.body} for e in templates.list_templates()],
        "templateTargets": templates.targets(), "templatesDir": str(templates.templates_dir()),
        "builtins": templates.builtins(),
    }


def default_prompt_for(workflow: str, field: str) -> str:
    templates = _tui("templates")
    return templates.default_prompt(templates.field_key(workflow, field) or templates.WORKFLOW_KEY.get(workflow)) or ""


def save_models(payload: dict) -> dict:
    model_profiles = _tui("model_profiles")
    try:
        saved = model_profiles.save_user_policy(payload.get("data"), expected_sha256=str(payload.get("sha256") or ""))
    except model_profiles.ProfileError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "models": saved}


def save_template(payload: dict) -> dict:
    templates = _tui("templates")
    action = str(payload.get("action") or "save")
    path = str(payload.get("path") or "").strip() or None
    try:
        if action == "delete":
            templates.delete(templates.entry_at(path or ""))
            return {"ok": True, "deleted": path}
        lst = lambda k: tuple(str(x).strip() for x in (payload.get(k) or []) if str(x).strip())  # noqa: E731
        entry = templates.update(path, name=str(payload.get("name") or ""), body=str(payload.get("body") or ""),
                                 description=str(payload.get("description") or ""), workflows=lst("workflows"),
                                 repos=lst("repos"), fields=lst("fields"))
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(entry.path), "name": entry.name}


# ─────────────────────────────── steering (write) ─────────────────────────────
def apply_steer(payload: dict, *, blackboard_dir: str | None = None, author: str = "human") -> dict:  # noqa: C901
    """Apply a human steering action from the console as an authored board write (§4.8). Humans are
    passive observers of the swarm — they watch, give feedback, and can terminate; nothing ever waits
    for their approval. Supported: ``message`` (plain language, routed for them), ``nudge`` (directive
    on an item), ``stop`` (blocking directive: interrupts the agent), ``reopen``, ``reprioritize``,
    ``lift`` / ``replace`` (a constraint), and ``terminate`` (the run)."""
    run = str(payload.get("run") or "").strip()
    action = str(payload.get("action") or "").strip()
    item = str(payload.get("item") or "").strip()
    text = str(payload.get("text") or "")
    if not run or not action:
        return {"ok": False, "error": "run and action are required"}
    extra: dict = {}
    if action == "message":
        # Plain-language steering: the human just says it; routing to item/team/action is worked out.
        import steer_message
        result = steer_message.send(run, text, author=author, blackboard_dir=blackboard_dir)
        return {**result, "action": action, "summary": steer_message.describe(result)}
    if action == "nudge":
        board_mod.nudge_item(run, item, text, author=author, blackboard_dir=blackboard_dir)
    elif action == "stop":
        # Blocking feedback (H11): the agent working the item is stopped at its next tool boundary and
        # the item is re-opened with this directive.
        board_mod.nudge_item(run, item, text or "stop", author=author, urgency="blocking",
                             blackboard_dir=blackboard_dir)
    elif action == "reopen":
        board_mod.reopen_item(run, item, author=author, blackboard_dir=blackboard_dir)
    elif action == "reprioritize":
        board_mod.reprioritize(run, item, float(payload.get("priority") or 0), author=author,
                               blackboard_dir=blackboard_dir)
    elif action == "lift":
        board_mod.lift_constraint(run, str(payload.get("directive") or ""), text, author=author,
                                  blackboard_dir=blackboard_dir)
    elif action == "replace":
        board_mod.replace_constraint(run, str(payload.get("directive") or ""), item, text, author=author,
                                     blackboard_dir=blackboard_dir)
    elif action == "terminate":
        extra = board_mod.terminate_run(run, text, author=author)
    else:
        return {"ok": False, "error": f"unknown action {action!r}"}
    return {"ok": True, "action": action, "item": item, "author": author, **extra}


# ─────────────────────────────── request logic (pure) ─────────────────────────
def _json(status: int, obj) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(obj, default=str).encode("utf-8")


def _actor(headers: dict | None) -> dict | None:
    auth = str((headers or {}).get("Authorization") or "")
    return actors.resolve(auth[7:] if auth.lower().startswith("bearer ") else "")


def handle_get(path: str, query: dict, blackboard_dir: str | None = None,
               headers: dict | None = None) -> tuple[int, str, bytes]:
    if path == "/" or path == "/index.html":
        return 200, "text/html; charset=utf-8", PAGE.encode("utf-8")
    if path == "/api/runs":
        return 200, "application/json", json.dumps({"runs": list_runs(blackboard_dir)}).encode("utf-8")
    if path == "/api/swarm-runs":
        listed = list_swarm_runs()
        if not listed["items"]:  # Conductor unreachable or empty: the board's run ids still show
            listed["items"] = [{"id": r, "title": r, "status": "unknown", "workflow": ""}
                               for r in list_runs(blackboard_dir) if r != retrospective.LEARNINGS_RUN]
        return _json(200, listed)
    if path == "/api/run":
        run = (query.get("run") or [""])[0]
        if not run:
            return _json(400, {"error": "run required"})
        return _json(200, run_view(run, blackboard_dir=blackboard_dir))
    if path == "/api/launchers":
        return _json(200, {"launchers": launchers()})
    if path == "/api/learnings":
        return _json(200, board_json(retrospective.LEARNINGS_RUN, blackboard_dir=blackboard_dir))
    if path == "/api/settings":
        return _json(200, settings_view())
    if path == "/api/template-default":
        return _json(200, {"body": default_prompt_for((query.get("workflow") or [""])[0], (query.get("field") or [""])[0])})
    if path == "/api/whoami":
        actor = _actor(headers)
        return _json(200 if actor else 401, actor or {"error": "unknown token"})
    if path == "/api/board":
        run = (query.get("run") or [""])[0]
        if not run:
            return 400, "application/json", b'{"error":"run required"}'
        ceiling = query.get("ceiling", [None])[0]
        view = board_json(run, ceiling_usd=float(ceiling) if ceiling else None, blackboard_dir=blackboard_dir)
        return 200, "application/json", json.dumps(view, default=str).encode("utf-8")
    if path == "/api/state":
        # State on demand for UIs, API clients and other agents (structured, no LLM).
        run = (query.get("run") or [""])[0]
        if not run:
            return 400, "application/json", b'{"error":"run required"}'
        import swarm_state
        return 200, "application/json", json.dumps(swarm_state.snapshot(run, blackboard_dir=blackboard_dir),
                                                   default=str).encode("utf-8")
    if path == "/api/trace":
        run = (query.get("run") or [""])[0]
        item = (query.get("item") or [""])[0]
        if not run or not item:
            return 400, "application/json", b'{"error":"run and item required"}'
        board_mod._use_board_dir(blackboard_dir)
        import board_trace as trace_mod
        return 200, "application/json", json.dumps(
            {"run": run, "item": item, "timeline": trace_mod.item_timeline(run, item)}, default=str).encode("utf-8")
    return 404, "application/json", b'{"error":"not found"}'


def handle_post(path: str, body: bytes, blackboard_dir: str | None = None,
                headers: dict | None = None) -> tuple[int, str, bytes]:
    if path not in ("/api/steer", "/api/ask", "/api/launch", "/api/retry", "/api/start-over", "/api/ship",
                    "/api/models", "/api/templates"):
        return 404, "application/json", b'{"error":"not found"}'
    # Every steering write is authenticated (H16, §7 #7): the bearer token must belong to a registered
    # actor, and the board write is authored as that actor — never as a caller-supplied name.
    actor = _actor(headers)
    if actor is None:
        return 401, "application/json", b'{"error":"steering requires a registered actor token (Authorization: Bearer <token>)"}'
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return 400, "application/json", b'{"error":"invalid json"}'
    if path == "/api/launch":
        result = launch(payload, author=actor["author"])
        return _json(200 if result.get("ok") else 400, result)
    if path in ("/api/retry", "/api/start-over"):
        run = str(payload.get("run") or "").strip()
        if not run:
            return _json(400, {"error": "run required"})
        result = retry_run(run) if path == "/api/retry" else start_over(run)
        return _json(200 if result.get("ok") else 400, {**result, "author": actor["author"]})
    if path in ("/api/models", "/api/templates"):
        result = save_models(payload) if path == "/api/models" else save_template(payload)
        return _json(200 if result.get("ok") else 400, {**result, "author": actor["author"]})
    if path == "/api/ship":
        run, how = str(payload.get("run") or "").strip(), str(payload.get("how") or "").strip()
        if not run or not how:
            return _json(400, {"error": "run and how are required"})
        result = ship_run(run, how, author=actor["author"])
        return _json(200 if result.get("ok") else 400, result)
    if path == "/api/ask":
        # A plain-language question about the current state, answered from the live snapshot.
        run, question = str(payload.get("run") or "").strip(), str(payload.get("question") or "").strip()
        if not run or not question:
            return 400, "application/json", b'{"error":"run and question are required"}'
        import swarm_state
        result = swarm_state.ask(run, question, blackboard_dir=blackboard_dir)
        return (200 if result.get("ok") else 502), "application/json", json.dumps(result).encode("utf-8")
    result = apply_steer(payload, blackboard_dir=blackboard_dir, author=actor["author"])
    return (200 if result.get("ok") else 400), "application/json", json.dumps(result).encode("utf-8")


# ─────────────────────────────── HTTP transport ───────────────────────────────
def make_handler(blackboard_dir: str | None = None):
    class ConsoleHandler(BaseHTTPRequestHandler):
        def _send(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            self._send(*handle_get(parsed.path, parse_qs(parsed.query), blackboard_dir,
                                   headers={"Authorization": self.headers.get("Authorization") or ""}))

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            self._send(*handle_post(urlparse(self.path).path, body, blackboard_dir,
                                    headers={"Authorization": self.headers.get("Authorization") or ""}))

        def log_message(self, *a):  # quiet
            pass

    return ConsoleHandler


def serve(port: int = 8750, host: str = "127.0.0.1", blackboard_dir: str | None = None) -> None:  # pragma: no cover
    httpd = ThreadingHTTPServer((host, port), make_handler(blackboard_dir))
    print(f"[swarm-console] http://{host}:{port}  (blackboard {_bb_dir(blackboard_dir)})")
    httpd.serve_forever()


def _main(argv=None) -> None:  # pragma: no cover - CLI entry
    import argparse
    ap = argparse.ArgumentParser(description="Web Swarm Console — board + exception queue over the blackboard")
    ap.add_argument("--port", type=int, default=8750)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--blackboard-dir", default=None)
    ap.add_argument("--add-actor", metavar="EMAIL", help="register a person who may launch and steer runs, "
                    "print their sign-in token once, and exit")
    ap.add_argument("--name", default="", help="display name for --add-actor")
    args = ap.parse_args(argv)
    if args.add_actor:
        token = actors.register(args.add_actor, args.name)
        print(f"Registered {args.add_actor}. Paste this token into the console's Sign in dialog "
              f"(it is shown only now):\n\n  {token}\n")
        return
    serve(port=args.port, host=args.host, blackboard_dir=args.blackboard_dir)


# The page is its own file (HTML, CSS and JS in one, no build step, no framework): a view over the
# JSON endpoints above. Read at import so a running console serves what it was started with.
PAGE = (_TUI / "console.html").read_text(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    _main()
