"""The §19 hill-climbing engine — analytic core (the parts that must be exactly right).

This is the safe, deterministic substance of the self-improving meta-loop: how a recurring
trace signature is diagnosed to a config surface (§19.5 map), how a challenger is accepted
*only* on a statistically-significant, non-regressing held-out gain (§19.5 / D17), how the
search avoids local optima (§19.6), and the hardening that keeps a self-modifying security
component safe (§19.3 H7, §19.8). The LLM *proposer* and the *live* benchmark evaluation are
runtime wiring layered on top; everything here is pure and unit-tested.

Invariants enforced here:
  - the optimizer NEVER trades precision for recall (FP is a protected metric)         [H2/§2]
  - per-class recall may never drop (no silent rare-class forgetting)                   [D17]
  - safety/authz is never a tunable surface; catalog/evidence-bars are ratify-only      [H2/§19.4]
  - changes act on corroborated (multi-trace) signal, statistically gated               [H4/D17]
  - traces are untrusted input to the proposer                                          [H7]
"""

from __future__ import annotations

import math

# Metrics the climb may MAXIMIZE freely vs. PROTECT (never regress) — §19.5.
MAXIMANDS = ("recall", "coverage", "attempt_rate")
PROTECTED = ("fp_rate", "per_class")            # fp must not rise; per-class recall must not drop

# §19.4 optimization-surface policy: who may change what, and how.
SURFACE_MODE = {
    "profile": "auto",          # target-specific, isolated -> benchmark-gated auto-tune
    "prompt": "auto",           # method; auto with multi-target non-regression
    "catalog": "ratify",        # expands what "catastrophic" means -> human judgment
    "evidence_bar": "ratify",   # this IS the truth machinery -> human-ratify only
    "tradecraft": "ratify",     # exploitation ladders + classifier signatures (detection machinery) -> human-ratify
    "safety": "never",          # optimizing safety defeats the design
}


def gate(surface: str) -> str:
    """The promotion mode for a surface: auto | ratify | never (§19.4)."""
    return SURFACE_MODE.get(surface, "never")   # unknown surfaces fail closed


# ── P4-a: diagnosis -> surface (the §19.5 map), read-only proposals ──────────────────

def diagnose(cluster: dict, benchmark: dict | None = None) -> dict | None:
    """Map ONE recurring trace cluster (+ optional benchmark signal) to a config proposal.

    Returns {diagnosis, surface, mode, objective_id, evidence} or None when the failure is
    not the harness's to fix (e.g. a blocked cell -> missing input, surfaced to the operator,
    never tuned around — the §19.5 honesty guard)."""
    obj = cluster.get("objective_id")
    outcome = cluster.get("outcome")
    reason = " ".join(cluster.get("examples") or []).lower()
    bench = benchmark or {}

    # Blocked-for-missing-input is an input gap, not a config defect (§19.5 last row, §9).
    if outcome == "blocked" or "missing identity" in reason or "need a second tenant" in reason:
        return None

    # ── attempt-level signal (richer than verdicts) routes FIRST ─────────────────────────
    sk = cluster.get("signal_kind", "verdict")
    sink = cluster.get("sink_class") or "a sink"
    # A technique ladder was walked to exhaustion without confirmation -> its escalation rungs are
    # too weak for this sink class. Propose ENRICHING THE LADDER (tradecraft, ratify-gated): the
    # ladder is now tunable data, so HC can propose a new rung a human approves.
    if outcome == "exhausted" or sk == "deepen":
        return _p(f"{sink} technique ladder exhausted without confirmation — enrich the ladder with a stronger rung",
                  "tradecraft", obj, cluster)
    # Triage flagged an injection class that exploitation never confirmed -> a classifier/ladder
    # SIGNATURE gap (e.g. a missing DB-error signature). Detection machinery -> human-ratify (H6).
    if sk == "triage":
        return _p(f"triage flagged {cluster.get('triage_class', 'an injection class')} but exploitation never "
                  f"confirmed it — classifier/ladder signature gap", "tradecraft", obj, cluster)
    # Objective explored with too few technique families -> breadth gap in the exploit prompt.
    if sk == "technique_coverage":
        return _p("objective explored with too few technique families — breadth gap", "prompt", obj, cluster)

    # Repeated auth failures -> the profile's auth scheme / token-exchange is wrong (auto).
    if "401" in reason or "token-exchange" in reason or "auth scheme" in reason:
        return _p("profile auth/token-exchange wrong", "profile", obj, cluster)

    # Adapter under-yield (e.g. docs nav-shell only) -> wrong fidelity tier (auto, profile/adapter).
    if "nav-shell" in reason or "low yield" in reason or "wrong tier" in reason:
        return _p("adapter fidelity tier mis-selected", "profile", obj, cluster)

    # Cost / stalls per confirmed finding -> redundant probing / ordering (prompt, auto).
    if "stall" in reason or "redundant" in reason or "too many requests" in reason:
        return _p("redundant probing / poor ordering", "prompt", obj, cluster)

    # Benchmark FALSE POSITIVE / near-miss trap taken -> evidence bar too permissive (RATIFY).
    if obj in set(bench.get("precision_failure_objectives") or []) or "false positive" in reason:
        return _p("evidence bar too permissive for the class", "evidence_bar", obj, cluster)

    # Many hypotheses, zero confirmations -> objective guidance vague (catalog how_to_test, RATIFY).
    if outcome == "inconclusive" and cluster.get("count", 0) >= 2:
        return _p("objective how_to_test is vague / not actionable", "catalog", obj, cluster)

    # Benchmark FALSE NEGATIVE for this objective's class -> technique weak (prompt + catalog).
    if obj in set(bench.get("missed_objectives") or []) or outcome == "rejected":
        return _p("hypothesis breadth / exploit technique weak for the class", "prompt", obj, cluster)

    return None


def _p(diagnosis: str, surface: str, obj, cluster: dict) -> dict:
    return {"diagnosis": diagnosis, "surface": surface, "mode": gate(surface),
            "objective_id": obj, "evidence": (cluster.get("examples") or [])[:3],
            "signature": cluster.get("signature"),
            # controlled attempt-level context the proposer uses to target a tradecraft edit
            "sink_class": cluster.get("sink_class", ""), "triage_class": cluster.get("triage_class", "")}


def propose(recurring_clusters: list, benchmark: dict | None = None) -> list:
    """Read-only analysis pass (P4-a): turn corroborated trace clusters into config proposals.
    Emits PROPOSALS ONLY — no write-back. Drops failures that aren't the harness's to fix.

    Coalesces proposals that target the SAME fix (surface + objective + diagnosis) — e.g. four
    per-family 'sqli ladder exhausted' clusters become ONE 'enrich the sqli ladder' proposal with
    the families' evidence merged — so HC emits one actionable item per ladder, not one per rung."""
    merged: dict = {}
    for c in recurring_clusters or []:
        d = diagnose(c, benchmark)
        if not d:
            continue
        key = (d.get("surface"), d.get("objective_id"), d.get("sink_class", ""), d.get("diagnosis"))
        if key in merged:
            ev = merged[key].get("evidence") or []
            for e in (d.get("evidence") or []):
                if e not in ev and len(ev) < 6:
                    ev.append(e)
            merged[key]["evidence"] = ev
        else:
            merged[key] = d
    return list(merged.values())


def unmeasured_warnings(benchmark: dict) -> list:
    """§19.2/P3-4: classes with no oracle fixture must NOT be auto-tuned — surface them."""
    cov = (benchmark or {}).get("objective_coverage") or {}
    return [{"objective_id": oid, "note": "unmeasured (no benchmark fixture) — not auto-tunable"}
            for oid in cov.get("unmeasured") or []]


# ── P4-b: fitness + statistically-sound acceptance (D17) ─────────────────────────────

def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs):
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def significant(before: list, after: list, *, z: float = 1.96, min_effect: float = 0.0) -> dict:
    """Paired significance of an improvement (§19.5 step 5): the 95% lower confidence bound
    on the per-run delta must exceed ``min_effect``. Not a single lucky run — N paired runs."""
    n = min(len(before), len(after))
    deltas = [after[i] - before[i] for i in range(n)]
    md = _mean(deltas)
    sem = (_stdev(deltas) / math.sqrt(n)) if n > 1 else float("inf")
    lower = md - z * sem
    return {"n": n, "mean_delta": round(md, 4), "lower_bound": round(lower, 4) if sem != float("inf") else None,
            "significant": bool(n > 1 and lower > min_effect)}


def protected_ok(before: dict, after: dict, *, fp_eps: float = 0.005, cost_factor: float = 1.15) -> dict:
    """No protected-metric regression (D17): FP must not rise, no per-class recall may drop,
    cost must not materially worsen. ``before``/``after`` are aggregate score dicts."""
    reasons = []
    if after.get("fp_rate", 0.0) > before.get("fp_rate", 0.0) + fp_eps:
        reasons.append("fp_rate rose")
    bpc, apc = before.get("per_class") or {}, after.get("per_class") or {}
    dropped = [c for c in bpc if apc.get(c, 0.0) < bpc[c] - 1e-9]
    if dropped:
        reasons.append(f"per-class recall dropped: {sorted(dropped)}")
    if after.get("cost", 0.0) > before.get("cost", 0.0) * cost_factor and before.get("cost", 0.0) > 0:
        reasons.append("cost materially worsened")
    return {"ok": not reasons, "reasons": reasons}


def accept(before_runs: list, after_runs: list, *, maximand: str = "recall", min_effect: float = 0.0) -> dict:
    """The full §19.5 acceptance gate: a challenger is accepted ONLY IF its held-out gain on
    the maximand is statistically significant AND it regresses no protected metric. Inputs
    are lists of per-run score dicts (paired). Returns {accept, sig, protected}."""
    bvals = [r.get(maximand, 0.0) for r in before_runs]
    avals = [r.get(maximand, 0.0) for r in after_runs]
    sig = significant(bvals, avals, min_effect=min_effect)
    prot = protected_ok(_agg(before_runs), _agg(after_runs))
    return {"accept": bool(sig["significant"] and prot["ok"]), "sig": sig, "protected": prot}


def _agg(runs: list) -> dict:
    """Aggregate paired runs into a mean score dict (fp_rate, cost, per_class means)."""
    runs = runs or []
    out = {k: _mean([r.get(k, 0.0) for r in runs]) for k in ("recall", "coverage", "attempt_rate", "fp_rate", "cost")}
    classes = {c for r in runs for c in (r.get("per_class") or {})}
    out["per_class"] = {c: _mean([(r.get("per_class") or {}).get(c, 0.0) for r in runs]) for c in classes}
    return out


# ── P4-c: champion / challenger selection + gated write-back ─────────────────────────

def select(champion: dict, challengers: list) -> dict:
    """Choose the next champion from evaluated challengers (each:
    {id, surface, before_runs, after_runs}). A challenger is *acceptable* if it passes the
    §19.5 gate vs the champion. Promotion is then gated by the surface (§19.4): auto-adopt,
    queue for human ratification, or reject. Returns the disposition."""
    promoted, pending, rejected = None, [], []
    scored = []
    for ch in challengers or []:
        verdict = accept(ch.get("before_runs") or [], ch.get("after_runs") or [])
        mode = gate(ch.get("surface"))
        scored.append((ch, verdict, mode))
        if not verdict["accept"] or mode == "never":
            rejected.append({"id": ch.get("id"), "surface": ch.get("surface"),
                             "why": "rejected by gate" if not verdict["accept"] else "surface never tunable"})
        elif mode == "ratify":
            pending.append({"id": ch.get("id"), "surface": ch.get("surface"), "verdict": verdict})
    # auto-promote the best accepted auto-surface challenger (highest significant lower bound)
    auto = [(ch, v) for ch, v, m in scored if v["accept"] and m == "auto"]
    if auto:
        best = max(auto, key=lambda cv: cv[1]["sig"]["lower_bound"] or 0.0)
        promoted = {"id": best[0].get("id"), "surface": best[0].get("surface"), "verdict": best[1]}
    return {"champion": promoted or champion, "promoted": promoted,
            "pending_ratification": pending, "rejected": rejected}


# ── P4-d: search strategy (population / Pareto / bandit / bounded annealing) ──────────

def dominates(a: dict, b: dict) -> bool:
    """Pareto domination over recall↑, coverage↑, cost↓ (a is at least as good on all, better on one)."""
    ge = a.get("recall", 0) >= b.get("recall", 0) and a.get("coverage", 0) >= b.get("coverage", 0) \
        and a.get("cost", 0) <= b.get("cost", 0)
    gt = a.get("recall", 0) > b.get("recall", 0) or a.get("coverage", 0) > b.get("coverage", 0) \
        or a.get("cost", 0) < b.get("cost", 0)
    return ge and gt


def pareto_front(population: list) -> list:
    """The non-dominated configs (§19.6): e.g. a fast-cheap point and a thorough-expensive one,
    instead of collapsing the trade-off to one scalar."""
    return [p for p in population if not any(dominates(q, p) for q in population if q is not p)]


def successive_halving(challengers: list, eval_fn, rounds: int = 2, keep: float = 0.5) -> list:
    """Bandit budget allocation (§19.6): cheaply score all, keep the top fraction, re-score the
    survivors at higher budget. ``eval_fn(challenger, budget) -> float``. Returns survivors,
    best first. Lets obviously-bad challengers die cheap so exploration stays wide."""
    pool = list(challengers or [])
    for r in range(rounds):
        budget = r + 1
        scored = sorted(((eval_fn(c, budget), c) for c in pool), key=lambda sc: -sc[0])
        n = max(1, int(len(scored) * keep)) if r < rounds - 1 else len(scored)
        pool = [c for _, c in scored[:n]]
    return pool


def anneal_accept(delta: float, dim: str, temperature: float) -> bool:
    """Bounded annealing (§19.6): always accept uphill; accept a small downhill move ONLY on an
    UNPROTECTED dimension within the current temperature; NEVER downhill on a protected metric."""
    if delta >= 0:
        return True
    if dim in PROTECTED:
        return False
    return delta > -abs(temperature)


# ── P4-f: hardening (H7, re-baseline, graded autonomy) ───────────────────────────────

def sanitize_trace(rec: dict) -> dict:
    """H7: traces are untrusted input to the proposer. Pass only CONTROLLED fields to the
    LLM proposer and strip/neutralize free text the target could have shaped (it may carry
    injected 'instructions'). The proposer reasons over signatures + bounded reasons, never
    raw target content."""
    safe_reason = str(rec.get("reason") or "")[:200].replace("\n", " ")
    for marker in ("ignore previous", "system:", "you are now", "disregard"):
        if marker in safe_reason.lower():
            safe_reason = "[redacted: possible injection in trace]"
            break
    # Controlled enums/bounded values are safe to pass through (they cannot carry target free-text);
    # only `reason` is scrubbed. This gives the proposer the attempt-level signal (family/sink/etc.).
    return {"objective_id": rec.get("objective_id"), "outcome": rec.get("outcome"),
            "evidence_bar": rec.get("evidence_bar"), "reason": safe_reason,
            "signal_kind": rec.get("signal_kind", "verdict"), "family": rec.get("family", ""),
            "sink_class": rec.get("sink_class", ""), "triage_class": rec.get("triage_class", ""),
            "n_tried": int(rec.get("n_tried") or 0), "exhausted": bool(rec.get("exhausted"))}


def needs_rebaseline(champion_model: str, current_model: str) -> bool:
    """§19.8: a base-model change is a distribution shift — a champion learned under an old
    model is not trusted under a new one; re-baseline before trusting it."""
    return bool(champion_model) and bool(current_model) and champion_model != current_model


def autonomy(blast_radius: str, confidence: float) -> str:
    """Graded autonomy gate (§19.8): blast-radius × held-out confidence -> auto | review | block.
    Small + high-confidence auto-adopts (post-hoc review); high-blast or low-confidence blocks."""
    if blast_radius == "high" or confidence < 0.5:
        return "block"
    if blast_radius == "low" and confidence >= 0.8:
        return "auto"
    return "review"


# ── P4-g: cold-start + cross-deployment transfer ─────────────────────────────────────

def curriculum(fixtures: list) -> list:
    """Cold-start (§19.9): order benchmark fixtures easy→hard so a fresh harness builds signal
    progressively. Heuristic difficulty: relational/chained classes are hardest, single-request
    classes easiest."""
    hard = {"tenancy": 3, "authz": 2, "logic": 2, "infra": 2, "crypto": 2}
    return sorted(fixtures or [], key=lambda f: hard.get(f.get("class"), 1))


def transfer_scope(surface: str) -> str:
    """§19.9 / H3: which editions propagate across deployments. Prompt/catalog/evidence_bar
    learnings are GENERIC (lift every target); profile learnings are PER-TARGET (stay local)."""
    return "per_target" if surface == "profile" else "generic"
