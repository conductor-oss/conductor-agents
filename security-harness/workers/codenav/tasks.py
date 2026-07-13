"""Read-only code-navigation tools for the source-only deepening agent.

These back the `./sast --deep` verification loop: an LLM agent investigates ONE
candidate SAST finding by reading and searching the source, then decides whether
it is reachable from untrusted input (real) or dead/test/sanitized (a false
positive). Everything here is READ-ONLY and path-jailed to ``source_path`` — no
writes, no execution, no network. That is why source-only deepening needs no
authorization gate.

Tools:
  codenav_read  – return a file (or a line range), relative to source_path.
  codenav_grep  – regex/substring search across the tree -> file:line matches.
  codenav_list  – list a directory's entries.
"""

import logging
import os
import re

from conductor.client.worker.worker_task import worker_task

log = logging.getLogger(__name__)

SKIP_DIRS = {".git", "node_modules", "dist", "build", "vendor", "venv", ".venv",
             "__pycache__", "target", ".next", "out", "coverage"}
MAX_FILE_BYTES = 400_000
MAX_READ_LINES = 400
MAX_GREP_MATCHES = 60
MAX_LIST_ENTRIES = 300


def _root(inp):
    p = str(inp.get("source_path") or "").strip()
    return os.path.realpath(p) if p and os.path.isdir(p) else ""


def _safe(root, rel):
    """Resolve ``rel`` under ``root`` and refuse anything that escapes the jail."""
    if not root:
        return None
    target = os.path.realpath(os.path.join(root, (rel or "").lstrip("/")))
    if target == root or target.startswith(root + os.sep):
        return target
    return None


def _skip(path, root):
    parts = os.path.relpath(path, root).split(os.sep)
    return any(p in SKIP_DIRS for p in parts)


@worker_task(task_definition_name="codenav_read", thread_count=4)
def codenav_read(task):
    inp = task.input_data or {}
    root = _root(inp)
    rel = str(inp.get("path") or "")
    target = _safe(root, rel)
    if not target:
        return {"ok": False, "error": f"path not allowed or outside source: {rel!r}"}
    if not os.path.isfile(target):
        return {"ok": False, "error": f"not a file: {rel!r}"}
    try:
        if os.path.getsize(target) > MAX_FILE_BYTES:
            return {"ok": False, "error": "file too large to read"}
        with open(target, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        return {"ok": False, "error": str(e)}

    total = len(lines)
    try:
        start = max(1, int(inp.get("start_line") or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        end = int(inp.get("end_line")) if inp.get("end_line") else start + MAX_READ_LINES - 1
    except (TypeError, ValueError):
        end = start + MAX_READ_LINES - 1
    end = min(end, start + MAX_READ_LINES - 1, total)
    chunk = lines[start - 1:end]
    numbered = "\n".join(f"{start + i}: {ln}" for i, ln in enumerate(chunk))
    return {
        "ok": True,
        "path": os.path.relpath(target, root),
        "total_lines": total,
        "start_line": start,
        "end_line": end,
        "truncated": end < total,
        "content": numbered[:12000],
    }


@worker_task(task_definition_name="codenav_grep", thread_count=4)
def codenav_grep(task):
    inp = task.input_data or {}
    root = _root(inp)
    if not root:
        return {"ok": False, "error": "no source_path"}
    pattern = str(inp.get("pattern") or "")
    if not pattern:
        return {"ok": False, "error": "no pattern"}
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    glob = str(inp.get("glob") or "").strip()  # optional suffix filter, e.g. ".py"
    sub = _safe(root, str(inp.get("path") or "")) or root
    matches, scanned = [], 0
    for dirpath, dirnames, filenames in os.walk(sub):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if glob and not fn.endswith(glob):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > MAX_FILE_BYTES:
                    continue
                scanned += 1
                with open(fp, "r", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            matches.append({"file": os.path.relpath(fp, root),
                                            "line": i, "text": line.rstrip()[:240]})
                            if len(matches) >= MAX_GREP_MATCHES:
                                return {"ok": True, "pattern": pattern, "matches": matches,
                                        "truncated": True, "files_scanned": scanned}
            except OSError:
                continue
    return {"ok": True, "pattern": pattern, "matches": matches,
            "truncated": False, "files_scanned": scanned}


@worker_task(task_definition_name="codenav_list", thread_count=4)
def codenav_list(task):
    inp = task.input_data or {}
    root = _root(inp)
    target = _safe(root, str(inp.get("path") or "")) if root else None
    if not target or not os.path.isdir(target):
        return {"ok": False, "error": "directory not allowed or not found"}
    entries = []
    try:
        for name in sorted(os.listdir(target)):
            if name in SKIP_DIRS:
                continue
            full = os.path.join(target, name)
            entries.append({"name": name, "dir": os.path.isdir(full)})
            if len(entries) >= MAX_LIST_ENTRIES:
                break
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": os.path.relpath(target, root) or ".", "entries": entries}


# ─────────────────────────────────────────────────────────────────────────────
# Hunt-lead builder: turn the surface (routes), dependency CVE leads, and the
# applicable objective catalog into focused "leads", one per hunter agent. Pure
# shaping (no code reading) — but co-located here because it's read-only and
# needs common.cve_tradecraft (Python, not reachable from jq).
# ─────────────────────────────────────────────────────────────────────────────

# The objective families a source hunter can most plausibly reason about statically.
PRIORITY_OBJECTIVES = [
    "INFRA-RCE-INJECTION", "INFRA-SSRF", "INFRA-SUPPLY-CHAIN",
    "AUTHZ-NEGATIVE-SPACE", "AUTHZ-FUNCTION-LEVEL",
    "CONF-BOLA-CROSS-USER", "CONF-SINK-LEAK", "INTEG-PRIVESC-MASSASSIGN",
]
MAX_ENTRYPOINT_LEADS = 6
MAX_CVE_LEADS = 4
MAX_OBJECTIVE_LEADS = 5


def _route_path(r):
    """The path of a route, whether it came as {path} (source) or {url} (live surface)."""
    p = r.get("path") or r.get("url") or r.get("route") or ""
    if "://" in p:  # strip scheme://host, keep the path
        rest = p.split("://", 1)[1]
        p = "/" + "/".join(rest.split("/")[1:])
    return p


def _cluster_key(r):
    """Group key for entry-point clustering: the source file if known, else a URL path prefix."""
    f = r.get("file")
    if f:
        return f
    segs = [s for s in _route_path(r).split("/") if s and not s.startswith(":") and "{" not in s]
    return "/" + "/".join(segs[:2]) if segs else "/"


@worker_task(task_definition_name="build_hunt_leads", thread_count=2)
def build_hunt_leads(task):
    """Assemble focused hunt leads for the source-code hunter fan-out.

    Returns {leads: [...]} where each lead is one of:
      entrypoint_cluster { routes[], area_hint }   — trace handlers to sinks
      dependency_cve     { dependency, version, top_cves[], tradecraft_hint, kev }
      objective_sweep    { objective_id, class, objective, how_to_test }
    """
    from collections import defaultdict

    inp = task.input_data or {}
    # Coerce defensively: routes may arrive as a source-extracted list ({path,method,file})
    # or a live-surface endpoint list ({url,method}); anything else -> empty (never iterate a scalar).
    routes = inp.get("routes") if isinstance(inp.get("routes"), list) else []
    cve_leads = inp.get("cve_leads") if isinstance(inp.get("cve_leads"), list) else []
    objectives = inp.get("catalog_objectives") if isinstance(inp.get("catalog_objectives"), list) else []
    try:
        max_leads = int(inp.get("max_leads") or 15)
    except (TypeError, ValueError):
        max_leads = 15

    leads = []

    # 1) Entry-point clusters — group routes by defining file (source form) or, when the
    #    routes came from the live surface (no file), by URL path prefix.
    by_group = defaultdict(list)
    for r in routes:
        if not isinstance(r, dict):
            continue
        by_group[_cluster_key(r)].append({
            "method": r.get("method", ""),
            "path": _route_path(r),
            "file": r.get("file", ""),
        })
    for i, (gkey, rs) in enumerate(
        sorted(by_group.items(), key=lambda kv: -len(kv[1]))[:MAX_ENTRYPOINT_LEADS]
    ):
        leads.append({
            "lead_id": f"ep-{i}", "kind": "entrypoint_cluster",
            "area_hint": gkey, "routes": rs[:20],
        })

    # 2) Dependency-CVE leads — version-matched, KEV/priority first, tradecraft-enriched.
    try:
        from common import cve_tradecraft
    except Exception:
        cve_tradecraft = None
    dep_sorted = sorted(
        (c for c in cve_leads if isinstance(c, dict)),
        key=lambda c: (not c.get("kev"), -(c.get("priority_score") or 0)),
    )
    for i, c in enumerate(dep_sorted[:MAX_CVE_LEADS]):
        top = c.get("top_cves") or []
        cve0 = top[0] if top and isinstance(top[0], dict) else {}
        hint = ""
        if cve_tradecraft is not None:
            try:
                hint = cve_tradecraft.hint_line(
                    cve0.get("id", ""), c.get("dependency", ""), cve0.get("summary", ""))
            except Exception:
                hint = ""
        leads.append({
            "lead_id": f"cve-{i}", "kind": "dependency_cve",
            "dependency": c.get("dependency", ""),
            "version": c.get("version") or ("known" if c.get("version_known") else "unknown"),
            "top_cves": top[:3], "tradecraft_hint": hint,
            "priority_score": c.get("priority_score"), "kev": bool(c.get("kev")),
        })

    # 3) Objective sweeps — the priority families that are applicable to this target.
    obj_by_id = {o.get("id"): o for o in objectives if isinstance(o, dict)}
    picked = [oid for oid in PRIORITY_OBJECTIVES if oid in obj_by_id][:MAX_OBJECTIVE_LEADS]
    for i, oid in enumerate(picked):
        o = obj_by_id[oid]
        leads.append({
            "lead_id": f"obj-{i}", "kind": "objective_sweep",
            "objective_id": oid, "class": o.get("class"),
            "objective": o.get("objective"), "how_to_test": o.get("how_to_test"),
        })

    leads = leads[:max_leads]
    return {
        "leads": leads,
        "counts": {
            "entrypoint": sum(1 for x in leads if x["kind"] == "entrypoint_cluster"),
            "dependency_cve": sum(1 for x in leads if x["kind"] == "dependency_cve"),
            "objective_sweep": sum(1 for x in leads if x["kind"] == "objective_sweep"),
            "total": len(leads),
        },
    }
