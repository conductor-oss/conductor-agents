"""Swarm standup-loop workers (design/SWARM.md sections 2-7).

The round loop is: seed -> [standup -> wave -> reconcile]* -> scribe. Each worker here is thin: it
reads/writes the blackboard and calls the pure engines (allocator, chaining). The hundreds of agents
run as the wave's dynamic fork; these tasks are the lead/scribe that make them a TEAM.

Every task never raises (errors come back in the result) so one bad standup can't crash a campaign.
"""

from __future__ import annotations

import logging

from conductor.client.worker.worker_task import worker_task

from common import allocator as al
from common import blackboard as bb
from common import doc_surface as docsurf
from common import identity as idmod
from common import oracle_router as orc
from common import repro as repro_mod

# Oracles whose proof needs a distinctive datum from ANOTHER identity — hand the agent those markers so
# it doesn't burn its budget discovering "which resource does the victim own".
_VICTIM_MARKER_ORACLES = ("cross_identity_access", "cross_tenant_write_persist", "monotonic_state")

log = logging.getLogger(__name__)

# Role -> the sub-workflow that implements it. Exploiters pick the deepening ladder for injection/
# RCE/CVE classes, the general agent otherwise (mirrors assess_pass's routing).
_INJECTION_RE = ("inject", "rce", "sqli", "sql", "eval", "sandbox", "deserial", "ssti", "expression",
                 "supply-chain", "cve", "xss", "traversal", "lfi", "ssrf", "open-redirect")


def _workflow_id(task, inp) -> str:
    return (getattr(task, "workflow_instance_id", None) or inp.get("author")
            or inp.get("run_id") or "anon")


def _exploiter_wf(objective_id: str, category: str) -> str:
    blob = f"{objective_id} {category}".lower()
    return "exploit_deepen" if any(t in blob for t in _INJECTION_RE) else "exploit_agent"


def _role_wf(role: str, objective_id: str, category: str) -> str:
    if role == "scout":
        return "scout_agent"
    if role == "refuter":
        return "refuter"
    # Exploiters AND chainers both run the deepening engine via chain_agent, which POSTS the
    # result (finding + verified_primitive + attempt-lesson) back to the board. Routing exploiters
    # straight to exploit_deepen lost their output -- the board never saw their confirmations, so
    # nothing compounded. chain_agent is the single exploit-and-post wrapper for both.
    return "chain_agent"


@worker_task(task_definition_name="swarm_seed", thread_count=2)
def swarm_seed(task):
    """Seed the campaign board: a coverage fact per candidate area (untested) AND the initial batch
    of hypotheses (from the hypothesize step) as OPEN work for the exploit/chain agents. Posting the
    hypotheses here is what lets round 1 already dispatch exploiters -- it bootstraps the depth half.
    Returns the area list + empty credits for the first standup."""
    inp = task.input_data or {}
    wf = _workflow_id(task, inp)
    cid = str(inp.get("correlation_id") or wf)
    objectives = inp.get("catalog_objectives") if isinstance(inp.get("catalog_objectives"), list) else []
    identities = list((inp.get("identities") or {}).keys()) if isinstance(inp.get("identities"), dict) else []
    hypotheses = inp.get("hypotheses") if isinstance(inp.get("hypotheses"), list) else []
    areas = []
    for obj in objectives:
        oid = obj.get("id") if isinstance(obj, dict) else str(obj)
        if not oid:
            continue
        areas.append({"area": oid, "objective_id": oid, "identities": identities[:2], "untested": True})
        bb.post_fact(cid, wf, "coverage", {"area": oid, "status": "untested", "untested": True})
    posted = 0
    seen_sigs = set()
    # SOURCE-derived hypotheses (from the SAST hunt) seed the board so round 1 attacks real code
    # paths, not just the URL surface.
    for h in hypotheses:
        if not isinstance(h, dict) or not (h.get("title") or h.get("objective_id")):
            continue
        sig = al.signature(h)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        bb.post_fact(cid, wf, "hypothesis", {**h, "signature": sig})
        posted += 1
    # DEP-derived hypotheses (from the dependency CVE scan) — attack known-vulnerable components.
    cve_leads = inp.get("cve_leads") if isinstance(inp.get("cve_leads"), list) else []
    cve_posted = 0
    for c in cve_leads:
        if not isinstance(c, dict):
            continue
        title = c.get("title") or (f"{c.get('package','')} {c.get('cve','')}".strip())
        if not title:
            continue
        h = {"title": title, "objective_id": c.get("objective_id") or "infra-supply-chain",
             "owasp": "A06:2021 - Vulnerable and Outdated Components",
             "rationale": c.get("summary") or c.get("detail") or "", "cve_id": c.get("cve"),
             "target": c.get("package") or "", "source": "dep_cve_scan"}
        sig = al.signature(h)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        bb.post_fact(cid, wf, "hypothesis", {**h, "signature": sig})
        cve_posted += 1
    # CROSS-RUN self-learning: re-seed prior-confirmed findings for THIS target as high-priority
    # re-verify hypotheses (regression check + warm start), so the swarm re-proves known bugs fast
    # instead of rediscovering them cold.
    prior_confirmed = inp.get("prior_confirmed") if isinstance(inp.get("prior_confirmed"), list) else []
    prior_posted = 0
    for pc in prior_confirmed:
        if not isinstance(pc, dict) or not (pc.get("title") or pc.get("objective_id")):
            continue
        sig = pc.get("signature") or al.signature(pc)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        bb.post_fact(cid, wf, "hypothesis",
                     {**pc, "signature": sig, "prior_confirmed": True,
                      "rationale": "Confirmed in a prior campaign against this deployment — re-verify "
                                   "(regression) and chain from it.", "source": "cross_run_memory"})
        prior_posted += 1
    # DOC-GROUNDED hypotheses (from doc-surface extraction): documented endpoints, authz/tenancy/
    # masking invariants, and high-value feature families (MCP/A2A/remote/gateway/audit/tag/status/
    # webhook). Seeded deterministically like CVE leads so documented surface is ALWAYS hunted in
    # round 1 — closes the "docs read but not adapted" gap (run 05b95dd9: only 1/36 doc-grounded).
    doc_posted = 0
    for h in docsurf.seed_hypotheses(str(inp.get("run_id") or cid)):
        sig = al.signature(h)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        bb.post_fact(cid, wf, "hypothesis", {**h, "signature": sig})
        doc_posted += 1
    return {"correlation_id": cid, "areas": areas, "credits": {},
            "summary": f"seeded {len(areas)} areas + {posted} source/initial + {cve_posted} CVE "
                       f"+ {prior_posted} prior-confirmed (cross-run) + {doc_posted} doc-grounded hypotheses"}


@worker_task(task_definition_name="swarm_standup", thread_count=2)
def swarm_standup(task):
    """Plan the next wave: read board state, score areas by credit, and emit the dynamic-fork tasks
    (one per allocated agent). Returns forkTasks/forkTasksInputs for a FORK_JOIN_DYNAMIC plus a fact
    watermark so the following reconcile only folds in THIS wave's facts."""
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or _workflow_id(task, inp))
    credits = inp.get("credits") if isinstance(inp.get("credits"), dict) else {}
    areas = inp.get("areas") if isinstance(inp.get("areas"), list) else []
    width_ceiling = int(inp.get("width_ceiling") or 24)
    budget_left = inp.get("budget_left")
    budget_left = int(budget_left) if str(budget_left or "").strip() != "" else None
    diversity = inp.get("diversity") if isinstance(inp.get("diversity"), dict) else {}

    state = bb.assemble_state(cid)
    cov = state["coverage"]
    # Refresh each area's untested flag from the board (scouts mark areas scouted over rounds).
    annotated = [{**a, "untested": bool((cov.get(str(a.get("area") or a.get("objective_id") or "")) or {})
                                        .get("untested", a.get("untested", True)))} for a in areas]

    plan = al.plan_wave(annotated, state["open_hypotheses"], state["review_candidates"],
                        credits, width_ceiling, budget_left, diversity=diversity)
    wf_self = _workflow_id(task, inp)
    fork_tasks, fork_inputs = [], {}
    passthru = {k: inp.get(k) for k in ("base_url", "scope", "identities", "app_model", "manifest",
                                        "capability_max", "oob_base", "run_id", "docs_namespace",
                                        "docs_index", "docs_available", "model", "llm_provider",
                                        "max_steps")}
    for i, a in enumerate(plan["assignments"]):
        ref = f"agent_{i}"
        wf = _role_wf(a["role"], a.get("objective_id", ""), a.get("area", ""))
        fork_tasks.append({"name": wf, "taskReferenceName": ref, "type": "SUB_WORKFLOW",
                           "optional": True, "subWorkflowParam": {"name": wf, "version": 1}})
        persona = a.get("persona", "")
        focus = a.get("focus_directive", "")
        if persona:
            focus = (f"Act as the {persona} persona. " + focus).strip()
        agent_in = {**passthru, "correlation_id": cid, "role": a["role"],
                    "objective_id": a.get("objective_id", ""), "area": a.get("area", ""),
                    "model": a.get("model") or passthru.get("model"),
                    "persona": persona, "temperature": a.get("temperature"),
                    "identity_focus": a.get("identities", []), "focus_directive": focus}
        # Carry the role's payload: exploiters/chainers get the hypothesis; refuters get the candidate.
        hyp = a.get("hypothesis")
        if hyp:
            # Gap 1 (oracle-selection): deterministically tag the hypothesis with the ONLY oracle that
            # can prove its class, and tell the agent exactly which probe/fields to plant. Stamp it on
            # the hypothesis so the agent, the refuter, and the board all agree on the confirming oracle.
            rec = orc.recommend_oracle(hyp)
            hyp = {**hyp, "confirming_oracle": rec["oracle"], "oracle_probe_fields": rec["probe_fields"],
                   "oracle_confidence": rec["confidence"]}
            agent_in["hypothesis"] = hyp
            agent_in["confirming_oracle"] = rec["oracle"]
            directive = orc.oracle_directive(hyp, rec=rec)
            # For identity-sensitive oracles, hand the agent the victim identity's distinctive markers
            # (decoded from the other identities' JWT claims) so it can plant/prove a real victim_marker
            # immediately instead of spending its whole budget discovering who owns what.
            if rec["oracle"] in _VICTIM_MARKER_ORACLES:
                vm = idmod.victim_marker_directive(inp.get("identities") if isinstance(inp.get("identities"), dict) else {},
                                                   exclude=(a.get("identities") or [""])[0] if a.get("identities") else "")
                if vm:
                    directive = directive + " " + vm
            agent_in["focus_directive"] = (directive + " "
                                           + (agent_in.get("focus_directive") or "")).strip()
            # RETRY escalation: if this hypothesis was attempted before, feed the prior lessons into
            # the directive so the agent tries a DIFFERENT approach rather than repeating what failed.
            n = int(hyp.get("attempt_count") or 0)
            if n > 0:
                prior = "; ".join(hyp.get("lessons") or []) or "no detail recorded"
                agent_in["focus_directive"] = (
                    f"RETRY #{n + 1} of this hypothesis — prior attempt(s) did NOT confirm it. "
                    f"What failed before: {prior}. Escalate with a DIFFERENT technique/identity/encoding; "
                    f"do not repeat the prior approach. " + (agent_in.get("focus_directive") or "")).strip()
            # Mark this hypothesis DISPATCHED (counts toward its retry budget).
            bb.post_fact(cid, wf_self, "attempt", {"signature": al.signature(hyp),
                                                   "objective_id": hyp.get("objective_id", ""),
                                                   "attempt": n + 1})
        if a.get("candidate"):
            agent_in["candidate"] = a["candidate"]
        fork_inputs[ref] = agent_in

    roles = {}
    for a in plan["assignments"]:
        roles[a["role"]] = roles.get(a["role"], 0) + 1
    return {"forkTasks": fork_tasks, "forkTasksInputs": fork_inputs, "width": plan["width"],
            "fact_watermark": state["fact_count"], "roles": roles,
            "summary": f"standup planned a wave of {plan['width']} agents: {roles}"}


@worker_task(task_definition_name="swarm_reconcile", thread_count=2)
def swarm_reconcile(task):
    """Fold the completed wave back in: dedup, boost/decay credit, and SYNTHESIZE verified primitives
    into emergent chain-hypotheses (posted back to the board as mandatory hypotheses for the next
    wave). Returns updated credits and the reflect/stop decision."""
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or _workflow_id(task, inp))
    credits = inp.get("credits") if isinstance(inp.get("credits"), dict) else {}
    since = int(inp.get("fact_watermark") or 0)
    round_idx = int(inp.get("round_idx") or 0)
    max_rounds = int(inp.get("max_rounds") or 4)
    budget_left = inp.get("budget_left")
    budget_left = int(budget_left) if str(budget_left or "").strip() != "" else None
    identities = list((inp.get("identities") or {}).keys()) if isinstance(inp.get("identities"), dict) else []

    all_facts = bb.read_facts(cid)
    new_facts = all_facts[since:]
    state = bb.assemble_state(cid)
    out = al.reconcile(state, new_facts, credits=credits, identity_labels=identities)

    # Post synthesized chains as mandatory hypotheses so the next wave picks them up.
    for ch in out["emergent_chains"]:
        sig = ("chaining|" + str(ch.get("target", "")).lower().replace(" ", "") + "|"
               + ",".join(sorted(str(i) for i in ch.get("identities", []))))
        bb.post_fact(cid, _workflow_id(task, inp), "hypothesis", {**ch, "signature": sig})
    for area, upd in out["coverage_updates"].items():
        bb.post_fact(cid, _workflow_id(task, inp), "coverage", {"area": area, **upd})

    # Progress = a new confirmation or verified primitive this round (can seed chains next round).
    new_prims = sum(1 for f in new_facts if (f or {}).get("kind") == "verified_primitive")
    made_progress = bool(out["promoted"]) or new_prims > 0
    # Re-read AFTER posting emergent chains + coverage so the stop decision sees the work they added.
    state2 = bb.assemble_state(cid)
    decision = al.should_continue(state2, round_idx=round_idx, max_rounds=max_rounds,
                                  budget_left=budget_left, made_progress=made_progress)
    wr = al.work_remaining(state2)
    return {"credits": out["credit_updates"], "keep_going": decision["continue"],
            "reason": decision["reason"], "emergent_chains": out["emergent_chains"],
            "promoted_count": len(out["promoted"]), "work_remaining": wr,
            "rollup": bb.campaign_rollup(cid),
            "summary": f"reconciled {len(new_facts)} facts; work left={wr['total']}; "
                       f"continue={decision['continue']} ({decision['reason']})"}


@worker_task(task_definition_name="swarm_post_fact", thread_count=8)
def swarm_post_fact(task):
    """Generic board writer an agent sub-workflow calls to publish a fact (hypothesis, observation,
    attempt, verified_primitive, finding, claim, critique). Author defaults to the calling workflow
    id so facts are attributable and reproducible."""
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or "")
    kind = str(inp.get("kind") or "observation")
    data = inp.get("data") if isinstance(inp.get("data"), dict) else {"value": inp.get("data")}
    author = str(inp.get("author") or _workflow_id(task, inp))
    if not cid:
        return {"posted": "", "error": "no correlation_id"}
    # Budget facts route to the lock-free per-author counter (campaign spend rollup), not the log.
    if kind == "budget":
        total = bb.bump_budget(cid, author, requests=int(data.get("requests", 0) or 0),
                               bytes=int(data.get("bytes", 0) or 0))
        return {"posted": "budget", "kind": kind, "total": total, "summary": "bumped campaign budget"}
    key = bb.post_fact(cid, author, kind, data)
    return {"posted": key, "kind": kind, "summary": f"posted {kind} fact"}


@worker_task(task_definition_name="swarm_rehypothesize_prep", thread_count=4)
def swarm_rehypothesize_prep(task):
    """Build the hypothesize input from ACCUMULATED LEARNING so each round forms NEW hypotheses from
    what the team discovered (scout observations + confirmed findings), not just the seed batch. This
    is round-over-round learning: round N proposes tests informed by rounds 1..N-1. Returns the prep
    JSON and whether there are observations yet (round 1 has none -> skip, the seed batch covers it)."""
    import json as _j
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or "")
    app_model = inp.get("app_model") if isinstance(inp.get("app_model"), dict) else {}
    identities = list((inp.get("identities") or {}).keys()) if isinstance(inp.get("identities"), dict) else []
    if not cid:
        return {"prep": "{}", "have_observations": False}
    state = bb.assemble_state(cid)
    obs = []
    for f in bb.read_facts(cid):
        if f.get("kind") == "observation":
            d = f.get("data") or {}
            note = d.get("note") or d.get("summary")
            if note:
                obs.append(str(note)[:200])
    obs = obs[-40:]
    confirmed = [{"title": f.get("title"), "objective_id": f.get("objective_id")}
                 for f in state["findings"] if f.get("confirmed") and f.get("refuted") is not True]
    # Full-system understanding threaded in from the understanding phase (source, docs, features,
    # deps, personas) so hypotheses target the WHOLE SYSTEM, not just the URL surface.
    def _list(k):
        v = inp.get(k)
        return v if isinstance(v, list) else []
    def _obj(k):
        v = inp.get(k)
        return v if isinstance(v, dict) else {}
    sast_findings = _list("sast_findings")
    cve_leads = _list("cve_leads")
    personas = _list("personas")
    feature_playbook = _obj("feature_playbook")
    feature_inventory = inp.get("feature_inventory") or {}
    docs_digest = _obj("docs_digest")
    # DOC-GROUNDED SEEDS (§adaptation): mine the ingested docs for concrete endpoints, documented
    # authz/tenancy/masking invariants, and high-value feature families (MCP/A2A/remote/gateway/
    # audit/tag/status-events/webhook), and hand the hypothesize LLM explicit probes to instantiate.
    # Without this the docs are a dead-end read — run 05b95dd9 grounded only 1/36 hypotheses in docs.
    doc_surface = docsurf.surface_for_run(str(inp.get("run_id") or cid))
    doc_seeds = doc_surface.get("seeds") or []
    # CROSS-RUN self-learning: prior campaigns' confirmed findings, tried signatures, and lessons for
    # THIS deployment — so round 1 starts warm (don't re-chase exhausted signatures; reuse what worked).
    recalled_confirmed = _list("recalled_confirmed")
    recalled_recipes = _list("recalled_recipes")
    recalled_tried = _list("recalled_tried")
    all_tried = sorted(set(state["all_hypothesis_signatures"]) | set(recalled_tried))
    prior_all = confirmed + [{"title": c.get("title"), "objective_id": c.get("objective_id"),
                              "prior_run": True} for c in recalled_confirmed if isinstance(c, dict)]
    prep = _j.dumps({
        "app_model": app_model, "exploration": obs, "available_identities": identities,
        "personas": personas, "catalog_objectives": [], "cve_leads": cve_leads,
        "mandatory_hypotheses": [], "chaining_context": {},
        "objective_focus": inp.get("objective_focus") or [],
        "object_id_patterns": (app_model.get("object_id_patterns") or []),
        "docs_digest": docs_digest,
        "doc_surface_seeds": doc_seeds,
        "documented_endpoints": [(e.get("method", "") + " " + e.get("path", "")).strip()
                                 for e in (doc_surface.get("endpoints") or [])][:30],
        "sast_findings": sast_findings, "feature_exploitation_playbook": feature_playbook,
        "feature_inventory": feature_inventory,
        "prior_run_recipes": recalled_recipes,
        "tried_signatures": all_tried, "prior_confirmed": prior_all,
        "focus_directive": "You are red-teaming a SYSTEM, not a list of URLs. Form NEW, concrete "
                           "hypotheses from the WHOLE system: the SOURCE CODE findings (sast_findings) "
                           "and how the code is reachable, the DOCS (docs_digest) describing intended "
                           "use and invariants, the FEATURE playbook/inventory (how the system is "
                           "operated — abuse the features, e.g. define+run a workflow that SSRFs/RCEs/"
                           "reads secrets, don't just GET endpoints), the dependency cve_leads, and "
                           "what the TEAM already learned this campaign. prior_run_recipes are techniques "
                           "that worked against THIS deployment before — reuse them. Do NOT repeat any "
                           "signature in tried_signatures; go deeper, chain, or into newly-revealed surface. "
                           "doc_surface_seeds are GROUNDED probes mined from the system's own docs "
                           "(documented endpoints + the authz/tenancy/masking rules guarding them + "
                           "high-value feature families): instantiate each seed that is not already in "
                           "tried_signatures into at least one concrete, oracle-testable hypothesis — these "
                           "are documented surface the team has NOT yet hunted.",
    })
    have_context = bool(obs or sast_findings or cve_leads or docs_digest or feature_inventory
                        or recalled_confirmed or recalled_recipes or doc_seeds)
    return {"prep": prep, "have_observations": have_context, "obs_count": len(obs),
            "sast_count": len(sast_findings), "cve_count": len(cve_leads),
            "recalled_count": len(recalled_confirmed)}


@worker_task(task_definition_name="swarm_absorb_hypotheses", thread_count=4)
def swarm_absorb_hypotheses(task):
    """Post the round's freshly-generated hypotheses to the board as OPEN work (deduped against every
    hypothesis already seen), so the next standup dispatches them. Completes the learning loop."""
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or "")
    hyps = inp.get("hypotheses") if isinstance(inp.get("hypotheses"), list) else []
    if not cid:
        return {"posted": 0, "summary": "no correlation_id"}
    existing = set(bb.assemble_state(cid)["all_hypothesis_signatures"])
    posted = 0
    for h in hyps:
        if not isinstance(h, dict) or not (h.get("title") or h.get("objective_id")):
            continue
        sig = al.signature(h)
        if sig in existing:
            continue
        bb.post_fact(cid, _workflow_id(task, inp), "hypothesis", {**h, "signature": sig})
        existing.add(sig)
        posted += 1
    return {"posted": posted, "summary": f"absorbed {posted} new hypotheses from team learning"}


@worker_task(task_definition_name="swarm_read_board", thread_count=8)
def _peer_digest(cid: str, me: str) -> dict:
    """Build the broadcast peer digest (what teammates found/claimed), excluding this agent."""
    facts = [f for f in bb.read_facts(cid) if str(f.get("author")) != me]
    prims, leads, obs, claims = [], [], [], []
    hyps, candidates, attempt_lessons = [], [], []
    tried = {}  # signature -> (max attempt, lesson)
    for f in facts:
        k, d = f.get("kind"), f.get("data") or {}
        if k == "verified_primitive":
            prims.append(d)
        elif k == "attempt_lesson" and d.get("lesson"):
            # Gap 3: live per-turn lessons from teammates THIS round — the freshest tradecraft.
            area = d.get("area") or d.get("objective_id") or ""
            attempt_lessons.append((f.get("ts") or 0, f"{area}: {d.get('lesson')}"[:200]))
        elif k == "finding" and d.get("confirmed"):
            leads.append(f"CONFIRMED: {d.get('title','')} [{d.get('severity','')}]")
        elif k == "finding" and d.get("refuted") is not True:
            # UNconfirmed candidate: teammates are still working it — corroborate or refute, don't re-find.
            t = d.get("title")
            if t:
                candidates.append(str(t)[:90])
        elif k == "hypothesis" and (d.get("mandatory_kind") in ("emergent_chain", "chain")):
            leads.append(f"CHAIN LEAD: {d.get('title','')} -> {d.get('objective_id','')}")
        elif k == "hypothesis":
            t = d.get("title") or d.get("signature")
            if t:
                hyps.append(str(t)[:90])
        elif k == "attempt":
            sig = d.get("signature")
            if sig:
                try:
                    att = int(d.get("attempt") or 0)
                except (TypeError, ValueError):
                    att = 0
                prev = tried.get(sig, (0, ""))
                tried[sig] = (max(att, prev[0]), str(d.get("lesson") or prev[1] or "")[:120])
        elif k == "observation":
            note = d.get("note") or ((d.get("observations") or [""])[0] if isinstance(d.get("observations"), list) else d.get("note"))
            if note:
                obs.append(str(note)[:160])
        elif k == "claim" and d.get("area"):
            claims.append(str(d.get("area")))
    lines = []
    if prims:
        lines.append("Verified primitives teammates already proved (CHAIN off these, don't re-find): "
                     + "; ".join(f"{p.get('kind')}<-{p.get('source','')}" for p in prims[:8]))
    if leads:
        lines.append("Team leads: " + " | ".join(leads[-6:]))
    if candidates:
        lines.append("Candidate findings under peer review (corroborate with a cleaner probe or move on — don't re-file the same): "
                     + " | ".join(sorted(set(candidates))[:6]))
    if tried:
        # Share what's already been attempted (and its lesson) so teammates build on it instead of
        # repeating the same payload — collaboration that works even when NOTHING is confirmed yet.
        items = sorted(tried.items(), key=lambda kv: -kv[1][0])[:10]
        lines.append("Already attempted by teammates (don't repeat the same payload — go deeper or a new angle): "
                     + " | ".join(f"{s} (x{a}){': '+lsn if lsn else ''}" for s, (a, lsn) in items))
    if hyps:
        lines.append("Hypotheses the team is pursuing (pick a DIFFERENT angle or a deeper variant): "
                     + " | ".join(sorted(set(hyps))[:12]))
    if claims:
        lines.append("Areas teammates are already on (avoid duplicating): " + ", ".join(sorted(set(claims))[:12]))
    if attempt_lessons:
        # Freshest first: what a teammate's probe JUST learned (WAF-bypass that worked, encoding that
        # got through, auth path that 403'd) — the live tradecraft feed that makes this a team.
        recent = [m for _, m in sorted(attempt_lessons, key=lambda x: -x[0])[:8]]
        lines.append("LIVE tradecraft from teammates' latest probes (reuse what worked, skip what didn't): "
                     + " || ".join(recent))
    if obs:
        lines.append("Recent teammate observations: " + " || ".join(obs[-6:]))
    return {"digest": "\n".join(lines), "peer_count": len(facts), "new_primitives": prims[:8],
            "open_leads": leads[-6:], "shared_attempts": len(tried), "shared_hypotheses": len(set(hyps)),
            "live_lessons": len(attempt_lessons)}


def swarm_read_board(task):
    """LIVE peer digest (broadcast context) — what teammates found/claimed/tried so far. No
    ``correlation_id`` -> empty, a no-op for non-swarm runs."""
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or "")
    me = str(inp.get("exclude_author") or _workflow_id(task, inp))
    if not cid:
        return {"available": False, "digest": "", "peer_count": 0, "new_primitives": [], "open_leads": []}
    pd = _peer_digest(cid, me)
    return {"available": bool(pd["digest"]), **pd}


@worker_task(task_definition_name="swarm_inbox", thread_count=8)
def swarm_inbox(task):
    """One call per agent step giving it BOTH the broadcast peer digest AND its DIRECT INBOX --
    messages teammates addressed to it (by workflowId, by its role, or broadcast). This is what turns
    the board into a conversation: the agent reads who said what TO IT and replies by from-id. No
    ``correlation_id`` -> empty no-op (non-swarm)."""
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or "")
    me = str(inp.get("me") or _workflow_id(task, inp))
    role = str(inp.get("role") or "")
    if not cid:
        return {"available": False, "digest": "", "inbox": [], "has_messages": False}
    pd = _peer_digest(cid, me)
    box = bb.read_inbox(cid, me, role, backend=None)
    parts = []
    if box["digest"]:
        parts.append("DIRECT MESSAGES TO YOU (answer questions addressed to you; reply with a message "
                     "action to the sender's from-id):\n" + box["digest"])
    if pd["digest"]:
        parts.append("TEAM BOARD (broadcast context):\n" + pd["digest"])
    return {"available": bool(parts), "digest": "\n\n".join(parts), "inbox": box["inbox"],
            "has_messages": box["available"], "peer_count": pd["peer_count"]}


@worker_task(task_definition_name="swarm_send", thread_count=8)
def swarm_send(task):
    """Send a DIRECT agent-to-agent message. ``to`` = a teammate's workflowId, ``role:<role>``, or
    ``all``. Used by an agent's `message` action to ask a teammate a question, hand off a lead, alert,
    or answer an inbox message (set ``reply_to``/``thread`` from the message being answered)."""
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or "")
    if not cid or not str(inp.get("to") or "").strip():
        return {"sent": "", "error": "need correlation_id and 'to'"}
    mid = bb.send_message(cid, str(inp.get("from") or _workflow_id(task, inp)),
                          str(inp.get("to")), str(inp.get("body") or ""),
                          sender_role=str(inp.get("from_role") or ""),
                          intent=str(inp.get("intent") or "share"),
                          thread=str(inp.get("thread") or ""), reply_to=str(inp.get("reply_to") or ""))
    return {"sent": mid, "to": inp.get("to"), "summary": f"message to {inp.get('to')} ({inp.get('intent','share')})"}


@worker_task(task_definition_name="scribe_rollup", thread_count=2)
def scribe_rollup(task):
    """Roll the campaign board up by correlation_id into the dossier view (coverage, confirmed
    findings, spend) — the campaign-level aggregate the per-node tree does not give at scale."""
    inp = task.input_data or {}
    cid = str(inp.get("correlation_id") or "")
    if not cid:
        return {"rollup": {}, "error": "no correlation_id"}
    state = bb.assemble_state(cid)
    rollup = bb.campaign_rollup(cid)
    confirmed = [f for f in state["findings"] if f.get("confirmed") and f.get("refuted") is not True]
    # Join the EXACT confirming requests the verifier captured (kind "repro" facts, keyed by endpoint)
    # onto the matching findings — the finding fact is assembled downstream and cannot carry it directly.
    repro_by_ep, repro_by_obj = {}, {}
    for fct in bb.read_facts(cid):
        if (fct or {}).get("kind") == "repro":
            d = (fct or {}).get("data") or {}
            rq = d.get("reproduction_request") or {}
            key = repro_mod.endpoint_key(rq.get("method"), rq.get("url") or rq.get("path"))
            if key:
                repro_by_ep[key] = rq  # latest wins
            if d.get("objective_id"):
                repro_by_obj[str(d["objective_id"])] = rq
    # Attach analyst-ready reproduction (steps + curl + validate + fix-verify) to every confirmed
    # finding so the report can render it — findings otherwise carry only a one-line oracle note.
    for f in confirmed:
        try:
            if not f.get("reproduction_request"):
                m, p = repro_mod._endpoint(f)
                hit = (repro_by_ep.get(repro_mod.endpoint_key(m, p))
                       or repro_by_obj.get(str(f.get("objective_id") or "")))
                if hit:
                    f["reproduction_request"] = hit
            f["reproduction_md"] = repro_mod.markdown(f)
            f["reproduction"] = repro_mod.build(f)
        except Exception:
            f["reproduction_md"] = ""
    return {"rollup": rollup, "confirmed_findings": confirmed,
            "verified_primitives": state["verified_primitives"],
            "tried_signatures": state["tried_signatures"],
            "reproduction": [bb.replay_ref(f) for f in confirmed],
            # One-time Prerequisites/setup block prepended to the report's Reproduction section.
            "reproduction_preamble": (repro_mod.preamble() if confirmed else ""),
            "summary": f"rollup: {rollup['confirmed_count']} confirmed, {rollup['coverage_areas']} areas"}
