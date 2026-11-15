"""Permission requests: what the agents need and are not allowed (docs/PERMISSIONS.md).

An agent refused something it needs (a sandbox write, a network host, a decision outside its authority)
reports it; the round review (the architect) passes on the requests it judges necessary. Each request is
decided, in order, by:

1. this run's earlier decision on the same request (a request is never asked twice);
2. a standing grant for the repository (an approver granted it "for this repository" on an earlier run);
3. the policy: the repository's own files are writable (not ``.git``, which the harness commits for the
   agents, nor an agent's or sandbox's own configuration); a package registry is reachable;
   nothing outside the repository is ever written;
4. an approver: a person or another agent, through the console API. The run waits for the decision.

Decisions are board facts, so the console shows them and the next round's sessions get what was granted.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from . import blackboard as bb

REQUEST_KIND, DECISION_KIND = "permission_request", "permission_decision"
KINDS = ("write", "network", "decision")
# Files whose writing changes what an agent or its sandbox may do: always an approver's call.
AGENT_CONFIG = (".codex/config.toml", ".claude/settings.json", ".claude/settings.local.json",
                ".conductor-code/permissions.json")

SCHEMA = {"type": "array", "items": {"type": "object", "properties": {
    "kind": {"type": "string", "enum": list(KINDS)},
    "target": {"type": "string"},
    "why": {"type": "string"}}, "required": ["kind", "target", "why"]}}


def _norm(kind: str, target: str) -> tuple[str, str]:
    kind = str(kind or "decision").strip().lower()
    kind = kind if kind in KINDS else "decision"
    target = str(target or "").strip()
    if kind == "write":
        target = target.removeprefix("./")
    if kind == "network":
        target = target.lower().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
    return kind, target


def request_id(kind: str, target: str) -> str:
    kind, target = _norm(kind, target)
    return hashlib.sha1(f"{kind}\0{target}".encode()).hexdigest()[:12]


# ── standing grants, per repository ───────────────────────────────────────────────────────────────────────
def _standing_path(repo_key: str) -> str:
    home = os.path.expanduser(os.environ.get("CONDUCTOR_HARNESS_HOME", "~/.conductor-harness"))
    name = "".join(c if c.isalnum() or c in "-_." else "_" for c in repo_key) or "repository"
    return os.path.join(home, "permissions", f"{name}.json")


def standing(repo_key: str) -> list[dict]:
    try:
        with open(_standing_path(repo_key), encoding="utf-8") as fh:
            return list(json.load(fh).get("grants") or [])
    except (OSError, ValueError):
        return []


def _remember(repo_key: str, grant: dict) -> None:
    path = _standing_path(repo_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    grants = [g for g in standing(repo_key) if g.get("id") != grant["id"]] + [grant]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"repository": repo_key, "grants": grants}, fh, indent=1)
    os.replace(tmp, path)


# ── the policy ────────────────────────────────────────────────────────────────────────────────────────────
def policy(kind: str, target: str, workdir: str) -> tuple[str, str] | None:
    """``("grant" | "deny", reason)`` when the policy decides; None when an approver must."""
    kind, target = _norm(kind, target)
    if kind == "write":
        root = os.path.realpath(workdir)
        path = os.path.realpath(os.path.join(root, target))
        rel = os.path.relpath(path, root)
        if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
            return "deny", "outside the repository: agents never write there"
        if rel == ".git" or rel.startswith(".git" + os.sep):
            return "deny", ".git is written by the harness, which commits the tree for the agents"
        if rel.replace(os.sep, "/") in AGENT_CONFIG:
            return None
        return "grant", "the repository's own file"
    if kind == "network":
        from .allowed_domains import DEFAULT_ALLOWED_DOMAINS
        if target in DEFAULT_ALLOWED_DOMAINS:
            return "grant", "a package registry or source host the harness allows"
        return None
    return None


# ── requests and decisions on the board ───────────────────────────────────────────────────────────────────
def decisions(run: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in bb.read_facts(run, kind=DECISION_KIND):
        d = f.get("data") or {}
        if d.get("id"):
            out[str(d["id"])] = d
    return out


def requests(run: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in bb.read_facts(run, kind=REQUEST_KIND):
        d = f.get("data") or {}
        if d.get("id"):
            out[str(d["id"])] = d
    return out


def pending(run: str) -> list[dict]:
    done = decisions(run)
    return [r for rid, r in requests(run).items() if rid not in done]


def _decision(run: str, req: dict, decision: str, *, by: str, note: str = "", scope: str = "run") -> dict:
    d = {"id": req["id"], "kind": req["kind"], "target": req["target"], "decision": decision, "scope": scope,
         "note": note, "by": by, "ts": time.time()}
    bb.post_fact(run, by, DECISION_KIND, d)
    return d


def submit(run: str, rnd: int, asked: list, *, workdir: str, repo_key: str, by: str = "architect") -> dict:
    """Record the round's requests and decide what the run's history, the repository's standing grants
    and the policy can. ``{"decided": [...], "pending": [...]}``."""
    done, known = decisions(run), requests(run)
    held = {g.get("id"): g for g in standing(repo_key)}
    decided, waiting = [], []
    for raw in asked or []:
        if not isinstance(raw, dict):
            continue
        kind, target = _norm(raw.get("kind"), raw.get("target"))
        if not target:
            continue
        rid = request_id(kind, target)
        if rid in done:
            continue  # already decided on this run: never asked twice
        req = {"id": rid, "kind": kind, "target": target, "why": str(raw.get("why") or ""), "round": rnd,
               "by": by, "repo": repo_key, "ts": time.time()}
        if rid not in known:
            bb.post_fact(run, by, REQUEST_KIND, req)
            known[rid] = req
        if rid in held:
            decided.append(_decision(run, req, "grant", by="policy:repository", scope="repository",
                                     note=f"granted for this repository by {held[rid].get('by')}"))
            continue
        ruled = policy(kind, target, workdir)
        if ruled:
            decided.append(_decision(run, req, ruled[0], by="policy", note=ruled[1]))
            continue
        waiting.append(req)
    return {"decided": decided, "pending": waiting}


def decide(run: str, rid: str, decision: str, *, by: str, note: str = "", scope: str = "run",
           repo_key: str = "") -> dict:
    """An approver's answer (a person or an agent, through the console API)."""
    decision = str(decision or "").strip().lower()
    if decision not in ("grant", "deny"):
        raise ValueError("decision must be grant or deny")
    scope = "repository" if str(scope or "").strip().lower() in ("repo", "repository") else "run"
    req = requests(run).get(rid)
    if not req:
        raise KeyError(f"no permission request {rid} on run {run}")
    if rid in decisions(run):
        raise ValueError(f"request {rid} is already decided")
    repo_key = repo_key or str(req.get("repo") or "")
    if scope == "repository" and decision == "grant":
        if not repo_key:
            raise ValueError("a grant for the repository needs the run's repository")
        _remember(repo_key, {"id": rid, "kind": req["kind"], "target": req["target"], "by": by, "note": note,
                             "ts": time.time()})
    return _decision(run, req, decision, by=by, note=note, scope=scope)


def grants(run: str) -> dict:
    """What the run's sessions are given: writable paths, reachable hosts, and every decision in words."""
    roots, hosts, lines = [], [], []
    for d in decisions(run).values():
        if d.get("decision") == "grant" and d.get("kind") == "write":
            roots.append(d["target"])
        if d.get("decision") == "grant" and d.get("kind") == "network":
            hosts.append(d["target"])
        lines.append(f"- {d.get('decision', '?').upper()} {d.get('kind')} `{d.get('target')}` (by {d.get('by')}"
                     + (f": {d['note']}" if d.get("note") else "") + ")")
    return {"writableRoots": sorted(set(roots)), "allowedDomains": sorted(set(hosts)),
            "text": "\n".join(lines) or "(none)"}
