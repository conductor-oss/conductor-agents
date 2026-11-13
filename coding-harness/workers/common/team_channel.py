"""The team board, live, from inside a running session: both directions.

A swarm collaborates while it works, not at phase boundaries. Until now a session could only READ the
board (directives injected after each tool call) and could only speak at its end (lessons in its final
report). Now every session has ``.cc-cache/bin/board``: ``board lesson "<text>"`` shares what it just
learned, ``board item <id> --files a,b "<description>"`` posts a piece of work a teammate can start on
now (the next standup dispatches it like any other item), ``board ask "<question>"`` asks the team, and
``board show`` prints the board. Requests are files under ``.cc-cache/board/`` (the one place the
sandbox lets a session write); the harness drains them at every tool boundary (Claude) or every few
seconds (Codex) and posts the facts. New lessons, items and questions from teammates are injected into
every running peer the same way directives are, so a lesson posted at minute two is read by the
teammate at minute two, not at the next dispatch.
"""
from __future__ import annotations

import json
import os
import stat
import time

CLIENT_REL = os.path.join(".cc-cache", "bin", "board")
NPM_SHIM_REL = os.path.join(".cc-cache", "bin", "npm")
_NPM_SHIM = """#!/usr/bin/env python3
\"\"\"npm, one install at a time per tree. Teammates share this working tree; three concurrent `npm install`
runs left node_modules without jsdom (ENOTEMPTY) and every test failed before collection (run c6c1e7a7).
A mutating subcommand waits for the tree's install lock; everything else passes straight through.\"\"\"
import fcntl, os, sys
here = os.path.dirname(os.path.abspath(__file__))
real = ""
for d in os.environ.get("PATH", "").split(os.pathsep):
    p = os.path.join(d, "npm")
    if d and os.path.abspath(d) != here and os.path.isfile(p) and os.access(p, os.X_OK):
        real = p
        break
if not real:
    sys.stderr.write("npm: not found on PATH\\n")
    sys.exit(127)
sub = next((a for a in sys.argv[1:] if not a.startswith("-")), "")
if sub in {"install", "i", "add", "ci", "install-clean", "clean-install", "isntall", "uninstall", "remove", "rm",
           "un", "unlink", "update", "up", "upgrade", "dedupe", "ddp", "prune", "link", "ln", "rebuild", "rb"}:
    lock = open(os.path.join(here, "..", "npm.lock"), "a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
os.execv(real, [real] + sys.argv[1:])
"""
QUEUE_REL = os.path.join(".cc-cache", "board")

_CLIENT = """#!/usr/bin/env python3
\"\"\"board show | board lesson "<text>" | board ask "<question>" | board decide "<the conflict and the options>" | board item <id> --files a,b [--criteria "..."] "<description>"
ONE board per run, shared by every team and agent. Posts land within seconds; everyone reads them live.
`decide` asks the Architect to break a tie: it answers with a binding decision on the board.\"\"\"
import json, os, sys, time, uuid
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.abspath(os.path.join(here, "..", ".."))
queue = os.path.join(root, ".cc-cache", "board")
os.makedirs(os.path.join(queue, "requests"), exist_ok=True)
args = sys.argv[1:]
if not args or args[0] in ("show", "-h", "--help"):
    snap = os.path.join(queue, "board.json")
    if not os.path.isfile(snap):
        print("board: no snapshot yet (it appears after your first tool call)"); sys.exit(0)
    d = json.load(open(snap))
    print("ITEMS:")
    for it in d.get("items", []):
        print(f"  - {it['id']} [{it['state']}] {it.get('title','')[:90]}  files={','.join(it.get('files', [])[:6])}")
    print("RECENT LESSONS:")
    for l in d.get("lessons", [])[-10:]:
        print(f"  - ({l.get('by','')}) {l.get('text','')[:160]}")
    print("OPEN QUESTIONS / DIRECTIVES:")
    for q in d.get("directives", [])[-10:]:
        print(f"  - ({q.get('by','')}) {q.get('text','')[:160]}")
    sys.exit(0)
kind = args[0]
req = {"id": uuid.uuid4().hex[:12], "kind": kind, "ts": time.time()}
if kind in ("lesson", "ask", "decide"):
    req["text"] = " ".join(args[1:]).strip()
    if not req["text"]:
        print(f"board {kind}: text required"); sys.exit(2)
elif kind == "item":
    rest = args[1:]
    if not rest:
        print("board item: id required"); sys.exit(2)
    req["item"] = rest[0]; rest = rest[1:]
    files, criteria, desc = [], [], []
    i = 0
    while i < len(rest):
        if rest[i] == "--files" and i + 1 < len(rest):
            files += [f.strip() for f in rest[i + 1].split(",") if f.strip()]; i += 2
        elif rest[i] == "--criteria" and i + 1 < len(rest):
            criteria.append(rest[i + 1]); i += 2
        else:
            desc.append(rest[i]); i += 1
    req.update({"files": files, "criteria": criteria, "description": " ".join(desc).strip()})
    if not files or not req["description"]:
        print("board item: --files a,b and a description are required (files must not overlap another item's)"); sys.exit(2)
else:
    print(f"board: unknown command {kind!r}"); sys.exit(2)
path = os.path.join(queue, "requests", req["id"] + ".json")
with open(path + ".tmp", "w") as fh:
    json.dump(req, fh)
os.replace(path + ".tmp", path)
print(f"board: {kind} queued ({req['id']}); the harness posts it at your next tool boundary")
"""


def install_client(worktree: str) -> str:
    path = os.path.join(worktree, CLIENT_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    current = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
    if current != _CLIENT:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_CLIENT)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shim = os.path.join(worktree, NPM_SHIM_REL)
    try:
        current = open(shim, encoding="utf-8").read()
    except OSError:
        current = ""
    if current != _NPM_SHIM:
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write(_NPM_SHIM)
    os.chmod(shim, os.stat(shim).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.makedirs(os.path.join(worktree, QUEUE_REL, "requests"), exist_ok=True)
    return path


def drain(worktree: str, cid: str, *, author: str, item: str, run_cid: str = "") -> list[dict]:
    """Post every queued request of this session: lessons, questions and decision requests to the RUN
    board (one board per run, every team and agent), split-off items to the team's item board plus a
    notice on the run board. Returns what was posted (a ``decide`` entry carries the request)."""
    if not worktree or not cid:
        return []
    run = str(run_cid or "") or cid
    from common import blackboard as bb
    from common import team_board
    req_dir = os.path.join(worktree, QUEUE_REL, "requests")
    try:
        names = sorted(n for n in os.listdir(req_dir) if n.endswith(".json"))
    except OSError:
        return []
    posted = []
    for name in names:
        path = os.path.join(req_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                req = json.load(fh)
        except (OSError, ValueError):
            continue
        os.replace(path, path + ".taken")
        kind = str(req.get("kind") or "")
        try:
            if kind == "lesson":
                team_board.post_lesson(run, author, str(req.get("text") or ""), confidence="medium")
            elif kind == "ask":
                bb.post_fact(run, author, "directive", {"target": "run", "by": author, "urgency": "advisory",
                                                        "text": f"QUESTION from {item}: {req.get('text')}"})
            elif kind == "decide":
                rid = bb.post_fact(run, author, "decision_request", {"question": str(req.get("text") or ""),
                                                                      "by": author, "item": item})
                bb.post_fact(run, author, "directive", {"target": "run", "by": author, "urgency": "advisory",
                                                        "text": f"{item} asked the ARCHITECT to decide: {req.get('text')}"})
                req = {**req, "requestId": rid, "run": run, "team": cid, "item": item, "author": author}
            elif kind == "item":
                team_board.post_item(cid, author, {
                    "id": str(req.get("item") or "").strip(), "description": str(req.get("description") or ""),
                    "kind": "feature", "files": req.get("files") or [], "acceptanceCriteria": req.get("criteria") or [],
                    "dependsOn": []})
                bb.post_fact(run, author, "directive", {"target": "run", "by": author, "urgency": "advisory",
                                                        "text": f"{item} split off a new item '{req.get('item')}' "
                                                                f"(files {', '.join(req.get('files') or [])}): "
                                                                f"{str(req.get('description') or '')[:200]}"})
            else:
                continue
            posted.append({"kind": kind, **{k: req.get(k) for k in ("text", "item", "files", "requestId", "run", "team", "author") if k in req}})
        except Exception:  # noqa: BLE001 - a bad request never costs the session
            continue
    return posted


def snapshot(worktree: str, cid: str, run_cid: str = "") -> None:
    """``.cc-cache/board/board.json``: what ``board show`` prints — the team's items, the RUN board's
    lessons and discussion (every team, every agent)."""
    if not worktree or not cid:
        return
    run = str(run_cid or "") or cid
    try:
        from common import team_board
        board = team_board.fold_board(cid)
        rb = team_board.fold_board(run) if run != cid else board
        items = [{"id": k, "state": v.get("state"), "title": v.get("title") or v.get("description") or "",
                  "files": v.get("files") or []} for k, v in sorted(board.get("items", {}).items())]
        lessons = [{"by": l.get("author") or l.get("by") or "", "text": l.get("text") or ""} for l in (rb.get("lessons", []) + ([] if rb is board else board.get("lessons", [])))[-20:]]
        directives = [{"by": d.get("by") or "", "text": d.get("text") or ""} for d in (rb.get("directives", []) + ([] if rb is board else board.get("directives", [])))[-20:]]
        out = os.path.join(worktree, QUEUE_REL, "board.json")
        with open(out + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"items": items, "lessons": lessons, "directives": directives, "ts": time.time()}, fh, default=str)
        os.replace(out + ".tmp", out)
    except Exception:  # noqa: BLE001
        return


def peer_updates(cid: str, item: str, *, since_ts: float, seen: set) -> list[str]:
    """New lessons from OTHER agents since ``since_ts`` not yet delivered (``seen`` holds fact keys)."""
    if not cid:
        return []
    from common import blackboard as bb
    out = []
    mine = {f"implementer:{item}", str(item), f"agent:{item}"}
    for fact in bb.read_facts(cid, kind="lesson", since=since_ts):
        key = f"{fact.get('author')}@{fact.get('ts')}"
        author = str(fact.get("author") or "")
        d = fact.get("data") or {}
        if key in seen or author in mine or not str(d.get("text") or "").strip():
            continue
        seen.add(key)
        out.append(f"- (lesson from {author}) {str(d.get('text'))[:300]}")
    return out
