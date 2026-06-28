"""safety_check -- the independent safety governor (spec section 14, 15.2).

Run at the start of every deep-assess pass. It answers a single question:
*may the campaign proceed?* It says no when

  - the authorization window has expired (re-reads the wall clock each tick),
  - the operator has tripped the kill-switch (a sentinel file on disk),
  - a prior action requested a halt (an accumulated breach flag), or
  - the manifest is structurally invalid.

The planner/executor cannot override it: it is a separate worker, consulted by a
SWITCH gate that TERMINATEs the workflow on ``proceed=false``. It never raises.
"""

from __future__ import annotations

import logging
import os

from conductor.client.worker.worker_task import worker_task

from common import authz

log = logging.getLogger(__name__)


@worker_task(task_definition_name="safety_check")
def safety_check(task):
    inp = task.input_data or {}
    manifest = inp.get("manifest") if isinstance(inp.get("manifest"), dict) else {}
    target = str(inp.get("target") or "").strip()
    # Conductor resolves an absent input to "" -> treat as None.
    kill_switch = str(inp.get("kill_switch") or "").strip()
    prior_halt = inp.get("prior_halt")

    # 1) A prior action breached policy and requested a halt.
    if isinstance(prior_halt, dict) and prior_halt.get("reason"):
        return {"proceed": False, "reason": f"halt requested by a prior action: {prior_halt['reason']}"}
    if isinstance(prior_halt, str) and prior_halt.strip():
        return {"proceed": False, "reason": f"halt requested by a prior action: {prior_halt}"}

    # 2) Operator kill-switch.
    if kill_switch and os.path.exists(kill_switch):
        return {"proceed": False, "reason": f"operator kill-switch is set ({kill_switch})"}

    # 3) Re-validate the manifest (window expiry is time-sensitive, so re-check it
    #    every pass even though normalize_target already gated the campaign start).
    verdict = authz.validate(manifest, target)
    if not verdict["ok"]:
        return {"proceed": False, "reason": f"authorization no longer valid: {verdict['reason']}"}

    return {"proceed": True, "reason": "ok", "capability_max": verdict["capability_max"]}
