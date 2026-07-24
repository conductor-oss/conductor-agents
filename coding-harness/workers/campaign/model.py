"""Pure campaign state helpers.

These functions intentionally know nothing about Conductor.  Keeping validation and
wave selection pure makes retries deterministic and lets the workflow persist the
entire state in task outputs instead of relying on worker-local memory.
"""

from __future__ import annotations

import os
import re
from collections import deque
from typing import Any

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ACTIONS = {"continue", "revise", "adopt_edits", "run_checks", "set_profiles", "stop"}


class CampaignValidationError(ValueError):
    pass


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise CampaignValidationError(f"{field} must be an array of non-empty strings")
    return [x.strip() for x in value]


def validate_plan(value: Any, *, max_tasks: int = 25) -> dict[str, Any]:
    """Validate and normalize the planner's DAG contract.

    Returns ``{valid, errors, tasks, order}``; validation failures are data, not
    exceptions, so a HUMAN checkpoint can request a revision without failing the run.
    """
    raw = value.get("tasks") if isinstance(value, dict) else value
    errors: list[str] = []
    if not isinstance(raw, list):
        return {"valid": False, "errors": ["plan must contain a tasks array"],
                "tasks": [], "order": []}
    if not raw:
        errors.append("plan must contain at least one task")
    if len(raw) > max_tasks:
        errors.append(f"plan has {len(raw)} tasks; maxTasks is {max_tasks}")

    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pos, item in enumerate(raw):
        prefix = f"tasks[{pos}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        ident = str(item.get("id") or "").strip()
        desc = str(item.get("description") or "").strip()
        if not _ID.match(ident):
            errors.append(f"{prefix}.id must be a unique lowercase slug")
        elif ident in seen:
            errors.append(f"duplicate task id: {ident}")
        seen.add(ident)
        if not desc:
            errors.append(f"{prefix}.description is required")
        try:
            deps = _strings(item.get("dependsOn"), f"{prefix}.dependsOn")
            files = _strings(item.get("files"), f"{prefix}.files")
            acceptance = _strings(item.get("acceptanceCriteria"), f"{prefix}.acceptanceCriteria")
            checks = _strings(item.get("checks"), f"{prefix}.checks")
        except CampaignValidationError as exc:
            errors.append(str(exc))
            deps, files, acceptance, checks = [], [], [], []
        if not files:
            errors.append(f"{prefix}.files must identify at least one write root")
        if not acceptance:
            errors.append(f"{prefix}.acceptanceCriteria must not be empty")
        unsafe = [p for p in files if os.path.isabs(p) or p == ".." or p.startswith("../")]
        if unsafe:
            errors.append(f"{prefix}.files escapes the repository: {', '.join(unsafe)}")
        tasks.append({"id": ident, "description": desc, "dependsOn": deps,
                      "files": files, "acceptanceCriteria": acceptance, "checks": checks})

    ids = {t["id"] for t in tasks if t["id"]}
    for task in tasks:
        missing = sorted(set(task["dependsOn"]) - ids)
        if missing:
            errors.append(f"{task['id']} has missing dependencies: {', '.join(missing)}")
        if task["id"] in task["dependsOn"]:
            errors.append(f"{task['id']} depends on itself")

    indegree = {i: 0 for i in ids}
    children = {i: [] for i in ids}
    for task in tasks:
        if task["id"] not in ids:
            continue
        for dep in task["dependsOn"]:
            if dep in ids and dep != task["id"]:
                indegree[task["id"]] += 1
                children[dep].append(task["id"])
    queue = deque(sorted(i for i, n in indegree.items() if n == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in sorted(children[node]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(ids):
        cycle = sorted(i for i, n in indegree.items() if n > 0)
        errors.append(f"dependency cycle detected: {', '.join(cycle)}")

    return {"valid": not errors, "errors": errors, "tasks": tasks, "order": order}


def paths_overlap(left: list[str], right: list[str]) -> bool:
    """Conservative overlap check for planned file paths/write roots."""
    def clean(path: str) -> str:
        return os.path.normpath(path).replace("\\", "/").rstrip("/")
    for a0 in left:
        for b0 in right:
            a, b = clean(a0), clean(b0)
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
            # Globs are conservatively treated as sharing their non-glob prefix.
            ap = re.split(r"[*?[{]", a, maxsplit=1)[0].rstrip("/")
            bp = re.split(r"[*?[{]", b, maxsplit=1)[0].rstrip("/")
            if ap and bp and (ap == bp or ap.startswith(bp + "/") or bp.startswith(ap + "/")):
                return True
    return False


def select_wave(tasks: list[dict[str, Any]], completed: list[str] | None = None,
                blocked: list[str] | None = None, *, max_parallelism: int = 6) -> dict[str, Any]:
    completed_set = set(completed or [])
    blocked_set = set(blocked or [])
    remaining = [t for t in tasks if t["id"] not in completed_set]
    ready = [t for t in remaining if t["id"] not in blocked_set
             and set(t.get("dependsOn") or []).issubset(completed_set)]
    picked: list[dict[str, Any]] = []
    for task in ready:
        if len(picked) >= max(1, max_parallelism):
            break
        if all(not paths_overlap(task.get("files") or [], p.get("files") or []) for p in picked):
            picked.append(task)
    unresolved = [t["id"] for t in remaining if t not in ready]
    return {
        "ready": picked,
        "readyIds": [t["id"] for t in picked],
        "remainingIds": [t["id"] for t in remaining],
        "blockedIds": sorted(blocked_set),
        "unresolvedIds": unresolved,
        "done": not remaining,
        "stalled": bool(remaining and not picked),
    }


def validate_checkpoint(value: Any, *, phase: str = "", blocking_passed: bool = True,
                        allowed_checks: list[str] | None = None) -> dict[str, Any]:
    data = dict(value or {}) if isinstance(value, dict) else {}
    action = str(data.get("action") or "continue").strip().lower()
    errors: list[str] = []
    if action not in _ACTIONS:
        errors.append(f"unknown checkpoint action {action!r}")
    if action == "revise" and not str(data.get("feedback") or "").strip():
        errors.append("revise requires feedback")
    if action == "continue" and not blocking_passed:
        errors.append("blocking checks must pass before continue")
    if action == "run_checks":
        profile = str(data.get("profile") or "").strip()
        if not profile:
            errors.append("run_checks requires a profile")
        requested = _strings(data.get("checks"), "checks") if data.get("checks") is not None else []
        unknown = sorted(set(requested) - set(allowed_checks or []))
        if unknown:
            errors.append(f"checks are not in the selected profile: {', '.join(unknown)}")
    for key in ("maxTurns", "maxBudgetUsd"):
        if data.get(key) not in (None, ""):
            try:
                if float(data[key]) <= 0:
                    errors.append(f"{key} must be positive")
            except (TypeError, ValueError):
                errors.append(f"{key} must be numeric")
    requested_action = action
    feedback = str(data.get("feedback") or "")
    if errors:
        action = "revise"
        feedback = feedback or "; ".join(errors)
    return {"valid": not errors, "errors": errors, "action": action,
            "requestedAction": requested_action,
            "phase": phase, "feedback": feedback,
            "profile": str(data.get("profile") or ""),
            "checks": data.get("checks") or [],
            "attachedConfirmed": bool(data.get("attachedConfirmed", False)),
            "maxTurns": data.get("maxTurns"), "maxBudgetUsd": data.get("maxBudgetUsd"),
            "profiles": data.get("profiles") if isinstance(data.get("profiles"), dict) else None,
            "outcome": "incomplete" if action == "stop" else "running"}


def aggregate_usage(records: list[Any]) -> dict[str, Any]:
    tokens = 0
    cost = 0.0
    sessions: list[str] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        tokens += int(record.get("tokenUsed") or record.get("totalTokens") or 0)
        cost += float(record.get("costUsd") or record.get("totalCostUsd") or 0.0)
        sid = record.get("sessionId")
        if sid and sid not in sessions:
            sessions.append(str(sid))
    return {"totalTokens": tokens, "totalCostUsd": round(cost, 6), "sessions": sessions}
