"""The round's permission requests (docs/PERMISSIONS.md): decided by the run's history, the repository's
standing grants and the policy where they can be; otherwise the run waits for an approver, a person or
another agent, deciding through the console API. Agents never keep retrying what they are refused."""
from __future__ import annotations

import os
import time

from conductor.client.worker.worker_task import worker_task

from common import permissions
from common.results import fail, ok

POLL_S = 5


def _structured(evaluation) -> dict:
    s = (evaluation or {}).get("structured") if isinstance(evaluation, dict) else None
    if isinstance(s, str):
        import json
        try:
            s = json.loads(s)
        except ValueError:
            s = None
    return s if isinstance(s, dict) else {}


def repo_key(target: dict | None, repo_path: str) -> str:
    slug = str((target or {}).get("slug") or "").strip()
    return f"github:{slug}" if slug else os.path.abspath(repo_path or ".")


@worker_task(task_definition_name="swarm_permissions")
def swarm_permissions(task):
    """Submit the round review's permission requests and wait until every one is decided."""
    i = task.input_data or {}
    run, rnd = str(i.get("runId") or ""), int(i.get("round") or 0)
    # A delivered run needs nothing more; a run out of budget ends this round whatever is decided.
    if any(str(i.get(k)).strip().lower() == "true" for k in ("delivered", "stop")):
        return ok(task, {"decided": [], "pending": [], "waitedSeconds": 0, **permissions.grants(run)},
                  ["[swarm_permissions] the run is ending: nothing to ask"])
    from common.progress import ProgressReporter
    reporter = ProgressReporter(task, heartbeat_s=15.0).start()
    try:
        asked = _structured(i.get("evaluation")).get("permissions") or []
        submitted = permissions.submit(run, rnd, asked, workdir=str(i.get("workdir") or "."),
                                       repo_key=repo_key(i.get("target"), str(i.get("repoPath") or "")))
        wait_s = float(i.get("waitMinutes") or 0) * 60
        started = time.monotonic()
        waiting = permissions.pending(run)
        while waiting and (not wait_s or time.monotonic() - started < wait_s):
            reporter.update({"progress": f"waiting for approval: {len(waiting)} request(s): " + "; ".join(
                f"{r['kind']} {r['target']}" for r in waiting[:4]), "pending": waiting})
            time.sleep(POLL_S)
            waiting = permissions.pending(run)
        granted = permissions.grants(run)
        return ok(task, {"decided": submitted["decided"], "pending": waiting,
                         "waitedSeconds": round(time.monotonic() - started), **granted},
                  [f"[swarm_permissions] round {rnd}: {len(asked)} asked, {len(submitted['decided'])} decided by "
                   f"policy, {len(waiting)} still pending"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_permissions", e)
    finally:
        reporter.stop()
