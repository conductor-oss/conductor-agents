"""Corner-case coverage: tail-feature classification, tail-risk scoring, a budget-bounded
scheduling reservation, and tail-coverage accounting (PLAN_V3 Phases 1-3).

Built ON the retained feature inventory (`features.build_inventory`, Phase 0.1): each feature
already carries a `rank`/`tail` tag plus {id, method, path, inputs, sink_hints, source, prio}.
This module adds, per feature:

  - `buckets`: one or more exploration buckets (core, low_traffic, legacy, hidden, admin_debug,
    import_export, integration, alternate_interface);
  - `tail_risk`: a popularity-INDEPENDENT risk score that up-weights rare / undocumented / legacy /
    admin / parser / integration / privileged features, so a neglected route can outrank a popular
    but low-risk one. (`features._prio` scores UP the common cues; this deliberately does not.)

Then:

  - `reserve(...)` (Phase 2): a pure, budget-bounded scheduler that returns which UNTESTED tail
    features to seed hypotheses for this pass. It reserves a configurable share of the explore /
    exploit slots and guarantees bucket diversity, WITHOUT lifting the campaign's global caps
    (constraints C1/C3 — the caller passes the already-bounded slot counts).
  - `tail_coverage(...)` (Phase 3): per-bucket lifecycle counts + the omitted-feature list for the
    residual-risk section. This feeds the REPORT; it is never a completion gate (constraint C1).

All tunables (bucket cues, tail-risk weights, reservation fractions) load through `tradecraft`
data (constraint C2), so the benchmark / hill-climber can move them under the ratify gate rather
than editing hardcoded literals. Pure logic, unit-tested (`tests/test_feature_graph.py`).
"""

from __future__ import annotations

import math
import re

from common import tradecraft

# ───────────────────────── bucket taxonomy (tunable cues) ─────────────────────────
CORE = "core"
LOW_TRAFFIC = "low_traffic"
LEGACY = "legacy"
HIDDEN = "hidden"
ADMIN_DEBUG = "admin_debug"
IMPORT_EXPORT = "import_export"
INTEGRATION = "integration"
ALTERNATE_INTERFACE = "alternate_interface"

# Every bucket except `core` is a "tail" bucket the reservation may pull from.
TAIL_BUCKETS = (LOW_TRAFFIC, LEGACY, HIDDEN, ADMIN_DEBUG, IMPORT_EXPORT, INTEGRATION,
                ALTERNATE_INTERFACE)

# Cue keywords per bucket. Deliberately conservative to keep false-bucketing low; overlaid by the
# YAML key `tail_bucket_cues` (tradecraft.mapping) so the corpus can tune them under ratify.
_BUCKET_CUES_DEFAULT = {
    LEGACY: ("legacy", "deprecated", "/old", "old-", "-old", "compat", "fallback", "obsolete", "/v0"),
    ADMIN_DEBUG: ("admin", "debug", "actuator", "management", "/mgmt", "maintenance", "console",
                  "sysadmin", "superuser", "/internal", "diagnostic", "trace", "heapdump"),
    IMPORT_EXPORT: ("import", "export", "upload", "download", "attachment", "report", "template",
                    "preview", "render", "csv", "xlsx", "xls", "backup", "restore", "document",
                    "parse", "convert", "ingest"),
    INTEGRATION: ("webhook", "callback", "integration", "connector", "oauth", "notify",
                  "subscribe", "outbound", "proxy", "/sync", "/hook", "/fetch", "/event"),
    HIDDEN: ("beta", "experimental", "preview-", "feature-flag", "featureflag", "hidden",
             "unstable", "canary", "x-internal", "flagged"),
}


def _bucket_cues() -> dict:
    return tradecraft.mapping("tail_bucket_cues", _BUCKET_CUES_DEFAULT)


# ───────────────────────── tail-risk weights (tunable, popularity-independent) ─────────────────────────
_RISK_WEIGHTS_DEFAULT = {
    LEGACY: 3.0,              # old code paths, often skipped by tests
    HIDDEN: 3.0,             # source-only / undocumented / feature-flagged
    ADMIN_DEBUG: 2.5,        # privileged side effects, sometimes weaker auth
    INTEGRATION: 2.5,        # SSRF / confused-deputy / secret exposure surface
    IMPORT_EXPORT: 2.0,      # complex parsing / serialization
    ALTERNATE_INTERFACE: 2.0,  # same capability, possibly inconsistent authz
    "source_only": 2.0,      # present in source/docs but never observed live
    "definition_input": 2.0,  # workflow-definition field = privileged side effect
    "sink_hint": 1.0,        # per sink hint (bounded to 3)
    LOW_TRAFFIC: 1.5,        # beyond the popularity cut — the popularity-INDEPENDENT boost
}


def _risk_weights() -> dict:
    return tradecraft.mapping("tail_risk_weights", _RISK_WEIGHTS_DEFAULT)


def _blob(feature: dict) -> str:
    parts = [str(feature.get("method") or ""), str(feature.get("path") or ""),
             str(feature.get("name") or ""), str(feature.get("source") or "")]
    parts += [str(i.get("name") or "") for i in (feature.get("inputs") or []) if isinstance(i, dict)]
    parts += [str(h) for h in (feature.get("sink_hints") or [])]
    return " ".join(parts).lower()


def _observed_live(feature: dict) -> bool:
    """True iff the feature was seen on the running system (crawl/browser), not only in
    source/app-model/docs. Source-only features are prime tail targets."""
    src = {s.strip() for s in str(feature.get("source") or "").split(",") if s.strip()}
    return bool(src & {"surface", "browser"})


def _has_definition_input(feature: dict) -> bool:
    return any(isinstance(i, dict) and i.get("location") == "definition"
              for i in (feature.get("inputs") or []))


def _cue_buckets(blob: str) -> set[str]:
    out: set[str] = set()
    for bucket, cues in _bucket_cues().items():
        if any(str(c).lower() in blob for c in (cues or ())):
            out.add(bucket)
    return out


def classify_one(feature: dict) -> tuple[list[str], float]:
    """Return ``(buckets, tail_risk)`` for one inventory feature. A feature may belong to several
    tail buckets; ``core`` is assigned only when no tail bucket applies."""
    if not isinstance(feature, dict):
        return [CORE], 0.0
    blob = _blob(feature)
    buckets = _cue_buckets(blob)
    if feature.get("tail") is True:
        buckets.add(LOW_TRAFFIC)
    source_only = not _observed_live(feature)
    if source_only:
        buckets.add(HIDDEN)          # source/docs-only == hidden-at-runtime
    if not buckets:
        buckets.add(CORE)

    w = _risk_weights()
    risk = 0.0
    for b in buckets:
        if b == CORE:
            continue
        risk += float(w.get(b, 0.0))
    if source_only:
        risk += float(w.get("source_only", 0.0))
    if _has_definition_input(feature):
        risk += float(w.get("definition_input", 0.0))
    risk += min(len(feature.get("sink_hints") or []), 3) * float(w.get("sink_hint", 0.0))
    return sorted(buckets), round(risk, 3)


_VERSION_RE = re.compile(r"/v(\d+)(?=/|$)", re.I)


def _version_split(path: str) -> tuple[int | None, str]:
    """'/api/v2/workflows' -> (2, '/api//workflows'); no version -> (None, path)."""
    m = _VERSION_RE.search(path or "")
    if not m:
        return None, str(path or "")
    ver = int(m.group(1))
    remainder = (path[:m.start()] + "/" + path[m.end():]).lower()
    return ver, remainder


def _mark_alternate_interfaces(feats: list[dict]) -> None:
    """Graph-level: the same capability reachable through more than one interface or API version is
    an alternate-interface risk (one path may enforce authz the other does not). Detects (a) any
    GraphQL endpoint, and (b) a path remainder that appears under two or more `/vN/` versions — the
    lower version(s) are additionally marked legacy."""
    by_remainder: dict[str, list[tuple[int, dict]]] = {}
    for f in feats:
        path = str(f.get("path") or "")
        if "graphql" in path.lower() or "graphiql" in path.lower():
            f.setdefault("_alt", True)
        ver, rem = _version_split(path)
        if ver is not None:
            by_remainder.setdefault(rem, []).append((ver, f))
    for rem, group in by_remainder.items():
        if len({v for v, _ in group}) < 2:
            continue
        newest = max(v for v, _ in group)
        for ver, f in group:
            f.setdefault("_alt", True)
            if ver < newest:
                f.setdefault("_legacy_version", True)


def build_graph(inventory: list | None) -> dict:
    """Enrich the retained inventory into a tail-aware feature graph: per-feature buckets +
    tail_risk, plus a bucket->ids index and per-bucket counts. Never mutates the input list."""
    feats = [dict(f) for f in (inventory or []) if isinstance(f, dict)]
    _mark_alternate_interfaces(feats)
    for f in feats:
        buckets, risk = classify_one(f)
        bset = set(buckets)
        if f.pop("_alt", False):
            bset.add(ALTERNATE_INTERFACE)
            risk += float(_risk_weights().get(ALTERNATE_INTERFACE, 0.0))
        if f.pop("_legacy_version", False) and LEGACY not in bset:
            bset.add(LEGACY)
            risk += float(_risk_weights().get(LEGACY, 0.0))
        if len(bset - {CORE}) > 0:
            bset.discard(CORE)          # a real bucket supersedes the default 'core'
        f["buckets"] = sorted(bset) or [CORE]
        f["tail_risk"] = round(risk, 3)
    buckets: dict[str, list[str]] = {}
    for f in feats:
        for b in f["buckets"]:
            buckets.setdefault(b, []).append(f["id"])
    counts = {b: len(ids) for b, ids in buckets.items()}
    return {
        "features": feats,
        "buckets": buckets,
        "counts": counts,
        "total": len(feats),
        "tail_total": sum(1 for f in feats if any(b in TAIL_BUCKETS for b in f["buckets"])),
    }


def is_tail(feature: dict) -> bool:
    return any(b in TAIL_BUCKETS for b in (feature.get("buckets") or []))


# ───────────────────────── Phase 2: budget-bounded reservation ─────────────────────────
_RESERVATION_DEFAULT = {
    "explore_fraction": 0.35, "explore_min": 2,
    "exploit_fraction": 0.25, "exploit_min": 1,
}


def reservation_params() -> dict:
    return tradecraft.mapping("tail_reservation", _RESERVATION_DEFAULT)


def _reserve_count(slots: int, fraction: float, minimum: int) -> int:
    """max(minimum, ceil(fraction*slots)) but never more than the slots available (C3)."""
    if slots <= 0:
        return 0
    want = max(int(minimum), math.ceil(fraction * slots))
    return min(slots, want)


def _round_robin_by_bucket(features: list[dict], n: int) -> list[dict]:
    """Pick up to ``n`` features, guaranteeing at least one from each non-empty bucket before any
    bucket is drawn from a second time (the 'nook-and-corner' fairness rule), highest tail_risk
    first within a bucket."""
    if n <= 0 or not features:
        return []
    per_bucket: dict[str, list[dict]] = {}
    for f in features:
        for b in (f.get("buckets") or []):
            if b in TAIL_BUCKETS:
                per_bucket.setdefault(b, []).append(f)
    for b in per_bucket:
        per_bucket[b].sort(key=lambda f: (-float(f.get("tail_risk") or 0), str(f.get("id"))))
    order = sorted(per_bucket, key=lambda b: (-max(float(f.get("tail_risk") or 0)
                                                   for f in per_bucket[b]), b))
    picked: list[dict] = []
    seen: set[str] = set()
    while len(picked) < n and any(per_bucket[b] for b in order):
        for b in order:
            if len(picked) >= n:
                break
            while per_bucket[b]:
                f = per_bucket[b].pop(0)
                if f["id"] in seen:
                    continue
                seen.add(f["id"])
                picked.append(f)
                break
    return picked


def _untested_ids(coverage: dict | None) -> set[str]:
    """Feature ids not yet exercised at all (status 'untested'), from `features.feature_coverage`."""
    per = (coverage or {}).get("per_feature") or []
    return {p.get("id") for p in per if isinstance(p, dict) and p.get("status") == "untested"}


def _exploit_eligible(feature: dict) -> bool:
    """A tail feature the exploit reservation may pull: it has a baseline/source-backed sink
    hypothesis (a sink hint, a class hint, or an injectable workflow-definition field)."""
    return bool(feature.get("sink_hints")) or bool(feature.get("class_hint")) \
        or _has_definition_input(feature)


def reserve(graph: dict | None, coverage: dict | None,
            explore_slots: int, exploit_slots: int,
            covered_ids: set | None = None) -> dict:
    """Return which UNTESTED tail features to schedule this pass.

    ``explore_slots`` / ``exploit_slots`` are the slots the caller has ALREADY budgeted for this
    pass (this function reserves a share of them; it never invents new budget — constraint C1/C3).
    ``covered_ids`` optionally excludes features already scheduled/covered so a repeated pass moves
    on rather than re-picking the same tail features.

    Returns ``{explore: [ids], exploit: [ids], explore_reserve, exploit_reserve,
    untested_tail_buckets: {bucket: count}}``."""
    graph = graph or {}
    feats = [f for f in (graph.get("features") or []) if isinstance(f, dict)]
    untested = _untested_ids(coverage)
    skip = set(covered_ids or set())
    # candidate pool: tail features that are untested and not already covered.
    pool = [f for f in feats if is_tail(f) and f["id"] in untested and f["id"] not in skip]
    p = reservation_params()
    explore_reserve = _reserve_count(explore_slots, float(p.get("explore_fraction", 0.35)),
                                     int(p.get("explore_min", 2)))
    exploit_reserve = _reserve_count(exploit_slots, float(p.get("exploit_fraction", 0.25)),
                                     int(p.get("exploit_min", 1)))
    explore_pick = _round_robin_by_bucket(pool, explore_reserve)
    # exploit reservation draws only from features with a source-backed sink hypothesis.
    exploit_pool = [f for f in pool if _exploit_eligible(f)]
    exploit_pick = _round_robin_by_bucket(exploit_pool, exploit_reserve)
    ubc: dict[str, int] = {}
    for f in pool:
        for b in f.get("buckets") or []:
            if b in TAIL_BUCKETS:
                ubc[b] = ubc.get(b, 0) + 1
    return {
        "explore": [f["id"] for f in explore_pick],
        "exploit": [f["id"] for f in exploit_pick],
        "explore_reserve": explore_reserve,
        "exploit_reserve": exploit_reserve,
        "untested_tail_buckets": ubc,
        "candidate_pool": len(pool),
    }


# ───────────────────────── Phase 3: tail-coverage accounting (report, not gate) ─────────────────────────
# Map the per-feature coverage statuses (features.feature_coverage) onto the PLAN_V3 lifecycle.
# Deliberately conservative: a cheap triage that surfaced nothing is "baseline_exercised", NOT
# "verified_secure" — absence of a triage signal is not proof of security (architecture principle 3).
LIFECYCLE = ("discovered", "reachable", "baseline_exercised", "adversarially_probed",
             "verified_vulnerable", "verified_secure", "blocked", "untested")


def _finding_feature_ids(findings: list | None) -> set[str]:
    return {str(f.get("feature_id")) for f in (findings or [])
            if isinstance(f, dict) and f.get("feature_id")}


def tail_coverage(graph: dict | None, coverage: dict | None,
                  confirmed: list | None = None, rejected: list | None = None) -> dict:
    """Per-bucket lifecycle counts + the omitted (untested/blocked) tail-feature list, for the
    'Corner and Neglected Feature Coverage' report section and the residual-risk statement."""
    graph = graph or {}
    feats = [f for f in (graph.get("features") or []) if isinstance(f, dict)]
    status_by = {p.get("id"): p.get("status")
                 for p in ((coverage or {}).get("per_feature") or []) if isinstance(p, dict)}
    vuln_ids = _finding_feature_ids(confirmed)
    secure_ids = _finding_feature_ids(rejected)

    def lifecycle_of(fid: str) -> str:
        st = status_by.get(fid, "untested")
        if fid in vuln_ids:
            return "verified_vulnerable"
        if st == "deep-exploited":
            return "verified_secure" if fid in secure_ids else "adversarially_probed"
        if st == "signal":
            return "adversarially_probed"
        if st == "triaged-clean":
            return "verified_secure" if fid in secure_ids else "baseline_exercised"
        if st in ("blocked", "out-of-scope", "error"):
            return "blocked"
        return "untested"

    by_bucket: dict[str, dict] = {}
    omitted: list[dict] = []
    for f in feats:
        life = lifecycle_of(f["id"])
        for b in f.get("buckets") or []:
            if b not in TAIL_BUCKETS:
                continue
            row = by_bucket.setdefault(b, {k: 0 for k in LIFECYCLE})
            row["discovered"] += 1
            row[life] = row.get(life, 0) + 1
            if life not in ("untested", "blocked"):
                row["reachable"] += 1
        if life in ("untested", "blocked") and is_tail(f):
            omitted.append({
                "id": f["id"], "method": f.get("method"), "path": f.get("path"),
                "buckets": [b for b in (f.get("buckets") or []) if b in TAIL_BUCKETS],
                "tail_risk": f.get("tail_risk"),
                "reason": ("not reached within budget" if life == "untested"
                           else "reachable/blocked: see per-feature status"),
            })
    omitted.sort(key=lambda o: (-(o.get("tail_risk") or 0), str(o.get("id"))))
    totals = {k: sum(row.get(k, 0) for row in by_bucket.values()) for k in LIFECYCLE}
    return {
        "by_bucket": by_bucket,
        "totals": totals,
        "omitted": omitted[:100],
        "omitted_count": len(omitted),
        "buckets_with_untested": sorted(b for b, row in by_bucket.items() if row.get("untested")),
    }


def residual_sentence(tail_cov: dict | None) -> str:
    """One-sentence residual-risk contribution for the dossier: which tail categories were left
    only discovered/untested. Honest accounting, never a completion gate (constraint C1)."""
    tail_cov = tail_cov or {}
    n = tail_cov.get("omitted_count") or 0
    if not n:
        return ""
    buckets = tail_cov.get("buckets_with_untested") or []
    b = (", ".join(buckets)) if buckets else "tail"
    return (f"{n} neglected/tail feature(s) across {b} were discovered but NOT exercised within "
            f"budget -- absence of findings there is not assurance; see the Corner and Neglected "
            f"Feature Coverage section.")
