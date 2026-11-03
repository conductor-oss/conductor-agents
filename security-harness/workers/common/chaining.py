"""Incremental attack-graph chaining (§5 / §8, P2-1).

Strategic pentesting is a kill chain: a CONFIRMED finding's output (a credential, a tenant
id, internal reach) becomes a PRECONDITION that seeds deeper, multi-step hypotheses on the
next pass — and the attack graph is assembled *during* the loop, not only at report time.
This module is the pure logic the reflect→hypothesize feed and the dossier graph build on.
"""

from __future__ import annotations

# A confirmed finding (by category/objective/class) unlocks these preconditions.
_UNLOCKS = {
    "ssrf": ["internal_reach"], "infra-ssrf": ["internal_reach"],
    "secret": ["credential_held"], "secret_exposure": ["credential_held"],
    "infra-secret-surface": ["credential_held"], "conf-excessive-data": ["credential_held", "internal_detail"],
    "idor": ["other_object_ref"], "bola": ["other_object_ref"], "conf-bola-cross-user": ["other_object_ref"],
    "cross-tenant": ["other_tenant_ref"], "conf-cross-tenant-read": ["other_tenant_ref"],
    "auth": ["authenticated_as_victim"], "auth-takeover": ["authenticated_as_victim"],
    "info_exposure": ["internal_detail"],
    "privesc": ["privileged_access"], "integ-privesc-massassign": ["privileged_access"],
    "authz-function-level": ["privileged_access"],
}

# A precondition makes these objective classes newly worth probing (deeper hypotheses).
_REACHES = {
    "credential_held": ["AUTHZ-FUNCTION-LEVEL", "INFRA-SECRET-SURFACE", "CONF-CROSS-TENANT-READ"],
    "internal_reach": ["INFRA-SSRF", "INFRA-RCE-INJECTION"],
    "internal_detail": ["INFRA-SECRET-SURFACE", "AUTHZ-FUNCTION-LEVEL"],
    "other_tenant_ref": ["CONF-CROSS-TENANT-READ"],
    "other_object_ref": ["CONF-BOLA-CROSS-USER", "INTEG-CROSS-WRITE"],
    "authenticated_as_victim": ["AUTHZ-FUNCTION-LEVEL", "INTEG-PRIVESC-MASSASSIGN"],
    "privileged_access": [
        "INFRA-SSRF", "INFRA-RCE-INJECTION", "INFRA-SECRET-SURFACE",
        "CONF-CROSS-TENANT-READ",
    ],
}


def preconditions(finding: dict) -> list:
    """The pivot preconditions a confirmed finding unlocks (matched on category/objective/class)."""
    keys = [str(finding.get(k) or "").lower() for k in ("category", "objective_id", "class")]
    out = []
    for k in keys:
        for unlock in _UNLOCKS.get(k, []):
            if unlock not in out:
                out.append(unlock)
    return out


def unlocked_objectives(preconds: list) -> list:
    """The objectives that become reachable given a set of preconditions — the deeper
    hypotheses the next pass should pursue (the §5 chaining feedback edge)."""
    out = []
    for p in preconds or []:
        for obj in _REACHES.get(p, []):
            if obj not in out:
                out.append(obj)
    return out


# A precondition a confirmed finding unlocks -> a concrete chained PLAY: the objective to drive next,
# and the exact escalation to attempt USING the confirmed material. This turns chaining from a textual
# reflect suggestion into FORCED mandatory hypotheses (§5 chaining edge, driven mid-run).
_CHAIN_PLAYS = {
    "internal_reach": ("INFRA-SECRET-SURFACE",
        "Use the confirmed internal-reach/SSRF primitive ({src}) to reach internal services + cloud "
        "metadata (169.254.169.254/latest/meta-data, metadata.google.internal, 127.0.0.1 actuator/env) "
        "and pull credentials/secrets the response surfaces."),
    "credential_held": ("CONF-CROSS-TENANT-READ",
        "Use the surfaced credential/secret ({src}) to authenticate and access resources it must not — "
        "another tenant's data, admin/secret APIs, or an integration — proving real impact of the leak."),
    "other_tenant_ref": ("INTEG-CROSS-WRITE",
        "Escalate the cross-tenant access ({src}) from READ to WRITE on the other tenant, and use that "
        "tenant's integrations/secrets/workflows (cascading isolation break), not just a single read."),
    "other_object_ref": ("INTEG-CROSS-WRITE",
        "Using the object reference from ({src}), enumerate adjacent ids and attempt WRITE/mutation of "
        "another user's object, not only a read."),
    "privileged_access": ("INFRA-RCE-INJECTION",
        "Using the privilege obtained in ({src}), register and RUN a workflow whose task performs SSRF/"
        "code-exec and read the secrets/internal data it surfaces — convert privilege into engine compromise."),
    "authenticated_as_victim": ("AUTHZ-FUNCTION-LEVEL",
        "As the victim principal obtained in ({src}), exercise privileged/function-level actions that "
        "principal should not have, and cross-object operations."),
    "internal_detail": ("INFRA-SECRET-SURFACE",
        "Use the internal detail leaked in ({src}) to locate and reach a secret/admin surface."),
}


def chained_hypotheses(confirmed: list, identity_labels: list | None = None) -> list:
    """FORCED next-step hypotheses built from confirmed findings (§5 chaining, driven mid-run not just
    suggested in reflect): each confirmed finding's unlocked precondition becomes a mandatory hypothesis
    that USES the confirmed material to escalate. Deduped per precondition. Pure."""
    labels = [str(x) for x in (identity_labels or []) if x] or ["anon"]
    out, seen = [], set()
    for f in confirmed or []:
        if not isinstance(f, dict):
            continue
        src = str(f.get("title") or f.get("objective_id") or "a confirmed finding")[:90]
        for pre in preconditions(f):
            play = _CHAIN_PLAYS.get(pre)
            if not play or pre in seen:
                continue
            seen.add(pre)
            objective_id, instruction = play
            out.append({
                "id": f"MAND-CHAIN-{pre}",
                "title": f"Chain from confirmed finding -> {objective_id}",
                "objective_id": objective_id,
                "category": "chaining",
                "target": src,
                "rationale": "A confirmed finding unlocked this precondition; a strategic attacker pivots "
                             "rather than stopping at the first win. Driven mid-run, not left as a suggestion.",
                "identities": labels[:2],
                "test_plan": [
                    instruction.format(src=src),
                    "Confirm with a concrete impact oracle (data from another tenant/user, a secret read, "
                    "an OOB hit, or a privileged action succeeding) — not a 200.",
                ],
                "expected_evidence": "Escalated impact built on the prior finding (cross-tenant/secret/"
                                     "privileged data or action), with a decisive oracle.",
                "blind": False,
                "mandatory": True,
                "mandatory_kind": "chain",
                "chained_from": src,
            })
    return out


def attach(graph: dict, finding: dict) -> dict:
    """Incrementally add a confirmed finding to the attack graph: a node + chaining edges from
    any earlier node whose unlocked objectives this finding's objective fulfils (machine-driven
    chaining during the loop, §5/§8 — not assembled only at report time)."""
    graph = graph or {"nodes": [], "edges": []}
    pre = preconditions(finding)
    node = {"id": finding.get("finding_sig") or finding.get("objective_id") or finding.get("title"),
            "objective_id": finding.get("objective_id"), "preconditions": pre,
            "unlocks": unlocked_objectives(pre)}
    for earlier in graph["nodes"]:
        if node.get("objective_id") and node["objective_id"] in (earlier.get("unlocks") or []):
            graph["edges"].append({"from": earlier["id"], "to": node["id"], "via": "chaining"})
    graph["nodes"].append(node)
    return graph


# Cross-primitive compositions: TWO confirmed primitives together unlock a deeper play that neither
# reaches alone. This is the synthesis the swarm standup performs over the board's verified-primitive
# pool (design/SWARM.md section 4) — the step that produces the deep, chained, hidden bugs.
_COMBO_PLAYS = {
    frozenset({"internal_reach", "credential_held"}): ("INFRA-RCE-INJECTION",
        "Combine the internal reach ({a}) with the held credential ({b}): authenticate to the internal "
        "service the SSRF exposed and drive its privileged API toward code/secret exfiltration."),
    frozenset({"internal_reach", "internal_detail"}): ("INFRA-SECRET-SURFACE",
        "Use the internal detail ({b}) to target the specific internal secret/admin endpoint reachable "
        "via the confirmed internal reach ({a})."),
    frozenset({"credential_held", "other_object_ref"}): ("INTEG-CROSS-WRITE",
        "Use the held credential ({a}) to WRITE/mutate the cross-user object referenced by ({b})."),
    frozenset({"credential_held", "other_tenant_ref"}): ("CONF-CROSS-TENANT-READ",
        "Use the held credential ({a}) to read/act across the tenant boundary referenced by ({b})."),
    frozenset({"privileged_access", "internal_reach"}): ("INFRA-RCE-INJECTION",
        "Combine privilege ({a}) with internal reach ({b}): register+run an engine task that performs "
        "SSRF/code-exec against the internal surface and read what it returns."),
}


def _chain_signature(objective_id: str, target: str, labels: list) -> str:
    """Match the loop's dedupe signature (category|target|identities) for a chaining hypothesis."""
    return ("chaining|" + str(target).lower().replace(" ", "") + "|" + ",".join(sorted(labels)))


def emergent_chains(primitives: list, identity_labels: list | None = None,
                    tried_signatures: list | None = None) -> list:
    """Board-driven chaining: compose the verified-primitive POOL into deeper chain-hypotheses,
    INCLUDING cross-primitive combinations, deduped against what has already been tried.

    Unlike ``chained_hypotheses`` (fixed per-finding map), this reads the live pool a swarm wave
    deposited on the blackboard — each primitive a dict with at least ``kind`` (e.g. ``internal_reach``,
    ``credential_held``) and an optional ``source``/``title``. Single-primitive plays reuse the
    ``_CHAIN_PLAYS`` seeds; combinations use ``_COMBO_PLAYS``. Pure (no IO)."""
    labels = [str(x) for x in (identity_labels or []) if x][:2] or ["anon"]
    tried = {str(s) for s in (tried_signatures or [])}
    by_kind: dict[str, dict] = {}
    for p in primitives or []:
        if isinstance(p, dict) and p.get("kind"):
            by_kind.setdefault(str(p["kind"]), p)  # first of each kind

    out, seen = [], set()

    def _emit(objective_id, target, instruction, provenance):
        sig = _chain_signature(objective_id, target, labels)
        if sig in tried or sig in seen:
            return
        seen.add(sig)
        out.append({
            "id": f"EMERGENT-{objective_id}-{len(out)}",
            "title": f"Emergent chain -> {objective_id}",
            "objective_id": objective_id,
            "category": "chaining",
            "target": target,
            "rationale": "Synthesized at the standup from the verified-primitive pool; combines "
                         "independent findings into a deeper chain no single agent reached.",
            "identities": labels,
            "test_plan": [instruction, "Confirm with a decisive impact oracle (cross-tenant/secret/"
                          "privileged data or an OOB hit), never a 200."],
            "expected_evidence": "Escalated, chained impact proven by an oracle.",
            "blind": False, "mandatory": True, "mandatory_kind": "emergent_chain",
            "chained_from": provenance,
        })

    # Single-primitive plays (the pool equivalent of chained_hypotheses).
    for kind, p in by_kind.items():
        play = _CHAIN_PLAYS.get(kind)
        if not play:
            continue
        objective_id, instruction = play
        src = str(p.get("source") or p.get("title") or kind)[:90]
        _emit(objective_id, src, instruction.format(src=src), src)

    # Cross-primitive combinations (the emergent synthesis).
    for combo, (objective_id, instruction) in _COMBO_PLAYS.items():
        if combo.issubset(by_kind.keys()):
            ka, kb = sorted(combo)
            a = str(by_kind[ka].get("source") or by_kind[ka].get("title") or ka)[:60]
            b = str(by_kind[kb].get("source") or by_kind[kb].get("title") or kb)[:60]
            _emit(objective_id, f"{a} + {b}", instruction.format(a=a, b=b), f"{a} + {b}")

    return out
