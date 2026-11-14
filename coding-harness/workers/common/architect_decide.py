"""The Architect breaks a tie. Any agent (or a human steer) posts ``board decide "<conflict>"``; the
Architect answers in its own short session with a decision it MUST make, posted on the run board as a
binding directive to the parties and a ``decision`` fact. Nothing waits on a human.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

log = logging.getLogger("coding-harness.workers.architect_decide")

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "description": "the decision, in one or two sentences, stated as the rule the parties follow from now on"},
        "rationale": {"type": "string"},
        "directives": {"type": "array", "items": {"type": "object", "properties": {
            "target": {"type": "string", "description": "run (everyone), team:<name>, or an item id"},
            "text": {"type": "string"}}, "required": ["target", "text"]}},
    },
    "required": ["decision", "rationale", "directives"],
}


def _thread(run: str, limit: int = 24) -> str:
    from common import blackboard as bb
    rows = []
    for kind in ("directive", "lesson", "decision", "decision_request"):
        for f in bb.read_facts(run, kind=kind)[-limit:]:
            d = f.get("data") or {}
            rows.append((float(f.get("ts") or 0), f"- [{kind}] ({f.get('author')}) {d.get('text') or d.get('question') or d.get('decision') or ''}"))
    rows.sort()
    return "\n".join(r for _, r in rows[-limit:]) or "(empty)"


def decide(req: dict, *, inputs: dict, worktree: str) -> dict:
    """Run the Architect's decision session for one request and post the outcome. Returns the posting."""
    from common import architect, blackboard as bb, templating
    from common.coding_agent import run_coding_agent
    from common.model_policy import select_role_tier
    run = str(req.get("run") or "")
    question = str(req.get("text") or "")
    asked_by = str(req.get("item") or req.get("author") or "an agent")
    prog = bb.read_facts(run, kind="program")
    subsystems = (prog[-1].get("data") or {}).get("subsystems") if prog else []
    ctx = {"question": question, "by": asked_by, "plan": architect.plan_text(subsystems or []),
           "contract": architect.read_contract_brief(worktree) or "(no external checks)", "thread": _thread(run)}
    prompt = templating.render_default("architect_decide", ctx)
    out: dict = {}
    try:
        tier, _ = select_role_tier(inputs, role="plan", worktree=worktree)
        res = asyncio.run(run_coding_agent(prompt, worktree=worktree, model=tier.get("model") or None,
                                           tools=["Read", "Grep", "Glob"], allowed_tools=["Read", "Grep", "Glob"],
                                           max_turns=12, max_budget_usd=1.0, output_schema=SCHEMA))
        structured = res.get("structured")
        if isinstance(structured, str):
            structured = json.loads(structured)
        out = structured if isinstance(structured, dict) else {}
        if not out.get("decision"):
            out = {"decision": str(res.get("result") or "").strip()[:600] or "no decision text returned",
                   "rationale": "", "directives": []}
    except Exception as e:  # noqa: BLE001 - the tie is still broken, by the fallback rule, never left open
        log.warning("architect decide failed: %s", e)
        out = {"decision": f"The Architect's session failed ({type(e).__name__}); until it answers, the party "
                           f"that owns the file in question decides and the other adapts.", "rationale": str(e)[:300],
               "directives": []}
    decision = str(out.get("decision") or "")
    bb.post_fact(run, "architect", "decision", {"requestId": req.get("requestId"), "question": question,
                                               "decision": decision, "rationale": str(out.get("rationale") or ""),
                                               "by": "architect", "ts": time.time()})
    directives = [d for d in (out.get("directives") or []) if isinstance(d, dict) and str(d.get("text") or "").strip()]
    if not directives:
        directives = [{"target": "run", "text": decision}]
    for d in directives:
        bb.post_fact(run, "architect", "directive", {"target": str(d.get("target") or "run"), "by": "architect",
                                                    "urgency": "advisory",
                                                    "text": f"ARCHITECT DECISION (on {asked_by}'s question): {d.get('text')}"})
    return {"decision": decision, "directives": directives}
