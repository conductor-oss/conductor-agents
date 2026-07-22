"""OpenSpec CLI wrapper — deterministic, typed access to the `openspec` CLI for
harness-driven planning (see openspecops/tasks.py). Every helper shells through
`common/exec.run` and parses `--json` stdout into plain dicts; `openspec new
change` self-bootstraps an implicit `openspec/` root in the target repo, so no
separate init step is needed.
"""

from __future__ import annotations

import json
import os

import yaml

from .exec import run

BIN = "openspec"

# Injected into the target repo's openspec/config.yaml `rules.tasks` (if not
# already present) so the `tasks` artifact is generated with independent,
# file-disjoint groups that `common/tasks_md.py` can deterministically parse
# into code_parallel's subtasks[] fan-out.
TASKS_RULE = (
    "Each numbered task group MUST be independent of every other group and "
    "file-disjoint: no file may be listed under more than one group. "
    "Immediately under each `## N. <title>` heading, before its checkbox "
    "items, add a `Files:` line listing every file the group touches "
    "(comma-separated) and a `Test:` line with the command that verifies it. "
    "Example:\nFiles: path/a.py, path/b.py\nTest: pytest path/to/test_a.py"
)


def _run_json(repo: str, *args: str) -> dict:
    res = run([BIN, *args, "--json"], cwd=repo)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"openspec {' '.join(args)} returned non-JSON output: {res.stdout[:300]}"
        ) from e


def new_change(repo: str, name: str, *, description: str | None = None) -> dict:
    args = ["new", "change", name]
    if description:
        args += ["--description", description]
    return _run_json(repo, *args)


def status(repo: str, change: str) -> dict:
    return _run_json(repo, "status", "--change", change)


def instructions(repo: str, artifact: str, change: str) -> dict:
    return _run_json(repo, "instructions", artifact, "--change", change)


def ensure_tasks_rule(repo: str) -> bool:
    """Seed the target repo's openspec/config.yaml with TASKS_RULE if it isn't
    already present. Returns True if it added the rule, False if the config
    file is missing or already carries the rule. Never touches other keys."""
    path = os.path.join(repo, "openspec", "config.yaml")
    if not os.path.isfile(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    rules = cfg.setdefault("rules", {})
    tasks_rules = rules.setdefault("tasks", [])
    if any(TASKS_RULE in r for r in tasks_rules):
        return False
    tasks_rules.append(TASKS_RULE)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    return True
