"""Safe candidate checkpoints and deterministic revision-loop scoring."""
from __future__ import annotations

import os
import re
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from common import git
from common.results import fail, ok


def _name(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "run")).strip("-")[:80] or "run"


def _owned_worktree(path: str) -> bool:
    # Never reset a source checkout. Harness run worktrees are intentionally named.
    real = os.path.realpath(path)
    return "/.cc-worktrees/run-" in real or "/.cc-worktrees/revision-" in real


def _ref(workflow_id: object, loop_id: object, candidate: object) -> str:
    return f"refs/conductor/revision/{_name(workflow_id)}/{_name(loop_id)}/{_name(candidate)}"


def _head(repo: str) -> str:
    return git.git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit_candidate(repo: str, message: str) -> tuple[str, list[str]]:
    before = git.status_changes(repo)
    created = sorted(p for p, state in before.items() if state == "A")
    with git._repo_lock(repo):
        git.git(repo, "add", "-A")
        git.git(repo, "commit", "-m", message, check=False)
        return _head(repo), created


def score_candidate(data: dict) -> dict:
    """Pure evaluation, kept independent of Conductor for exhaustive tests."""
    checks = data.get("checks") or []
    total = passed = 0.0
    blocking_failed = False
    for check in checks:
        if str(check.get("status", "")).lower() == "skipped":
            continue
        weight = float(check.get("weight", 1) or 1)
        total += weight
        if bool(check.get("passed")):
            passed += weight
        elif check.get("blocking", True):
            blocking_failed = True
    checks_score = passed / total if total else None
    findings = data.get("findings") or []
    penalty = sum(1.0 if str(x.get("severity", "")).lower() == "critical" else 0.5
                  if str(x.get("severity", "")).lower() == "major" else 0.0 for x in findings)
    review_score = max(0.0, 1.0 - penalty)
    acceptance = bool(data.get("accepted", False))
    weights = data.get("weights") or {"checks": .55, "review": .30, "acceptance": .15}
    signals = []
    if checks_score is not None: signals.append((float(weights.get("checks", .55)), checks_score))
    signals.extend([(float(weights.get("review", .30)), review_score), (float(weights.get("acceptance", .15)), 1.0 if acceptance else 0.0)])
    denominator = sum(weight for weight, _ in signals) or 1.0
    score = sum(weight * value for weight, value in signals) / denominator
    has_blocking_finding = any(str(x.get("severity", "")).lower() in {"critical", "major"} for x in findings)
    return {"score": round(score, 6), "checksScore": checks_score, "reviewScore": review_score,
            "acceptanceScore": 1.0 if acceptance else 0.0, "blockingChecksPassed": not blocking_failed,
            "autoEligible": not blocking_failed and acceptance and not has_blocking_finding}


@worker_task(task_definition_name="revision_checkpoint")
def revision_checkpoint(task):
    data = task.input_data or {}
    try:
        repo = str(data["worktreePath"])
        if not _owned_worktree(repo):
            raise ValueError("revision checkpoint requires a harness-owned worktree; source checkouts are never reset")
        action = str(data.get("action") or "save")
        workflow_id, loop_id = data.get("workflowId"), data.get("loopId")
        ref = _ref(workflow_id, loop_id, data.get("candidateId") or "best")
        expected_head = str(data.get("expectedHead") or "")
        if expected_head and _head(repo) != expected_head:
            raise ValueError("worktree HEAD changed unexpectedly; refusing checkpoint mutation")
        if action == "save":
            commit, created = _commit_candidate(repo, f"conductor revision { _name(loop_id) }")
            with git._repo_lock(repo):
                git.git(repo, "update-ref", ref, commit)
            return ok(task, {"action": "save", "commit": commit, "ref": ref, "createdPaths": created}, [f"[revision_checkpoint] saved {commit} -> {ref}"])
        if action == "restore":
            target = str(data.get("commit") or "").strip()
            if not target:
                existing = git.git(repo, "rev-parse", ref, check=False)
                target = existing.stdout.strip() if existing.code == 0 else ""
            if not target:
                return ok(task, {"action": "restore", "commit": _head(repo), "ref": ref, "restored": False},
                          ["[revision_checkpoint] no prior candidate; keeping current HEAD"])
            # A target must be an ancestor/reachable commit in this repository.
            if git.git(repo, "merge-base", "--is-ancestor", target, "HEAD", check=False).code != 0 and \
               git.git(repo, "merge-base", "--is-ancestor", "HEAD", target, check=False).code != 0:
                raise ValueError("restore target is not related to this worktree HEAD")
            with git._repo_lock(repo):
                git.git(repo, "reset", "--hard", target)
                for rel in data.get("createdPaths") or []:
                    path = (Path(repo) / str(rel)).resolve()
                    root = Path(repo).resolve()
                    if root in path.parents and path.exists() and path.is_file() and not git.git(repo, "ls-files", "--error-unmatch", str(rel), check=False).code == 0:
                        path.unlink()
            return ok(task, {"action": "restore", "commit": target, "ref": ref}, [f"[revision_checkpoint] restored {target}"])
        raise ValueError("action must be save or restore")
    except Exception as exc:  # noqa: BLE001
        return fail(task, "revision_checkpoint", exc)


@worker_task(task_definition_name="revision_evaluate")
def revision_evaluate(task):
    try:
        data = task.input_data or {}
        current = score_candidate(data)
        prior = data.get("best") or {}
        prior_score = float(prior.get("score", -1))
        better = (current["autoEligible"], current["score"]) > (bool(prior.get("autoEligible")), prior_score)
        rounds = int(data.get("round", 1))
        improvement = current["score"] - prior_score if prior else current["score"]
        plateau = int(data.get("plateauCount", 0)) + (1 if prior and improvement < float(data.get("minImprovement", .05)) else 0)
        max_rounds = int(data.get("maxRounds", 8))
        exhausted = rounds >= max_rounds or bool(data.get("budgetExhausted"))
        out = {**current, "retain": better or not prior, "improvement": round(improvement, 6), "plateauCount": plateau,
               "escalate": plateau >= int(data.get("plateauRounds", 2)),
               "stopReason": "budget_exhausted" if data.get("budgetExhausted") else "round_limit" if rounds >= max_rounds else "target" if current["autoEligible"] and current["score"] >= float(data.get("targetScore", 1.0)) else "continue",
               "needsHuman": exhausted or bool(data.get("modelExhausted"))}
        return ok(task, out, [f"[revision_evaluate] score={out['score']:.3f} eligible={out['autoEligible']} retain={out['retain']}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "revision_evaluate", exc)
