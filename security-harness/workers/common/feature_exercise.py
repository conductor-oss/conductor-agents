"""Product-feature exercise tracking and deterministic mandatory hypotheses.

The RCA in ``docs/RCA-product-exploitation-gap.md`` identified a control failure:
prompts asked the agent to operate the product, but the workflow neither measured that
behavior nor prevented completion when it had not happened.  This module is the
deterministic half of the fix.

The operation ledger is produced by the code-exec sandbox and annotated with the
hypothesis/objective that caused each operation.  ``evaluate`` converts that ledger into
machine-readable completion state.  ``mandatory_hypotheses`` emits the still-missing
profile primitives (and the top version-matched CVE) ahead of LLM-proposed hypotheses.
"""

from __future__ import annotations

import re

from common import chaining as chaining_mod
from common import cve_tradecraft as cve_tradecraft_mod
from common import features as features_mod
from common import identity as identity_mod

# Safety cap on how many version-matched CVE leads become forced exploitation hypotheses in a
# single pass. Every reachable lead is attempted (not just the top one); this only bounds a
# pathological lead explosion. Version-matched leads are normally a handful.
MAX_CVE_HYPS = 12

# Safety cap on feature-sweep deep-exploitation hypotheses seeded from triage signals per pass.
MAX_FEATURE_HYPS = 24


def dedupe_operations(operations: list | None) -> list[dict]:
    """Stable-dedupe operation records without retaining response bodies or secrets."""
    out, seen = [], set()
    for raw in operations or []:
        if not isinstance(raw, dict):
            continue
        op = {
            k: raw.get(k)
            for k in (
                "type", "method", "path", "status", "workflow_name", "execution_id",
                "task_types", "objective_id", "hypothesis_id", "cve_id", "dependency",
                "identity", "action", "note", "blocked_reason", "family",
            )
            if raw.get(k) not in (None, "", [])
        }
        op["task_types"] = sorted({str(x).upper() for x in (op.get("task_types") or [])})
        sig = (
            op.get("type"), op.get("method"), op.get("path"), op.get("status"),
            op.get("workflow_name"), op.get("execution_id"),
            tuple(op.get("task_types") or []), op.get("objective_id"),
            op.get("hypothesis_id"), op.get("cve_id"), op.get("identity"), op.get("family"),
        )
        if sig not in seen:
            seen.add(sig)
            out.append(op)
    return out


def _started_workflows(operations: list[dict]) -> dict[str, dict]:
    registered: dict[str, set[str]] = {}
    started: dict[str, dict] = {}
    for op in operations:
        status = op.get("status")
        successful = status is None or (isinstance(status, int) and 200 <= status < 300)
        if not successful:
            continue
        name = str(op.get("workflow_name") or "")
        if op.get("type") == "workflow_registered" and name:
            registered.setdefault(name, set()).update(op.get("task_types") or [])
        if op.get("type") == "workflow_started":
            key = name or str(op.get("execution_id") or "")
            if key:
                started[key] = {**op, "task_types": sorted(registered.get(name, set()))}
    return started


def _objective_exercised(objective_id: str, operations: list[dict]) -> bool:
    started = _started_workflows(operations)
    candidates = [
        op for op in started.values()
        if str(op.get("objective_id") or "") == objective_id
    ]
    if objective_id == "INFRA-SSRF":
        return any({"HTTP", "EVENT"} & set(op.get("task_types") or []) for op in candidates)
    if objective_id == "INFRA-RCE-INJECTION":
        # A recorded active injection attempt (sc.injection_attempt, after a payload was sent
        # to ANY sink) counts -- generic across engines -- as does running the product's own
        # INLINE/expression task. A static SAST report alone never satisfies this.
        if any(op.get("type") == "injection_attempt" for op in (operations or [])):
            return True
        return any("INLINE" in set(op.get("task_types") or []) for op in candidates)
    if objective_id == "CONF-CROSS-TENANT-READ":
        return bool(candidates)
    return bool(candidates)


def _focused_objective_attempted(objective_id: str, operations: list[dict]) -> bool:
    return any(
        op.get("type") == "objective_attempt"
        and str(op.get("objective_id") or "") == objective_id
        for op in operations
    )


# Generic source/SAST signal that a code/expression/template/command/deserialization sink
# exists and must be ACTIVELY exploited (INFRA-RCE-INJECTION), not just reported.
_INJECTION_SINK_HINTS = (
    "inject", "code execution", "rce", "eval(", "scriptengine", "script engine",
    "expression", "spel", "jexl", "ssti", "template inject", "deserial",
    "os command", "command injection", "shell", "pickle", "yaml.load", "exec(",
)


def injection_sinks(sast_findings: list | None) -> list[dict]:
    """Findings (from SAST/source) that name an injection sink — leads the campaign must drive
    to an ACTIVE, OOB-confirmed injection attempt rather than leave as a static 'potential'."""
    out = []
    for f in sast_findings or []:
        if not isinstance(f, dict):
            continue
        blob = " ".join(str(f.get(k) or "") for k in ("title", "evidence", "description", "category")).lower()
        if any(hint in blob for hint in _INJECTION_SINK_HINTS):
            out.append(f)
    return out


# Each SAST injection sink maps to a runtime exploitation CLASS so it gets the right technique
# ladder + oracle (a formatted-SQL sink needs a SQL error/timing oracle, NOT an OOB-exec canary).
# Ordered by specificity — SQL first, since "sql injection" also contains the generic "inject".
_SINK_CLASS_HINTS = (
    ("sqli", ("sql injection", "sqli", "formatted-sql", "formatted sql", "sql statement",
              "tainted sql", "tainted-sql", "jdbc", "hql", "jpql", "prepared statement")),
    ("traversal", ("path traversal", "directory traversal", "zip slip", "zipslip",
                   "file inclusion", "\\blfi\\b", "arbitrary file read")),
    ("ssti", ("ssti", "template inject", "server-side template", "freemarker", "velocity", "thymeleaf")),
    ("command", ("os command", "command injection", "runtime.exec", "processbuilder", "shell")),
    ("deserialization", ("deserial", "objectinputstream", "readobject", "pickle", "yaml.load")),
    ("code-eval", ("scriptengine", "script engine", "code execution", "eval(", "exec(",
                   "spel", "jexl", "groovy", "nashorn", "rhino", "expression")),
)
# Classes covered by the exec-family INFRA-RCE-INJECTION block (OOB-exec canary oracle); the rest
# (data-layer) get their own always-on hypotheses with class-specific oracles.
_EXEC_FAMILY = {"code-eval", "ssti", "command", "deserialization"}


def injection_sink_class(finding: dict) -> str:
    """Classify a SAST injection-sink finding into a runtime exploitation class (sqli / traversal /
    ssti / command / deserialization / code-eval), or "" if it names no recognizable sink."""
    if not isinstance(finding, dict):
        return ""
    blob = " ".join(str(finding.get(k) or "") for k in ("title", "evidence", "description", "category")).lower()
    for klass, hints in _SINK_CLASS_HINTS:
        if any(h in blob for h in hints):
            return klass
    return "code-eval" if "inject" in blob else ""


def _catalog_entry(catalog_objectives: list | None, objective_id: str) -> dict:
    return next(
        (
            entry for entry in (catalog_objectives or [])
            if isinstance(entry, dict) and str(entry.get("id") or "") == objective_id
        ),
        {},
    )


def _focused_block_reason(entry: dict, adequacy: dict, cap: int) -> str:
    required_cap = entry.get("required_capability")
    try:
        if required_cap not in (None, "") and cap < int(required_cap):
            return f"requires capability >={int(required_cap)}"
    except (TypeError, ValueError):
        # Named tiers such as "resilience" are enforced by the action worker and
        # authorization manifest. Leave them pending until an action is attempted.
        pass
    if identity_mod.blocked_by_adequacy(entry.get("required_identities"), adequacy):
        return f"requires identities: {entry.get('required_identities')}"
    return ""


def technique_coverage(operations: list | None, catalog_objectives: list | None = None) -> dict:
    """Per-objective summary of which exploit technique families were attempted, derived ONLY from
    the operation ledger (design/DEEP_EXPLOITATION.md §6). A read-only *summary of what happened* — not
    the rejected next_technique/proximity/exhausted engine. Lets `reflect` reopen a hypothesis that
    tried only one family. Returns {objective_id: {tried_families: [...], n_tried: k}}."""
    by: dict = {}
    for op in operations or []:
        if not isinstance(op, dict):
            continue
        fam = op.get("family")
        if not fam:
            continue
        oid = str(op.get("objective_id") or "other")
        by.setdefault(oid, set()).add(fam)
    return {oid: {"tried_families": sorted(fams), "n_tried": len(fams)} for oid, fams in by.items()}


def _states_coverage(deepen_states: list | None) -> tuple[dict, list]:
    """Per-objective technique families + CVE attempts read from the terminal deepen-state ledgers.
    This SURVIVES operation-ledger truncation: the deepen_state ledger records every family tried
    (and the hypothesis cve_id) independently of the capped operation ledger, so coverage stays
    visible even after the raw ops are tail-sliced. Returns ({objective_id: set(families)}, [cve...])."""
    cov: dict = {}
    cves: list = []
    for st in deepen_states or []:
        if not isinstance(st, dict):
            continue
        oid = str(st.get("objective_id") or "other")
        ledger = st.get("ledger") if isinstance(st.get("ledger"), dict) else {}
        fams = {str(f) for f, slot in ledger.items()
                if isinstance(slot, dict) and (slot.get("tries") or 0) > 0}
        if fams:
            cov.setdefault(oid, set()).update(fams)
        cid = str(st.get("cve_id") or "")
        if cid and fams:
            cves.append({"cve_id": cid, "dependency": str(st.get("dependency") or "")})
    return cov, cves


def evaluate(
    playbook: dict | None,
    operations: list | None,
    cve_leads: list | None,
    adequacy: dict | None,
    capability_max: int | str | None,
    objective_focus: list | None = None,
    catalog_objectives: list | None = None,
    sast_findings: list | None = None,
    deepen_states: list | None = None,
) -> dict:
    """Return the machine-enforced product-feature completion state.

    A requirement is either completed, blocked with a concrete input/authority reason,
    or pending.  ``complete`` means there are no pending requirements; blocked work is
    reported honestly rather than silently treated as tested.
    """
    playbook = playbook if isinstance(playbook, dict) else {}
    ops = dedupe_operations(operations)
    adequacy = adequacy if isinstance(adequacy, dict) else {}
    try:
        cap = int(capability_max)
    except (TypeError, ValueError):
        cap = 0

    required = [str(x) for x in (playbook.get("must_exercise") or []) if x]
    focused = []
    for raw in objective_focus or []:
        objective_id = str(raw or "").strip()
        if objective_id and objective_id not in focused:
            focused.append(objective_id)
    cve_required = bool(cve_leads)
    completed, blocked, pending = [], [], []
    for objective_id in required:
        if _objective_exercised(objective_id, ops):
            completed.append(objective_id)
            continue
        reason = ""
        if cap < 2:
            reason = "requires capability >=2 to define and run a synthetic product flow"
        elif objective_id == "CONF-CROSS-TENANT-READ" and not adequacy.get("cross_tenant"):
            reason = "requires two distinct-tenant identities"
        if reason:
            blocked.append({"id": objective_id, "reason": reason})
        else:
            pending.append(objective_id)

    for objective_id in focused:
        if objective_id in required or (objective_id == "INFRA-SUPPLY-CHAIN" and cve_required):
            continue  # Product-feature requirements keep their stricter evidence rule.
        if objective_id == "INFRA-SUPPLY-CHAIN" and not cve_required:
            blocked.append({
                "id": objective_id,
                "reason": "no reachable version-matched CVE lead was discovered",
            })
            continue
        if _focused_objective_attempted(objective_id, ops):
            completed.append(objective_id)
            continue
        entry = _catalog_entry(catalog_objectives, objective_id)
        reason = _focused_block_reason(entry, adequacy, cap) if entry else ""
        if reason:
            blocked.append({"id": objective_id, "reason": reason})
        else:
            pending.append(objective_id)

    # Coverage from deepen-state ledgers — survives operation-ledger truncation (Phase 2a).
    state_cov, state_cves = _states_coverage(deepen_states)
    cve_attempted = any(op.get("type") == "cve_attempt" for op in ops) or bool(state_cves)
    cve_blocked = ""
    if cve_required and not cve_attempted and cap < 2:
        cve_blocked = "requires capability >=2 for a crafted exploit attempt"
    if cve_required and not cve_attempted and not cve_blocked:
        pending.append("INFRA-SUPPLY-CHAIN")
    elif cve_attempted:
        completed.append("INFRA-SUPPLY-CHAIN")
    elif cve_blocked:
        blocked.append({"id": "INFRA-SUPPLY-CHAIN", "reason": cve_blocked})

    # Source/SAST-flagged injection sinks force an ACTIVE, OOB-confirmed injection attempt
    # (INFRA-RCE-INJECTION) -- a static "potential injection" finding is not assurance.
    sinks = injection_sinks(sast_findings)
    injection_required = bool(sinks) and "INFRA-RCE-INJECTION" not in required and "INFRA-RCE-INJECTION" not in focused
    if injection_required:
        if _objective_exercised("INFRA-RCE-INJECTION", ops):
            completed.append("INFRA-RCE-INJECTION")
        elif cap < 2:
            blocked.append({"id": "INFRA-RCE-INJECTION",
                            "reason": "requires capability >=2 to send an injection payload"})
        else:
            pending.append("INFRA-RCE-INJECTION")

    workflow_defs = [op for op in ops if op.get("type") == "workflow_registered"]
    workflow_runs = list(_started_workflows(ops).values())
    task_types = sorted({t for op in workflow_defs for t in (op.get("task_types") or [])})
    cve_attempts = [op for op in ops if op.get("type") == "cve_attempt"]
    all_required = required + [x for x in focused if x not in required]
    if cve_required:
        all_required.append("INFRA-SUPPLY-CHAIN")
    if injection_required:
        all_required.append("INFRA-RCE-INJECTION")
    all_required = list(dict.fromkeys(all_required))
    completed = list(dict.fromkeys(completed))
    pending = list(dict.fromkeys(pending))
    denominator = len(all_required)
    return {
        "complete": not pending,
        "required": all_required,
        "focused": focused,
        "completed": completed,
        "blocked": blocked,
        "pending": pending,
        "exercise_rate": (len(completed) / denominator) if denominator else 1.0,
        "workflows_defined": len(workflow_defs),
        "workflows_run": len(workflow_runs),
        "workflow_executions": [
            {
                "workflow_name": op.get("workflow_name"),
                "execution_id": op.get("execution_id"),
                "objective_id": op.get("objective_id"),
                "task_types": op.get("task_types") or [],
            }
            for op in workflow_runs
        ],
        "task_types_exercised": task_types,
        "technique_coverage": _merge_coverage(technique_coverage(ops), state_cov),
        "cves_attempted": _dedupe_cves(
            [{"cve_id": op.get("cve_id"), "dependency": op.get("dependency")} for op in cve_attempts]
            + state_cves
        ),
    }


def _merge_coverage(ops_cov: dict, state_cov: dict) -> dict:
    """Union ops-derived technique_coverage with deepen-state-derived families per objective."""
    sets = {oid: set(v.get("tried_families") or []) for oid, v in (ops_cov or {}).items()}
    for oid, fams in (state_cov or {}).items():
        sets.setdefault(oid, set()).update(fams)
    return {oid: {"tried_families": sorted(fams), "n_tried": len(fams)} for oid, fams in sets.items()}


def _dedupe_cves(cves: list) -> list:
    out, seen = [], set()
    for c in cves:
        cid = str((c or {}).get("cve_id") or "")
        if cid and cid not in seen:
            seen.add(cid)
            out.append({"cve_id": cid, "dependency": str(c.get("dependency") or "")})
    return out


def _identity_labels(identities: dict | None) -> list[str]:
    labels = [
        str(k) for k, v in (identities or {}).items()
        if k != "anon" and isinstance(v, dict) and v.get("value")
    ]
    return labels or ["anon"]


_SWEEP_PLAN = {
    "xss": [
        "Confirm the field reflects into the response and in WHICH context (HTML/attribute/JS/URL).",
        "Break out of that context so an injected element/handler parses; defeat any encoding.",
        "If the value is STORED, persist it as one identity then RETRIEVE the resource as a DIFFERENT identity/victim view (stored, cross-user XSS is the prize).",
        "Confirm execution via a DOM effect or an sc.oob() beacon fired from the victim context -- never a bare reflected 200.",
    ],
    "sqli": [
        "Confirm injectability (error/boolean/time-blind), then extract a value: version()/current_user or a sibling/other-tenant row.",
        "Escalate to UNION/stacked/OOB-exfil as the response shape allows. A timing delta vs control or extracted data is the proof; a 200 is not.",
    ],
    "ssti": [
        "Confirm template evaluation with an arithmetic oracle, identify the engine, then escalate toward host access (object access / runtime).",
        "Confirm via the evaluated result or an sc.oob() callback from server-side execution.",
    ],
    "traversal": [
        "Climb out of the intended directory (../, then encoded variants) toward a known file and surface a distinctive line (root:x:0:0 / an app secret).",
        "If reads are blind, confirm via an OOB fetch or a file-open error oracle.",
    ],
    "open-redirect": [
        "Confirm the field controls the redirect target (Location echoes your host); demonstrate redirection to an external/attacker host.",
    ],
    "ssrf": [
        "Define+RUN an outbound-fetch feature pointed inside; walk the FULL internal-target corpus (canonical 127.0.0.1/169.254 to baseline the egress block, then loopback forms incl. [::1], numeric-encoded, IPv6-mapped, AWS IPv6 IMDS [fd00:ec2::254], metadata hosts).",
        "A 403 'blocked in this cluster' on one form is NOT a dead end — a single representation slipping past the filter reaches the internal service. Confirm via a non-403 backend response (actuator/health/IMDS body, internal OpenAPI spec, or a backend INVALID_TOKEN proving traversal) or an OOB hit.",
    ],
}


def feature_sweep_hypotheses(triage_signals: list | None, inventory: list | None,
                             capability_max: int | str | None, identities: dict | None,
                             cap: int = MAX_FEATURE_HYPS) -> list[dict]:
    """Seed one MANDATORY deep-exploitation hypothesis per (feature, injection-class) that surfaced
    during triage -- so the feature-complete sweep drives every flagged input to a confirmed exploit
    or an exhaustively-refuted verdict via exploit_deepen. The hypothesis title/category route it to
    the matching technique ladder; cross-user classes (stored XSS, cross-tenant read) get two
    identities so the high-severity, cross-principal case can be proven."""
    inv = {f.get("id"): f for f in (inventory or []) if isinstance(f, dict)}
    labels = _identity_labels(identities)
    hyps: list[dict] = []
    seen: set = set()
    for sig in (triage_signals or []):
        if not isinstance(sig, dict):
            continue
        fid = sig.get("feature_id")
        cls = str(sig.get("class") or "").lower()
        field = sig.get("field") or ""
        if not fid or not cls:
            continue
        key = (fid, cls)
        if key in seen:
            continue
        seen.add(key)
        feat = inv.get(fid, {"id": fid, "method": "", "path": str(fid)})
        loc = f"{feat.get('method', '')} {feat.get('path', fid)}".strip()
        obj = features_mod.CLASS_OBJECTIVE.get(cls, "INFRA-RCE-INJECTION")
        cross_user = cls in ("xss",)   # stored-XSS confirmation needs a second (victim) identity
        hyps.append({
            "id": f"MAND-SWEEP-{re.sub(r'[^A-Za-z0-9]+', '-', fid)[:48]}-{cls}",
            "title": f"Exploit {cls.upper()} in {loc} (field '{field}') — surfaced during feature triage",
            "objective_id": obj,
            "category": cls,
            "target": f"{loc} :: {field}",
            "rationale": (
                f"Feature triage planted a canary in '{field}' of {loc} and it surfaced as a {cls} "
                f"candidate ({str(sig.get('evidence') or '')[:140]}). A flagged reflection is NOT a "
                f"finding — drive it to a confirmed exploit or exhaustively refute it."
            ),
            "identities": (labels[:2] if cross_user and len(labels) >= 2 else labels[:1]),
            "test_plan": _SWEEP_PLAN.get(cls, _SWEEP_PLAN["sqli"]),
            "expected_evidence": "Demonstrated runtime impact (executed script / extracted data / read file / OOB callback); reflection alone is not confirmation.",
            "blind": False,
            "mandatory": True,
            "mandatory_kind": "feature_sweep",
            "feature_id": fid,
            "field": field,
            "sweep_class": cls,
        })
        if len(hyps) >= cap:
            break
    # Engine workflow-definition fields (INLINE.expression, HTTP.uri, ...): these can't be
    # host-triaged (they need define+run), so seed a deep probe directly for each.
    labels2 = labels
    for feat in (inventory or []):
        if not isinstance(feat, dict) or len(hyps) >= cap:
            continue
        cls = str(feat.get("class_hint") or "").lower()
        if not cls or not any(i.get("location") == "definition" for i in feat.get("inputs", [])):
            continue
        fid = feat.get("id")
        field = next((i.get("name") for i in feat.get("inputs", []) if i.get("location") == "definition"), "payload")
        tt = feat.get("path") or feat.get("name") or fid
        if (fid, cls) in seen:
            continue
        seen.add((fid, cls))
        obj = features_mod.CLASS_OBJECTIVE.get(cls, "INFRA-RCE-INJECTION")
        hyps.append({
            "id": f"MAND-SWEEP-{re.sub(r'[^A-Za-z0-9]+', '-', str(fid))[:48]}-{cls}",
            "title": f"Exploit {cls.upper()} via the {tt} task '{field}' workflow-definition field",
            "objective_id": obj,
            "category": cls,
            "target": f"workflow-definition {tt}.{field}",
            "rationale": (
                f"An orchestration engine's real injection surface is workflow-definition content: "
                f"the {tt} task's '{field}' field. Define+run a workflow carrying a payload there and "
                f"drive it to a confirmed exploit or exhaustively refute."
            ),
            "identities": labels2[:1],
            "test_plan": [
                f"Define a minimal workflow with a {tt} task whose '{field}' carries the payload "
                f"(for INLINE/expression: code that evaluates/breaks out; for HTTP/EVENT: an sc.oob() URL).",
                "REGISTER the workflow then START an execution — config-only does NOT confirm, you must RUN it.",
                "Read the execution/task output and the OOB collaborator; confirm via evaluated output or an "
                "inbound canary hit, escalating the ladder until impact or exhaustion.",
            ],
            "expected_evidence": "Evaluated/executed output or an OOB callback from the running workflow; a created definition is not confirmation.",
            "blind": False,
            "mandatory": True,
            "mandatory_kind": "feature_sweep",
            "feature_id": fid,
            "field": field,
            "sweep_class": cls,
        })
    return hyps


def _primitive_for(playbook: dict, objective_id: str) -> dict:
    for primitive in playbook.get("primitives") or []:
        if isinstance(primitive, dict) and primitive.get("objective") == objective_id:
            return primitive
    return {}


def mandatory_hypotheses(
    playbook: dict | None,
    operations: list | None,
    cve_leads: list | None,
    adequacy: dict | None,
    capability_max: int | str | None,
    identities: dict | None,
    objective_focus: list | None = None,
    catalog_objectives: list | None = None,
    sast_findings: list | None = None,
    prior_confirmed: list | None = None,
) -> list[dict]:
    """Build deterministic hypotheses for every still-pending mandatory exercise."""
    playbook = playbook if isinstance(playbook, dict) else {}
    status = evaluate(
        playbook,
        operations,
        cve_leads,
        adequacy,
        capability_max,
        objective_focus,
        catalog_objectives,
        sast_findings,
    )
    labels = _identity_labels(identities)
    sinks = injection_sinks(sast_findings)
    exec_sinks = [s for s in sinks if injection_sink_class(s) in _EXEC_FAMILY]
    hypotheses: list[dict] = []

    for objective_id in status["pending"]:
        if objective_id == "INFRA-RCE-INJECTION" and exec_sinks:
            sink_list = "; ".join((s.get("title") or s.get("location") or "injection sink")[:80] for s in exec_sinks[:3])
            hypotheses.append({
                "id": "MAND-INJECTION",
                "title": f"Actively exploit SAST-flagged injection sink(s): {sink_list}",
                "objective_id": "INFRA-RCE-INJECTION",
                "category": "injection",
                "target": sink_list,
                "rationale": "A source/SAST finding flags a code/expression/template/command/deserialization sink. A static 'potential injection' is not assurance -- it must be actively exploited with an OOB canary or reported blocked.",
                "identities": labels[:1],
                "test_plan": [
                    f"Find how attacker-controlled input reaches the flagged sink: {sink_list}.",
                    "Mint canary = sc.oob('injection'); craft a payload whose EXECUTED code fetches the canary (JS fetch / SpEL URL / shell curl / SSTI / deserialization gadget) -- generic to the sink's engine.",
                    "Deliver the payload through the sink as a low-privilege identity (via the product's inline/script/expression task, or directly into the vulnerable field).",
                    "Call sc.injection_attempt(sink, 'payload issued'); confirm via the OOB inbound hit (decisive for blind) or an in-band exec/error oracle.",
                ],
                "expected_evidence": "An OOB canary hit from the target (server-side execution), or a concrete in-band exec/error oracle -- never a 200 or a reflected echo.",
                "blind": False,
                "mandatory": True,
                "mandatory_kind": "sast_injection",
            })
            continue
        if objective_id == "INFRA-SUPPLY-CHAIN":
            # Attempt EVERY version-matched lead (not just the top one) — one deep-exploitation
            # hypothesis per CVE, highest-priority first, bounded by MAX_CVE_HYPS as a safety cap.
            leads = sorted(
                [x for x in (cve_leads or []) if isinstance(x, dict)],
                key=lambda x: float(x.get("priority_score") or 0),
                reverse=True,
            )[:MAX_CVE_HYPS]
            for lead in leads:
                cve = (lead.get("top_cves") or [{}])[0]
                cid = cve.get("id") or "version-matched CVE"
                dep = lead.get("dependency") or "dependency"
                # The concrete HOW: a curated/runtime exploit hint (class + technique + oracle) so the
                # agent issues a real exploit instead of synthesizing one from the CVE id alone.
                summary = str(cve.get("summary") or cve.get("details") or "")
                hint = cve_tradecraft_mod.hint(cid, dep, summary)
                hint_line = cve_tradecraft_mod.hint_line(cid, dep, summary)
                hypotheses.append({
                    "id": f"MAND-CVE-{re.sub(r'[^A-Za-z0-9]+', '-', str(cid))}",
                    "title": f"Attempt {cid} against reachable {dep}",
                    "objective_id": "INFRA-SUPPLY-CHAIN",
                    "category": "cve",
                    "target": f"{dep} / {cid}",
                    "rationale": f"A version-matched CVE lead — {hint_line}. Every reachable lead is driven (not only the top one) and kept trying across the CVE escalation ladder until impact is shown or the ladder is exhausted.",
                    "identities": labels[:1],
                    "exploit_hint": hint,
                    "cve_class": hint.get("class"),
                    "test_plan": [
                        f"Issue the actual exploit, do NOT just report the version. {hint_line}",
                        "Map the reachable feature/endpoint that exercises this dependency (HTTP uri/body, INLINE expression, EVENT/WEBHOOK sink, import/upload), then use run_code to deliver the payload as a low-priv identity.",
                        f"After issuing EACH payload call sc.cve_attempt('{cid}', '{dep}', 'crafted payload issued', family='<published-poc|payload-variant|alternate-vector|chain-precondition|oob-confirm>').",
                        "Escalate the CVE ladder (do not stop at the first bounce) until the ORACLE above fires or every rung is exhausted. Dependency/version presence alone is NOT confirmation.",
                    ],
                    "expected_evidence": f"{hint.get('oracle')} — dependency/version presence alone is not confirmation.",
                    "blind": False,
                    "mandatory": True,
                    "mandatory_kind": "cve",
                    "cve_id": cid,
                    "dependency": dep,
                })
            continue

        primitive = _primitive_for(playbook, objective_id)
        catalog_entry = _catalog_entry(catalog_objectives, objective_id)
        how = (
            primitive.get("how")
            or catalog_entry.get("how_to_test")
            or f"Exercise objective {objective_id} end to end."
        )
        category = (
            "ssrf" if objective_id == "INFRA-SSRF"
            else "injection" if objective_id == "INFRA-RCE-INJECTION"
            else "bola" if objective_id == "CONF-CROSS-TENANT-READ"
            else catalog_entry.get("class") or "other"
        )
        is_product_feature = bool(primitive)
        hypotheses.append({
            "id": f"MAND-{objective_id}",
            "title": (
                f"Mandatory product-feature exercise: {primitive.get('abuse') or objective_id}"
                if is_product_feature
                else f"Focused objective: {catalog_entry.get('objective') or objective_id}"
            ),
            "objective_id": objective_id,
            "category": category,
            "target": primitive.get("task_type") or objective_id,
            "rationale": (
                "The campaign cannot claim product-level depth until this applicable primary feature is defined, run, and observed."
                if is_product_feature
                else "The operator explicitly pinned this objective; it must be attempted or reported blocked."
            ),
            "identities": (
                labels[:2]
                if identity_mod.required_key(catalog_entry.get("required_identities")) in ("cross_user", "cross_tenant")
                or objective_id == "CONF-CROSS-TENANT-READ"
                else labels[:1]
            ),
            "test_plan": (
                [how, "Use run_code; capture workflow definition name, execution id, task types, and resulting evidence."]
                if is_product_feature
                else [how, "Record the exact action, identity, result, and evidence needed to confirm or reject the objective."]
            ),
            "expected_evidence": (
                "A registered and started workflow execution, plus the class-specific side effect or negative result."
                if is_product_feature
                else catalog_entry.get("impact_evidence") or "Concrete impact evidence or a reproducible negative result."
            ),
            "blind": False,
            "mandatory": True,
            "mandatory_kind": "product_feature" if is_product_feature else "focused_objective",
        })

    # DATA-LAYER SAST sinks (SQLi / traversal) get their OWN always-on runtime hypothesis — the
    # INFRA-RCE-INJECTION exec-family block above neither covers them (wrong oracle: a SQL sink needs
    # an error/boolean/time-based oracle, not an OOB-exec canary) NOR fires once that objective is
    # marked complete by the JS/eval testing. A static 'potential SQLi' is not assurance: it must be
    # driven to a runtime oracle through the live endpoint reaching the sink, or honestly downgraded.
    # category routes the hypothesis to exploit_deepen's matching technique ladder (sqli / traversal).
    hypotheses.extend(_data_layer_sink_hypotheses(sinks, labels))

    # Predictive mid-run CHAINING (§5): each confirmed finding's unlocked precondition becomes a
    # FORCED hypothesis that USES the confirmed material to escalate (cross-tenant write, secret reuse,
    # privilege->engine compromise) — so the campaign pivots instead of stopping at the first win.
    existing_ids = {h.get("id") for h in hypotheses}
    for h in chaining_mod.chained_hypotheses(prior_confirmed, labels):
        if h.get("id") not in existing_ids:
            hypotheses.append(h)
    return hypotheses


def _data_layer_sink_hypotheses(sinks: list, labels: list) -> list[dict]:
    out, seen = [], set()
    for s in sinks:
        klass = injection_sink_class(s)
        if klass not in ("sqli", "traversal") or klass in seen:
            continue
        seen.add(klass)
        sink_list = "; ".join((x.get("title") or x.get("location") or f"{klass} sink")[:80]
                              for x in sinks if injection_sink_class(x) == klass)[:240]
        out.append(_SINK_HYPO_BUILDERS[klass](sink_list, labels[:1]))
    return out


def _sqli_hypothesis(sink_list: str, ids: list) -> dict:
    return {
        "id": "MAND-SQLI",
        "title": f"Actively exploit SAST-flagged SQL injection sink(s): {sink_list}",
        "objective_id": "INFRA-RCE-INJECTION",
        "category": "sqli",                       # -> exploit_deepen, sqli technique ladder
        "target": sink_list,
        "rationale": "A SAST finding flags formatted/concatenated SQL in a data-access path. A static "
                     "'potential SQLi' is NOT assurance — it must be driven to a runtime SQL oracle "
                     "(error/boolean/time-based) through the live endpoint that reaches the sink, or "
                     "the finding downgraded (no runtime PoC => not High).",
        "identities": ids,
        "test_plan": [
            "Map the flagged DAO/method to the LIVE endpoint(s) whose request parameters reach it "
            "(e.g. workflow/archive/audit search: freeText/query/name/correlationId/date/partition/"
            "sort selectors); enumerate candidates from the discovered API surface.",
            "Inject SQL metacharacters (' and \") into each candidate parameter and watch for a 500 / "
            "DB error string (the sqli ladder's error-based rung).",
            "Escalate the ladder: boolean-based (' OR '1'='1 vs ' AND '1'='2 response differential), "
            "then time-based (pg_sleep/SLEEP/WAITFOR delay differential) to confirm BLIND injection.",
            "Confirm via a DB error reflection, a boolean/time differential, or extracted data — never "
            "a 200 or a reflected echo. If no rung confirms, the SAST finding must be downgraded.",
        ],
        "expected_evidence": "A SQL error string (SQLSTATE / ORA- / 'syntax error near' / PSQLException), "
                             "a boolean/time-based response differential, or extracted data — not a 200.",
        "blind": False,
        "mandatory": True,
        "mandatory_kind": "sast_injection",
    }


def _traversal_hypothesis(sink_list: str, ids: list) -> dict:
    return {
        "id": "MAND-TRAVERSAL",
        "title": f"Actively exploit SAST-flagged path-traversal sink(s): {sink_list}",
        "objective_id": "INFRA-PATH-TRAVERSAL",
        "category": "traversal",                  # -> exploit_deepen, traversal ladder
        "target": sink_list,
        "rationale": "A SAST finding flags a file path built from input. A static 'potential traversal' "
                     "is not assurance — drive it to a runtime read of an out-of-tree file through the "
                     "live endpoint, or downgrade the finding.",
        "identities": ids,
        "test_plan": [
            "Map the flagged file/path API to the LIVE endpoint + parameter that reaches it "
            "(upload/download/import/export/proto/document-store paths).",
            "Walk the traversal ladder: ../ sequences, encoded (%2e%2e%2f, double-encode), null-byte, "
            "absolute paths, then leading-slash and UNC variants.",
            "Confirm by reading a known out-of-tree file (e.g. /etc/passwd, a sibling tenant's file) "
            "returned in the response — never a 200 alone.",
        ],
        "expected_evidence": "Contents of a file outside the intended directory returned in the response.",
        "blind": False,
        "mandatory": True,
        "mandatory_kind": "sast_injection",
    }


_SINK_HYPO_BUILDERS = {"sqli": _sqli_hypothesis, "traversal": _traversal_hypothesis}


def focus_directive(status: dict, chaining_context: dict | None = None) -> str:
    """Deterministic next-pass focus that cannot be erased by the reflection LLM."""
    parts = []
    pending = status.get("pending") or []
    if pending:
        parts.append(
            "Mandatory incomplete objectives: "
            + ", ".join(pending)
            + ". Use run_code to define, start, and poll the product flow; record execution IDs."
        )
    unlocked = (chaining_context or {}).get("unlocked_objectives") or []
    if unlocked:
        parts.append("Chain confirmed pivots into: " + ", ".join(unlocked) + ".")
    return " ".join(parts)
