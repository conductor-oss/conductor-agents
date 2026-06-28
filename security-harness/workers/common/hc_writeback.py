"""The §19 hill-climbing WRITE-BACK loop (Phase 4c) — gated champion/challenger promotion.

This is the half that was missing: `hillclimb` *decides* (accept/gate/autonomy) and
`config_lineage` *versions* (content-addressed, rollback-able); this module ties them together
into an actual, safe write-back — turn a proposal into a challenger config edition, gate it, and
either auto-adopt it (commit a new champion edition), queue it for human ratification, reject it,
or demand a re-baseline.

The iron rule (design §19, IMPLEMENTATION_PLAN sequencing): **write-back is gated on an ADEQUATE
oracle.** Auto-adoption requires (a) the objective's class is *measured* by the benchmark, and
(b) a real held-out split (≥2 scored targets) — else a "gain" is unmeasurable and self-tuning would
degrade the engine (D16/H1). When the oracle is inadequate, an otherwise-auto change is *downgraded
to human ratification*, never silently applied. Safety/authz is never a surface (H2). All decisions
are statistical + non-regressing (D17), act on corroborated multi-trace signal (H4), and every
applied edition is a provenanced, reversible lineage record (H5).

Pure logic (clock injected as ``as_of``); the real benchmark eval + LLM proposer + file writes are
the runner's job (``bench/hc_writeback.py``). Unit-tested with stubs.
"""

from __future__ import annotations

from . import config_lineage, hillclimb, trace

AUTO_SURFACES = ("profile", "prompt")        # §19.4 auto-tune (benchmark-gated)
RATIFY_SURFACES = ("catalog", "evidence_bar", "tradecraft")  # §19.4 human-ratify only (tradecraft = ladders + classifier signatures)


def _blast(surface: str) -> str:
    return "low" if surface in AUTO_SURFACES else "high"


def oracle_adequate(benchmark: dict | None, surface: str, objective_id: str | None) -> tuple[bool, str]:
    """The D16/§19.2 gate: may the oracle support AUTO-adoption of a change for this objective?

    Requires the class to be measured (a fixture exists) and a genuine held-out split (≥2 scored
    targets). Returns (ok, reason-when-not). ``benchmark`` = {objective_coverage:{unmeasured:[...]},
    scored_targets:int, name:str}."""
    b = benchmark or {}
    unmeasured = set((b.get("objective_coverage") or {}).get("unmeasured") or [])
    if objective_id and objective_id in unmeasured:
        return False, f"objective {objective_id} is UNMEASURED by the benchmark — HC must not auto-tune it (§19.2)"
    scored = int(b.get("scored_targets") or 0)
    if scored < 2:
        return False, f"held-out split needs >=2 scored targets (have {scored}) — a held-out gain is not measurable (§19.2/D16)"
    return True, ""


def decide(challenger: dict, *, champion_model: str, current_model: str, benchmark: dict | None) -> dict:
    """Disposition for one evaluated challenger ({id, surface, path, objective_id, diagnosis,
    content, before_runs, after_runs}). Returns {disposition, reason, verdict} where disposition ∈
    {auto_adopt, ratify, reject, rebaseline}. Composes the §19 gates in order."""
    surface = challenger.get("surface")
    if surface not in config_lineage.SURFACES:  # safety/authz/unknown -> never tunable (H2)
        return {"disposition": "reject", "reason": f"surface {surface!r} is not tunable (never auto-modified, H2)", "verdict": None}

    if hillclimb.needs_rebaseline(champion_model, current_model):
        return {"disposition": "rebaseline",
                "reason": f"base model changed {champion_model}->{current_model}; re-evaluate before trusting (§19.8)",
                "verdict": None}

    verdict = hillclimb.accept(challenger.get("before_runs") or [], challenger.get("after_runs") or [])
    if not verdict["accept"]:
        why = "not statistically significant over the held-out split" if not verdict["sig"]["significant"] \
            else "regressed a protected metric: " + "; ".join(verdict["protected"]["reasons"])
        return {"disposition": "reject", "reason": f"benchmark gate failed — {why} (D17)", "verdict": verdict}

    gate = hillclimb.gate(surface)                 # auto | ratify | never
    adq, adq_reason = oracle_adequate(benchmark, surface, challenger.get("objective_id"))
    conf = 0.9 if verdict["sig"]["significant"] else 0.3
    auto = hillclimb.autonomy(_blast(surface), conf)   # auto | review | block

    if gate == "auto" and adq and auto == "auto":
        return {"disposition": "auto_adopt",
                "reason": "significant held-out gain, no protected-metric regression, measured class, low blast-radius",
                "verdict": verdict}
    if gate != "auto":
        reason = f"{surface} is a human-ratify surface (§19.4) — propose, never auto-adopt"
    elif not adq:
        reason = f"oracle inadequate for auto-adopt: {adq_reason}"
    else:
        reason = f"graded-autonomy gate returned {auto!r} (blast={_blast(surface)}) — human review"
    return {"disposition": "ratify", "reason": reason, "verdict": verdict}


def _scores(ch: dict) -> dict:
    def agg(runs):
        runs = runs or []
        n = len(runs) or 1
        return {k: round(sum(r.get(k, 0.0) for r in runs) / n, 4) for k in ("recall", "fp_rate")}
    return {"before": agg(ch.get("before_runs")), "after": agg(ch.get("after_runs"))}


def promote(store: list, challengers: list, *, champion_model: str, current_model: str,
            benchmark: dict | None, as_of: str, shadow: bool = True) -> dict:
    """Run the gated write-back over evaluated challengers. Auto-adopted ones are committed as a new
    (shadow) lineage edition via ``config_lineage`` (retaining the prior for rollback, H5); the rest
    are queued for ratification, flagged for re-baseline, or rejected. Mutates ``store``."""
    out = {"promoted": [], "pending_ratification": [], "rejected": [], "rebaseline_required": []}
    for ch in challengers or []:
        d = decide(ch, champion_model=champion_model, current_model=current_model, benchmark=benchmark)
        item = {"id": ch.get("id"), "surface": ch.get("surface"), "path": ch.get("path"),
                "objective_id": ch.get("objective_id"), "reason": d["reason"]}
        if d["disposition"] == "auto_adopt":
            rec = config_lineage.commit(
                store, surface=ch["surface"], path=ch["path"], content=ch.get("content") or "",
                as_of=as_of, model=current_model, benchmark=(benchmark or {}).get("name"),
                provenance={"diagnosis": ch.get("diagnosis"), "proposal_id": ch.get("id"), "shadow": shadow},
                scores=_scores(ch))
            out["promoted"].append({**item, "version": rec["version"], "content_hash": rec["content_hash"],
                                    "shadow": shadow, "why": config_lineage.why(rec)})
        elif d["disposition"] == "ratify":
            out["pending_ratification"].append(item)
        elif d["disposition"] == "rebaseline":
            out["rebaseline_required"].append(item)
        else:
            out["rejected"].append(item)
    return out


def _eval_overlay(eval_fn, overlay: dict | None, targets: list, n_runs: int) -> list:
    """Produce ``n_runs`` paired run-metric dicts for one config (``overlay`` = None means the
    current champion; a dict {surface,path,content} means the challenger edition) across every
    held-out target. ``eval_fn(overlay, target) -> {recall, fp_rate, per_class, cost}`` is
    injected (the live scan+score substrate, or a stub in tests)."""
    runs = []
    for target in targets or []:
        for _ in range(max(1, n_runs)):
            m = eval_fn(overlay, target)
            if m:
                runs.append(m)
    return runs


def run_cycle(store: list, traces: list, *, propose_fn, eval_fn, surface_path,
              champion_model: str, current_model: str, benchmark: dict | None, as_of: str,
              holdout_targets: list, n_runs: int = 3, shadow: bool = True) -> dict:
    """The full §19 write-back CYCLE — the loop that was only *projected* before.

    traces -> sanitize (H7) -> recurring (H4) -> diagnose->surface proposals -> for each: resolve
    the surface file (``surface_path``), ask the proposer for the edited ``content``
    (``propose_fn(proposal, path) -> str|None``; None = nothing safely editable, e.g. no TACTICS
    region), then EVALUATE the challenger against the champion on the held-out split
    (``eval_fn``, paired before/after) and run the gated ``promote``. The champion baseline is
    evaluated ONCE and reused across proposals (paired comparison, §19.2 held-out).

    Pure given the injected ``propose_fn``/``eval_fn``/``surface_path`` (unit-tested with stubs);
    the runner wires the real proposer + live scan eval (``bench/hc_writeback.py``). Returns the
    ``promote`` result augmented with ``proposals`` (count) and ``skipped`` (with reasons)."""
    recs = [hillclimb.sanitize_trace(r) for r in traces or []]      # H7: untrusted traces
    recurring = trace.recurring(recs, min_count=2)                  # H4: corroborated only
    proposals = hillclimb.propose(recurring, benchmark)
    skipped = []
    if not proposals:
        return {"promoted": [], "pending_ratification": [], "rejected": [],
                "rebaseline_required": [], "proposals": 0, "skipped": skipped}

    baseline = _eval_overlay(eval_fn, None, holdout_targets, n_runs)  # champion, evaluated once
    challengers = []
    for i, p in enumerate(proposals):
        path = surface_path(p["surface"], p.get("objective_id"))
        if not path:
            skipped.append({"surface": p["surface"], "objective_id": p.get("objective_id"),
                            "why": "no config file maps to this surface/objective"})
            continue
        content = propose_fn(p, path)
        if not content:
            skipped.append({"surface": p["surface"], "objective_id": p.get("objective_id"),
                            "why": "proposer produced no edit (no editable region / no LLM)"})
            continue
        after = _eval_overlay(eval_fn, {"surface": p["surface"], "path": path, "content": content},
                              holdout_targets, n_runs)
        challengers.append({"id": p.get("signature") or f"prop-{i}", "surface": p["surface"],
                            "path": path, "objective_id": p.get("objective_id"),
                            "diagnosis": p.get("diagnosis"), "content": content,
                            "before_runs": baseline, "after_runs": after})

    out = promote(store, challengers, champion_model=champion_model, current_model=current_model,
                  benchmark=benchmark, as_of=as_of, shadow=shadow)
    out["proposals"] = len(proposals)
    out["skipped"] = skipped
    return out


def apply_ratified(store: list, item: dict, content: str, *, current_model: str,
                   benchmark: dict | None, as_of: str, shadow: bool = False) -> dict:
    """Commit a human-RATIFIED challenger (catalog/evidence-bar surfaces, or an oracle-downgraded
    one). The human is the gate (H6); this records the edition with that provenance."""
    return config_lineage.commit(
        store, surface=item["surface"], path=item["path"], content=content or "",
        as_of=as_of, model=current_model, benchmark=(benchmark or {}).get("name"),
        provenance={"diagnosis": item.get("reason"), "ratified": True, "shadow": shadow}, scores={})


def should_rollback(before_runs: list, after_runs: list) -> tuple[bool, list]:
    """A live edition has regressed (→ auto-rollback, H5) if re-evaluation now fails the
    protected-metric gate vs its baseline (FP up / per-class recall down / cost up)."""
    prot = hillclimb.protected_ok(hillclimb._agg(before_runs), hillclimb._agg(after_runs))
    return (not prot["ok"]), prot["reasons"]


def rollback_plan(store: list, surface: str, path: str) -> dict | None:
    """The edition to revert to on regression (H5). The runner restores that content
    (content-addressed) and commits the revert. None if there is no prior edition."""
    return config_lineage.rollback_target(store, surface, path)


def activate(store: list, surface: str, path: str, content: str, *, current_model: str,
             benchmark: dict | None, as_of: str) -> dict:
    """Promote a SHADOW edition to live (§19.8 shadow promotion): after it has gathered live
    evidence, commit a non-shadow edition of the same content so subsequent runs load it."""
    return config_lineage.commit(
        store, surface=surface, path=path, content=content or "", as_of=as_of, model=current_model,
        benchmark=(benchmark or {}).get("name"), provenance={"shadow": False, "activated": True}, scores={})
