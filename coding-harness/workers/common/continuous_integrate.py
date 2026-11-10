"""Continuous-rebase integrator for the convergence spike (roadmap H1, Phase 1).

N long-lived branches merged pairwise at the end is the exponential merge pain real teams avoid. The
spike instead integrates like trunk-based development: agents work in **short-lived** worktrees and an
integrator **rebases each ready worktree onto the current shared-branch head, one at a time**, so the
shared head moves after every integration and the next item builds on it. There is **no single
big-bang merge** of all branches at the end.

A rebase conflict does not silently drop work: the item is **re-opened** with the precise conflict
mechanism attached (recorded on the board), for a follow-up pass (the agent conflict resolver in
``gitops.tasks`` can be reused by the caller) or human attention.

The ordering/classification is pure logic (unit-testable without git); the rebase/advance helpers call
``common.git`` and are exercised against a real temporary repo in the tests.
"""

from __future__ import annotations

from common import git

# Failure reasons a later wave can fix by re-dispatching (the moving shared head lets a retry land). A
# frozen-contract edit is NOT here: it is a controlled event for a human (H4), never a blind retry.
# ``integration_oracle_red``: the item's frozen acceptance tests fail at the integrated head after a
# sibling landed; the re-dispatch fixes the code or amends the test (the peer reviewer decides).
RETRYABLE_REOPEN = ("rebase_conflict", "empty_delivery", "integration_oracle_red")


def integration_order(ready_items) -> list[dict]:
    """Deterministic order to integrate ready items: by the time they became ready, then by id. Pure —
    so every actor computes the same order from the same inputs."""
    items = [it for it in (ready_items or []) if isinstance(it, dict)]
    return sorted(items, key=lambda it: (it.get("readyAt", 0), str(it.get("id", ""))))


def classify_rebase_result(returncode: int, conflicts) -> dict:
    """Pure: a rebase is ``integrated`` only with exit 0 and no conflicted files; otherwise ``conflict``."""
    conflicts = [c for c in (conflicts or []) if str(c or "").strip()]
    if int(returncode) == 0 and not conflicts:
        return {"status": "integrated", "conflicts": []}
    return {"status": "conflict", "conflicts": sorted(conflicts)}


def reopen_record(item_id: str, onto_ref: str, conflicts, touched=None) -> dict:
    """A board-ready record re-opening an item whose rebase conflicted, naming the precise mechanism
    (which files conflicted against which ref, and which shared-branch commit last changed each) per
    the AGENTS.md failure-terminology policy. ``touched`` maps a conflicting path to the ``<sha> <subject>``
    of the shared-branch commit that last changed it: the other side of the conflict, so the reader
    sees at once WHOSE change collided (a sibling fork's test-cycle repair had written another item's
    file, run a33d7061, store/lists.go)."""
    conflicts = sorted(c for c in (conflicts or []) if str(c or "").strip())
    touched = {str(k): str(v) for k, v in (touched or {}).items() if str(v or "").strip()}
    named = [f"{c} (last changed on {onto_ref} by {touched[c]})" if c in touched else c for c in conflicts]
    return {
        "op": "reopen",
        "item": str(item_id),
        "reason": "rebase_conflict",
        "onto": str(onto_ref),
        "conflicts": conflicts,
        "touched": touched,
        "mechanism": (f"continuous rebase of item {item_id} onto {onto_ref} left unresolved conflict "
                      f"markers in: {', '.join(named) or '(none reported)'}"),
    }


def last_change(shared_repo: str, shared_branch: str, paths) -> dict:
    """``{path: "<sha> <subject>"}`` of the shared-branch commit that last changed each path."""
    out = {}
    for path in paths or []:
        line = git.git(shared_repo, "log", "-1", "--format=%h %s", shared_branch, "--", str(path),
                       check=False).stdout.strip()
        if line:
            out[str(path)] = line
    return out


def rebase_onto(worktree_path: str, onto_ref: str) -> dict:
    """Rebase the branch checked out in ``worktree_path`` onto ``onto_ref``. On conflict, abort the
    rebase (leaving the worktree clean) and report the conflicted files. Returns the classification
    plus the resulting HEAD on success."""
    res = git.git(worktree_path, "rebase", onto_ref, check=False)
    conflicts = git.has_conflicts(worktree_path)
    classified = classify_rebase_result(res.code, conflicts)
    if classified["status"] == "conflict":
        git.git(worktree_path, "rebase", "--abort", check=False)
        return classified
    classified["head"] = git.head(worktree_path)
    return classified


def integrate_ready(shared_repo: str, shared_branch: str, items, *, correlation_id: str | None = None,
                    backend=None) -> dict:
    """Continuously integrate ready items onto the moving shared branch.

    ``items`` is a list of ``{id, worktree, branch[, readyAt]}``. For each item in
    ``integration_order``: rebase its short-lived worktree onto the current ``shared_branch`` head,
    then fast-forward ``shared_branch`` to the rebased tip (so the head moves for the next item). On
    conflict, produce a ``reopen_record`` (posted to the board when ``correlation_id`` is given) and
    skip the item — the shared branch is left untouched. There is no big-bang merge step.

    Returns ``{integrated, reopened, big_bang_merge: False, final_head}``."""
    integrated, reopened = [], []
    for it in integration_order(items):
        branch = str(it.get("branch") or "")
        worktree = str(it.get("worktree") or "")
        item_id = str(it.get("id") or branch)
        # Several integrators may advance ONE shared branch concurrently (child teams of a program, H3).
        # Rebase-onto-current-head + fast-forward is done under the repo's cross-process lock, so each
        # integration sees the head the previous one left: the branch only ever moves forward, linearly.
        with git._repo_lock(shared_repo):
            res = rebase_onto(worktree, shared_branch)
            refused = ""
            if res["status"] == "integrated":
                # --ff-only guarantees the head only moves forward onto work that already contains the
                # prior shared head (no merge commit). A refusal (an untracked file in the shared
                # worktree in the way of the fork's commit, run a4f1ff1f) re-opens this one item; it
                # must not end the whole wave's integration with every claim still held.
                ff = git.git(shared_repo, "merge", "--ff-only", branch, check=False)
                if ff.code != 0:
                    git.git(shared_repo, "merge", "--abort", check=False)
                    refused = (ff.stderr or ff.stdout or "").strip()[-600:] or f"exit {ff.code}"
                head = git.head(shared_repo)
        if res["status"] == "integrated" and not refused:
            integrated.append({"item": item_id, "branch": branch, "head": head})
        elif refused:
            rec = {**reopen_record(item_id, shared_branch, []),
                   "mechanism": f"fast-forward of item {item_id} ({branch}) onto {shared_branch} refused: {refused}"}
            if correlation_id is not None:
                from common import blackboard as bb
                bb.post_fact(correlation_id, item_id, "reopen", rec, backend=backend)
            reopened.append(rec)
        else:
            rec = reopen_record(item_id, shared_branch, res["conflicts"],
                                touched=last_change(shared_repo, shared_branch, res["conflicts"]))
            if correlation_id is not None:
                from common import blackboard as bb
                bb.post_fact(correlation_id, item_id, "reopen", rec, backend=backend)
            reopened.append(rec)
    return {
        "integrated": integrated,
        "reopened": reopened,
        "big_bang_merge": False,
        "final_head": git.head(shared_repo),
    }
