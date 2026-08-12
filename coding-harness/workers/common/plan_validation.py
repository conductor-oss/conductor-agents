"""Deterministic validation for plans that will fan out into parallel work.

This module validates only the execution contract: task identity, descriptions,
and exclusive ownership of safe repository-relative file paths.  Testing is a
separate concern and is deliberately not represented in the normalized plan.
"""

from __future__ import annotations

import re
from pathlib import Path


_SUBTASK_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_TEST_FIELDS = {"testCmd", "testCommands"}


class PlanValidationError(ValueError):
    """A candidate parallel plan violates one or more deterministic rules."""

    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__("; ".join(str(issue["message"]) for issue in issues))


def validate_subtasks(value: object, repo_path: object = "") -> list[dict]:
    """Return a normalized plan or raise with every actionable issue found."""
    issues: list[dict] = []
    if not isinstance(value, list) or not value:
        raise PlanValidationError([{
            "code": "empty_plan",
            "field": "subtasks",
            "message": "subtasks must be a non-empty array",
        }])

    repo = Path(str(repo_path)).resolve() if str(repo_path or "").strip() else None
    ids: set[str] = set()
    claimed: dict[str, str] = {}
    normalized: list[dict] = []

    for index, raw in enumerate(value):
        location = f"subtasks[{index}]"
        if not isinstance(raw, dict):
            issues.append({"code": "invalid_subtask", "field": location,
                           "message": f"{location} must be an object"})
            continue

        subtask_id = str(raw.get("id") or "").strip()
        if not _SUBTASK_ID_RE.fullmatch(subtask_id):
            issues.append({"code": "invalid_id", "field": f"{location}.id",
                           "message": f"{location}.id must be a lowercase kebab-case slug"})
        elif subtask_id in ids:
            issues.append({"code": "duplicate_id", "field": f"{location}.id", "subtask": subtask_id,
                           "message": f"subtask id {subtask_id!r} is claimed more than once"})
        else:
            ids.add(subtask_id)

        description = str(raw.get("description") or "").strip()
        if not description:
            issues.append({"code": "missing_description", "field": f"{location}.description",
                           "subtask": subtask_id,
                           "message": f"{location}.description must be non-empty"})

        raw_files = raw.get("files")
        clean_files: list[str] = []
        if not isinstance(raw_files, list) or not raw_files:
            issues.append({"code": "missing_files", "field": f"{location}.files",
                           "subtask": subtask_id,
                           "message": f"{location}.files must be a non-empty array of exact files"})
        else:
            for file_index, file_value in enumerate(raw_files):
                field = f"{location}.files[{file_index}]"
                path = str(file_value or "").strip().replace("\\", "/")
                candidate = Path(path)
                if (not path or candidate.is_absolute() or re.match(r"^[A-Za-z]:/", path)
                        or ".." in candidate.parts or path in {".", ".."}):
                    issues.append({"code": "unsafe_path", "field": field, "subtask": subtask_id,
                                   "message": f"{field} is not a safe repository-relative file: {path!r}"})
                    continue
                path = candidate.as_posix()
                if repo is not None and (repo / path).is_dir():
                    issues.append({"code": "directory_claim", "field": field, "subtask": subtask_id,
                                   "message": f"{field} must name an exact file, not directory {path!r}"})
                    continue
                owner = claimed.get(path)
                if owner is not None:
                    issues.append({"code": "overlapping_file", "field": field, "subtask": subtask_id,
                                   "conflictsWith": owner, "path": path,
                                   "message": f"file {path!r} is claimed by both {owner!r} and {subtask_id!r}"})
                    continue
                claimed[path] = subtask_id
                clean_files.append(path)

        normalized.append({
            **{key: item for key, item in raw.items() if key not in _TEST_FIELDS},
            "id": subtask_id,
            "description": description,
            "files": clean_files,
        })

    if issues:
        raise PlanValidationError(issues)
    return normalized


def inspect_subtasks(value: object, repo_path: object = "", *, current: object = None,
                     exhausted: bool = False) -> dict:
    """Return a workflow-friendly result instead of failing the worker task.

    On failure the caller's ``current`` plan is returned instead of an empty
    list, so a repair round has the plan it is meant to fix rather than
    nothing. This used to be a `JSON_JQ_TRANSFORM` wrapping this call; folding
    it in here removed that task entirely.
    """
    try:
        subtasks = validate_subtasks(value, repo_path)
        return {"valid": True, "planningState": "valid", "issues": [], "subtasks": subtasks}
    except PlanValidationError as exc:
        state = "plan_exhausted" if exhausted else "needs_replan"
        fallback = current if isinstance(current, list) else []
        return {"valid": False, "planningState": state, "issues": exc.issues, "subtasks": fallback}
