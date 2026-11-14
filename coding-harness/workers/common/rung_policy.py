"""Hill-climbing over a role's model ladder: which rung (model + effort) an agent starts on.

A role's tiers in the model profile are a ladder, cheapest first (Sonnet at medium effort, Sonnet at
high, Opus at high). Inside one item the rescue ladder climbs a rung each time an agent fails in a way
an agent can fix (a red test, review findings, a failed check). Across items this module remembers what
worked: every finished item records the rung it was done on and every agent-fixable failure the rung it
failed on, per repository, role and kind of item. The next item of that kind starts at the cheapest
rung that has worked, and after a success it probes one rung lower, so the swarm drifts to the cheapest
rung that still gets the work done instead of starting every item at the bottom and climbing again, or
staying on the strongest model once it got there.

Failures that are not the agent's (the session never ran, the harness could not see the files, the
reviewer never answered) never move the ladder: a stronger model cannot fix them. Before this, a
harness fault that hid written files sent 32 sessions of one run up to Opus at high effort (run
a87e9ddb).
"""

from __future__ import annotations

import os
from pathlib import Path

OUTCOME_RUN = "_rung_outcomes"
OUTCOME_KIND = "rung_outcome"

# Reasons an item is reopened that no stronger agent can fix: they retry on the same rung.
INFRA_CAUSES = frozenset({"agent_unavailable", "audit_unavailable", "staging_fault", "reviewer_unavailable"})

# After this many successes on a rung since the rung below last failed, the rung below is probed again:
# the code, the prompts or the models may have changed since.
REPROBE_AFTER = 3

_MANIFESTS = frozenset({"package.json", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
                        "Cargo.toml", "go.mod", "pyproject.toml", "requirements.txt", "setup.py", "Gemfile",
                        "composer.json", "mix.exs", "Package.swift", "CMakeLists.txt", "Makefile"})


def agent_fixable(cause: str) -> bool:
    return str(cause or "").strip() not in INFRA_CAUSES


def repo_key(path: str) -> str:
    """The repository a worktree belongs to: every run of one repository shares its outcomes."""
    from common import git
    try:
        out = git.git(str(path), "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
    except OSError:
        out = ""
    if out:
        common = Path(out)
        return str(common.parent if common.name == ".git" else common)
    return os.path.realpath(str(path or ""))


def item_kind(entry: dict) -> str:
    """What kind of work an item is: ``docs``, ``scaffold`` (creates the project's build manifest),
    ``wiring`` (depends on two or more items and joins them) or ``module``."""
    entry = entry if isinstance(entry, dict) else {}
    if str(entry.get("deliverable") or "") == "docs":
        return "docs"
    files = [str(f) for f in entry.get("files") or []]
    if "scaffold" in str(entry.get("id") or "").lower() or any(os.path.basename(f) in _MANIFESTS for f in files):
        return "scaffold"
    if len([d for d in entry.get("dependsOn") or [] if str(d).strip()]) >= 2:
        return "wiring"
    return "module"


def record(repo: str, *, role: str, kind: str, rung: int, ok: bool, cause: str = "", item: str = "",
           run: str = "", backend=None) -> str:
    """One finished attempt: done on ``rung`` (``ok``) or failed there for an agent-fixable ``cause``."""
    from common import blackboard as bb
    return bb.post_fact(OUTCOME_RUN, f"team:{run}", OUTCOME_KIND,
                        {"repo": repo, "role": role, "kind": kind, "rung": int(rung), "ok": bool(ok),
                         "cause": str(cause or ""), "item": str(item or ""), "run": str(run or "")},
                        backend=backend)


def history(repo: str, *, role: str, kind: str, backend=None) -> list[dict]:
    """Recorded outcomes for this repository, role and kind, oldest first."""
    from common import blackboard as bb
    rows = []
    for f in bb.read_facts(OUTCOME_RUN, kind=OUTCOME_KIND, backend=backend):
        d = f.get("data") or {}
        if d.get("repo") == repo and d.get("role") == role and d.get("kind") == kind:
            rows.append({**d, "ts": f.get("ts") or 0})
    return sorted(rows, key=lambda r: r["ts"])


def choose_start(rows: list[dict], *, top: int) -> tuple[int, str]:
    """The rung to start the next item of this kind on, and why. ``top`` is the highest rung index."""
    top = max(int(top), 0)
    if not rows:
        return 0, "no history for this kind of item: start on the cheapest rung"
    last: dict[int, dict] = {}
    for r in rows:
        last[int(r["rung"])] = r
    worked = sorted(r for r, o in last.items() if o.get("ok"))
    if not worked:
        failed = max(last)
        start = min(failed + 1, top)
        return start, f"rung {failed} failed on this kind of item and none has worked yet: start on rung {start}"
    best = worked[0]
    below = best - 1
    if below < 0:
        return 0, "the cheapest rung got this kind of item done last time"
    tried = last.get(below)
    if tried is None:
        return below, f"rung {best} got this kind of item done: probe rung {below}, one cheaper"
    wins = sum(1 for r in rows if int(r["rung"]) == best and r.get("ok") and r["ts"] > tried["ts"])
    if wins >= REPROBE_AFTER:
        return below, (f"rung {below} failed on this kind of item, but rung {best} has succeeded {wins} times "
                       f"since: probe rung {below} again")
    return best, f"rung {best} is the cheapest that got this kind of item done (rung {below} failed)"


def start_rung(repo: str, *, role: str, kind: str, top: int, backend=None) -> tuple[int, str]:
    return choose_start(history(repo, role=role, kind=kind, backend=backend), top=top)


def outcomes_from_board(*, dispatches: list[dict], rescues: list[dict], final_state: str,
                        blocked_reason: str = "") -> list[dict]:
    """Turn one item's dispatch and rescue log into outcomes to record: a failure on the rung each
    agent-fixable rescue left, and a success on the last rung when the item is done."""
    dispatches = sorted(dispatches, key=lambda d: d.get("ts") or 0)
    out = []
    for r in sorted(rescues, key=lambda d: d.get("ts") or 0):
        before = [d for d in dispatches if (d.get("ts") or 0) <= (r.get("ts") or 0) and d.get("rung") is not None]
        if before and agent_fixable(r.get("cause")):
            out.append({"rung": int(before[-1]["rung"]), "ok": False, "cause": str(r.get("cause") or "")})
    ruled = [d for d in dispatches if d.get("rung") is not None]
    if ruled and final_state == "done":
        out.append({"rung": int(ruled[-1]["rung"]), "ok": True, "cause": ""})
    elif ruled and final_state == "blocked" and agent_fixable(blocked_reason.split(":", 1)[0].strip()):
        out.append({"rung": int(ruled[-1]["rung"]), "ok": False, "cause": "blocked"})
    return out
