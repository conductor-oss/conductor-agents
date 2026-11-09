"""Shared-file ownership on the board (roadmap H1, Phase 1).

File-disjoint partitioning collapses the moment two agents touch the same shared file. So the spike
records **who holds which shared file** as facts on the shared ``swarm_substrate`` blackboard, and an
item is dispatched against a shared file only when no live owner holds it — two agents never hold the
same file open concurrently.

Ownership is expressed as append-only ``claim`` and ``release`` facts (per-author-unique keys — no
lock, no lost update, consistent with the substrate's guarantee). The current owner of each file is a
**fold** of those events in timestamp order: the earliest still-active claim owns the file; later
claimants wait; when an owner releases, the next-earliest waiting claim takes it. Because the fold is
deterministic, every reader computes the same owner map from the same log.

Pure-ish logic (the fold is pure; claim/release/map read and write the board). The thin
``@worker_task`` wrapper lives in ``campaign/tasks.py``.
"""

from __future__ import annotations

from swarm_substrate import blackboard as bb

OWNERSHIP_KIND = "ownership"


def _events(correlation_id: str, *, backend=None) -> list[dict]:
    """All ownership events for the run, oldest first (read_facts already sorts by ts). A stable
    tiebreak on the fact key keeps same-ts events deterministically ordered across readers."""
    facts = bb.read_facts(correlation_id, kind=OWNERSHIP_KIND, backend=backend)
    return sorted(facts, key=lambda f: (f.get("ts", 0), f.get("data", {}).get("seq", "")))


def fold_owner_map(events: list[dict]) -> dict:
    """Pure fold: event list -> ``{path: owner}`` for files that currently have a live owner.

    Semantics: process events in order; for each ``claim`` by A on F, if F is unowned A becomes the
    owner, else A joins F's wait queue; a ``release`` by the current owner frees F and promotes the
    earliest still-waiting claimant. A release by a non-owner only drops them from the wait queue."""
    owner: dict[str, str] = {}
    waiters: dict[str, list[str]] = {}
    for ev in events:
        d = ev.get("data") or {}
        op = d.get("op")
        path = str(d.get("path") or "")
        who = str(d.get("author") or ev.get("author") or "")
        if not path or not who:
            continue
        if op == "claim":
            if path not in owner:
                owner[path] = who
            elif owner[path] != who and who not in waiters.get(path, []):
                waiters.setdefault(path, []).append(who)
        elif op == "release":
            if owner.get(path) == who:
                del owner[path]
                queue = waiters.get(path, [])
                while queue:
                    nxt = queue.pop(0)
                    owner[path] = nxt
                    break
                if not waiters.get(path):
                    waiters.pop(path, None)
            else:
                if who in waiters.get(path, []):
                    waiters[path].remove(who)
    return owner


def owner_map(correlation_id: str, *, backend=None) -> dict:
    """Current ``{path: owner}`` for the run, folded from the board's ownership log."""
    return fold_owner_map(_events(correlation_id, backend=backend))


def claim(correlation_id: str, author: str, path: str, *, backend=None) -> dict:
    """Append a claim for ``path`` and report whether this author now holds it. If another author is
    the live owner, the claim is recorded (queued) and ``granted`` is False — the caller must wait."""
    bb.post_fact(correlation_id, author, OWNERSHIP_KIND,
                 {"op": "claim", "path": str(path), "author": author}, backend=backend)
    om = owner_map(correlation_id, backend=backend)
    current = om.get(str(path))
    return {"granted": current == author, "owner": current, "path": str(path)}


def post_claim(correlation_id: str, author: str, path: str, *, backend=None) -> str:
    """Append a claim without re-folding the log. For a caller (the Lead's standup) that already holds
    the current owner map and simulates its own claims in memory (closure plan WP4: one ownership fold
    per standup instead of one per file)."""
    return bb.post_fact(correlation_id, author, OWNERSHIP_KIND,
                        {"op": "claim", "path": str(path), "author": author}, backend=backend)


def blocked_by(owners: dict, author: str, files) -> dict:
    """Pure: the files in ``files`` held by someone other than ``author`` in ``owners``."""
    return {str(f): owners[str(f)] for f in (files or [])
            if owners.get(str(f)) is not None and owners.get(str(f)) != author}


def release(correlation_id: str, author: str, path: str, *, backend=None) -> dict:
    """Append a release of ``path`` by ``author`` and report the new owner (if any)."""
    bb.post_fact(correlation_id, author, OWNERSHIP_KIND,
                 {"op": "release", "path": str(path), "author": author}, backend=backend)
    om = owner_map(correlation_id, backend=backend)
    return {"released": True, "owner": om.get(str(path)), "path": str(path)}


def can_dispatch(correlation_id: str, author: str, files, *, backend=None) -> dict:
    """An item may be dispatched only if every shared file it needs is free or already owned by this
    author. Returns ``{dispatchable, blocked_by}`` where ``blocked_by`` maps a blocked file to its
    current (other) owner."""
    om = owner_map(correlation_id, backend=backend)
    blocked = {}
    for f in (files or []):
        cur = om.get(str(f))
        if cur is not None and cur != author:
            blocked[str(f)] = cur
    return {"dispatchable": not blocked, "blocked_by": blocked}
