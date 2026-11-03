"""Deterministic in-band exploitation oracles (PLAN_V3 section 5.4).

These are the proof functions a dedicated in-band verifier worker calls AFTER it re-issues an
oracle against the target itself (never trusting sandbox stdout). Each returns the typed proof
fields that `evidence.inband_confirmation` re-validates before a finding is promoted, and each
encodes the guard against its classic false positive so a lucky/benign response cannot confirm.

Pure logic, unit-tested (`tests/test_inband_oracle.py`). A verifier worker wraps a result as::

    {"verification": evidence.INBAND_VERIFIER, "available": True, **result}
"""

from __future__ import annotations

import json

# Statuses a front-end/authz layer returns when it BLOCKS a request (used for the SSRF control).
DEFAULT_BLOCKED_STATUSES = (401, 403, 407)

# LEVEL-4 (destructive / availability-impacting / real-data extraction) oracles. The verifier only
# executes these when the campaign is authorized to capability_max>=4 -- re-issuing the proof is
# itself a level-4 action.
LEVEL4_ORACLES = ("data_exfiltration", "destructive_persist", "availability_degradation")


def _contains(haystack, needle) -> bool:
    return bool(needle) and str(needle) in str(haystack or "")


def ssrf_differential(control: dict, test: dict, markers,
                      blocked_statuses=DEFAULT_BLOCKED_STATUSES) -> dict:
    """Confirm a server-side request forgery by DIFFERENTIAL, not by a bare success.

    ``control`` is the response to a known-blocked internal form (e.g. ``http://127.0.0.1/...``);
    ``test`` is the response to a candidate bypass form (e.g. ``http://[::1]/...``). Each is a dict
    ``{status, body}``. ``markers`` are distinctive strings that only an INTERNAL backend response
    would contain (e.g. an internal-only path, an actuator key, an OpenAPI title).

    Confirmed iff the test was NOT blocked and at least one marker is present in the test body but
    ABSENT from the control body -- an internal-only signal that the server's own backend fetch
    produced. Exclusivity (in test, not control) rules out open redirects, generic error pages, and
    front-end echoes (those appear in BOTH). This covers two SSRF shapes: EGRESS-BYPASS (control is a
    filtered form blocked 401/403/407; test is the bypass form) and DIRECT-REACH-REFLECTION (control
    and test are the SAME endpoint pointed at a benign vs an internal target, and the internal body is
    reflected only in the test -- e.g. a config-repoint SSRF). ``control_blocked`` is reported as
    corroboration but is not required.
    """
    control = control or {}
    test = test or {}
    markers = [m for m in (markers or []) if m]
    cstatus, tstatus = control.get("status"), test.get("status")
    cbody, tbody = control.get("body"), test.get("body")

    control_blocked = cstatus in blocked_statuses
    test_not_blocked = tstatus is not None and tstatus not in blocked_statuses
    marker = next((m for m in markers if _contains(tbody, m) and not _contains(cbody, m)), "")
    reached_backend = bool(test_not_blocked and marker)
    # Two valid SSRF-confirmation models, both proven by an INTERNAL-only marker present in the test
    # response and ABSENT from the control (a front-end echo would appear in BOTH, so exclusivity rules
    # it out): (a) EGRESS-BYPASS -- a filtered control form is blocked (401/403/407) while the bypass
    # form reaches the backend; (b) DIRECT-REACH-REFLECTION -- control and test hit the SAME endpoint
    # pointed at DIFFERENT targets (benign vs internal) and the internal backend's distinctive body is
    # reflected only in the test (e.g. a config-repoint SSRF whose error echoes the internal response).
    # The exclusive marker is the real proof in both; control_blocked is corroboration, not a gate.
    confirmed = reached_backend
    return {
        "oracle": "ssrf_differential",
        "confirmed": bool(confirmed),
        "model": ("egress_bypass" if control_blocked else "direct_reach_reflection") if confirmed else "",
        "control_blocked": control_blocked,
        "reached_backend": reached_backend,
        "marker": marker,
        "control_status": cstatus,
        "test_status": tstatus,
    }


def rce_arithmetic(body, operand_a: int, operand_b: int) -> dict:
    """Confirm code/expression execution by an arithmetic echo the target had to COMPUTE.

    The verifier injects an expression like ``<a>*<b>`` with fresh per-attempt operands and passes
    the response ``body`` here. Confirmed iff the product appears in the response AND neither operand
    does -- if an operand appears, the payload was reflected verbatim rather than evaluated, so it is
    NOT confirmation. Operands should be large/random so the product cannot occur by chance.
    """
    product = int(operand_a) * int(operand_b)
    product_echoed = _contains(body, product)
    operands_reflected = _contains(body, operand_a) or _contains(body, operand_b)
    return {
        "oracle": "rce_arithmetic",
        "confirmed": bool(product_echoed and not operands_reflected),
        "product": product,
        "product_echoed": product_echoed,
        "operands_reflected": operands_reflected,
    }


def time_blind_sql(test_ms, control_ms, injected_delay_ms,
                   min_fraction: float = 0.6, max_control_fraction: float = 0.5) -> dict:
    """Confirm blind SQL injection by a CONDITIONAL time differential, not a bare slow response.

    ``test_ms`` is the latency of the conditional-delay payload (e.g. ``AND SLEEP(n)``),
    ``control_ms`` the latency of an otherwise-identical request whose predicate is false (no
    delay), and ``injected_delay_ms`` the delay asked of the DB (``n`` in ms).

    Confirmed iff the measured delta (test - control) is at least ``min_fraction`` of the injected
    delay AND the control is not itself already slow (control < ``max_control_fraction`` of the
    injected delay). So a uniformly slow endpoint, or random jitter, cannot confirm.
    """
    try:
        t = float(test_ms); c = float(control_ms); inj = float(injected_delay_ms)
    except (TypeError, ValueError):
        return {"oracle": "time_blind_sql", "confirmed": False, "delta_ms": 0.0,
                "test_ms": test_ms, "control_ms": control_ms, "threshold_ms": 0.0,
                "control_fast": False}
    delta = t - c
    threshold = inj * float(min_fraction)
    control_fast = c < (inj * float(max_control_fraction))
    confirmed = bool(inj > 0 and delta >= threshold and control_fast)
    return {"oracle": "time_blind_sql", "confirmed": confirmed, "delta_ms": round(delta, 1),
            "test_ms": round(t, 1), "control_ms": round(c, 1), "threshold_ms": round(threshold, 1),
            "control_fast": control_fast}


def stored_xss_victim_context(author_identity, victim_identity, executed_in_victim_context: bool,
                              marker: str = "") -> dict:
    """Confirm STORED XSS only when a payload persisted as one identity EXECUTES in a DIFFERENT
    identity's view (a cross-user hit), not when it merely reflects back to its author.

    ``executed_in_victim_context`` is the decisive execution signal (an OOB callback fired from the
    victim's render, or a DOM-exec marker observed while viewing as the victim). Confirmed iff that
    signal fired AND the author and victim are distinct identities.
    """
    distinct = (bool(author_identity) and bool(victim_identity)
                and str(author_identity) != str(victim_identity))
    confirmed = bool(executed_in_victim_context and distinct)
    return {"oracle": "stored_xss_victim_context", "confirmed": confirmed,
            "executed_in_victim_context": bool(executed_in_victim_context),
            "author_identity": author_identity, "victim_identity": victim_identity,
            "cross_identity": distinct, "marker": marker or ""}


def race_invariant(allowed_successes, observed_successes, control_enforced: bool,
                   status_distribution: dict | None = None) -> dict:
    """Confirm a race / TOCTOU / double-spend only when a CONCURRENT burst breaks an invariant that
    the SEQUENTIAL control proved is enforced.

    ``control_enforced`` must be True -- a sequential retry of the one-time/limited action was
    rejected, so there IS a rule. ``observed_successes`` is the number of 2xx successes in the
    concurrent burst; confirmed iff it exceeds ``allowed_successes`` while the sequential rule held
    (otherwise the app simply has no limit -- a different, non-race finding).
    """
    try:
        allowed = int(allowed_successes); obs = int(observed_successes)
    except (TypeError, ValueError):
        return {"oracle": "race_invariant", "confirmed": False, "invariant_breached": False,
                "allowed_successes": allowed_successes, "observed_successes": observed_successes,
                "control_enforced": bool(control_enforced), "status_distribution": status_distribution or {}}
    breached = obs > allowed
    confirmed = bool(control_enforced and breached)
    return {"oracle": "race_invariant", "confirmed": confirmed, "invariant_breached": breached,
            "allowed_successes": allowed, "observed_successes": obs,
            "control_enforced": bool(control_enforced), "status_distribution": status_distribution or {}}


def path_traversal_canary(test_body, canary, control_body=None) -> dict:
    """Confirm path traversal / LFI by a DISTINCTIVE file-content canary that appears when reading
    the out-of-bounds path but NOT when reading an in-bounds control path.

    ``canary`` is a distinctive line from the target file (e.g. ``root:x:0:0`` for /etc/passwd), not
    a string we sent. Confirmed iff the canary is present in ``test_body`` and absent from
    ``control_body`` -- guarding against a generic echo/200 or a page that always contains it.
    """
    present = _contains(test_body, canary)
    control_clean = not _contains(control_body, canary)
    confirmed = bool(canary and present and control_clean)
    return {"oracle": "path_traversal_canary", "confirmed": confirmed,
            "canary_present": present, "control_clean": control_clean,
            "marker": str(canary) if confirmed else ""}


def auth_state_illegal_transition(illegal_persisted: bool, control_enforced: bool,
                                  from_state: str = "", to_state: str = "") -> dict:
    """Confirm an illegal state-machine transition only when (a) the illegal transition is
    OBSERVABLY PERSISTED (a follow-up read shows the resource in a state that should be unreachable)
    AND (b) a control shows the LEGAL guard is actually enforced -- so the illegal path bypassed a
    real rule, rather than the app simply having no state guard at all.
    """
    confirmed = bool(illegal_persisted and control_enforced)
    return {"oracle": "auth_state_illegal_transition", "confirmed": confirmed,
            "illegal_persisted": bool(illegal_persisted), "control_enforced": bool(control_enforced),
            "from_state": from_state or "", "to_state": to_state or ""}


_SQL_ERROR_MARKERS = ("sql syntax", "sqlite", "syntax error", "unrecognized token",
                      "unterminated", "sqlstate", "ora-0", "pg::", "psqlexception",
                      "you have an error in your sql", "quoted string not properly terminated",
                      "near \"", "sequelize", "unexpected token")


def sql_error_differential(control, test, markers=None) -> dict:
    """Confirm ERROR-BASED SQL injection by a DIFFERENTIAL: the injection payload provokes a database
    error signal the benign control does not. ``control``/``test`` are ``{status, body}`` (test = the
    quote/type-mismatch payload). Confirmed iff a SQL-error marker appears in the test body but NOT the
    control body (so a page that always shows the marker, or a generic 500, cannot pass)."""
    control = control or {}
    test = test or {}
    cb = str(control.get("body") or "").lower()
    tb = str(test.get("body") or "").lower()
    mks = [m.lower() for m in (markers or _SQL_ERROR_MARKERS) if m]
    marker = next((m for m in mks if m in tb and m not in cb), "")
    return {"oracle": "sql_error_differential", "confirmed": bool(marker),
            "marker": marker, "control_status": control.get("status"), "test_status": test.get("status")}


def boolean_sqli(baseline, true_resp, false_resp) -> dict:
    """Confirm BOOLEAN-BLIND SQL injection: a TRUE predicate (``AND 1=1``) matches the baseline while a
    FALSE predicate (``AND 1=2``) diverges — proving the injected condition is evaluated by the DB.
    Each arg is ``{status, body, rows?}``. Uses ``rows`` when supplied, else response-body length.
    Confirmed iff metric(true) == metric(baseline) AND metric(false) != metric(baseline)."""
    def metric(r):
        r = r or {}
        if r.get("rows") is not None:
            try:
                return ("rows", int(r["rows"]))
            except (TypeError, ValueError):
                pass
        return ("len", len(str(r.get("body") or "")))
    mb, mt, mf = metric(baseline), metric(true_resp), metric(false_resp)
    true_matches = (mt == mb)
    false_differs = (mf != mb)
    return {"oracle": "boolean_sqli", "confirmed": bool(true_matches and false_differs and mt != mf),
            "true_matches_baseline": true_matches, "false_differs": false_differs,
            "baseline_metric": mb, "true_metric": mt, "false_metric": mf}


def cross_tenant_write_persist(attacker_write, victim_read, injected_marker, control_read=None,
                               blocked_statuses=DEFAULT_BLOCKED_STATUSES) -> dict:
    """Confirm SHARED-RESOURCE / CROSS-TENANT POISONING — the class classic BOLA/IDOR oracles miss.
    A low-priv or free-signup tenant WRITES to a GLOBAL/shared resource with no ownership check (e.g.
    a shared integration-provider definition, a global config/registry entry, a cross-tenant template),
    and the write is visible to / affects a DIFFERENT tenant.

    ``attacker_write`` is the write response (must be ACCEPTED, not blocked). ``victim_read`` is a
    DIFFERENT identity/tenant reading the SAME shared resource afterward. ``injected_marker`` is the
    distinctive value the attacker wrote (e.g. ``https://attacker.com/v1`` repointing a provider
    endpoint, a canary string). Optional ``control_read`` is the resource BEFORE the write (or a
    sibling the attacker never touched) and MUST NOT contain the marker.

    Confirmed iff: the write was accepted AND the attacker's marker appears in the victim's
    (non-blocked) read AND is absent from the control — so a value that was always there, or a
    rejected write, cannot pass. Each arg is ``{status, body}``."""
    attacker_write = attacker_write or {}
    victim_read = victim_read or {}
    marker = str(injected_marker or "")
    wstatus = attacker_write.get("status")
    write_accepted = wstatus is not None and wstatus not in blocked_statuses
    vbody = str(victim_read.get("body") or "")
    cbody = str((control_read or {}).get("body") or "") if control_read else ""
    vstatus = victim_read.get("status")
    victim_ok = vstatus is not None and vstatus not in blocked_statuses
    marker_persisted = bool(marker) and marker in vbody
    control_clean = (control_read is None) or (marker not in cbody)
    confirmed = bool(write_accepted and victim_ok and marker_persisted and control_clean)
    return {"oracle": "cross_tenant_write_persist", "confirmed": confirmed,
            "write_accepted": write_accepted, "marker_persisted": marker_persisted,
            "control_clean": control_clean, "write_status": wstatus, "victim_status": vstatus,
            "marker": marker if confirmed else ""}


def auth_bypass(escalated, control, privilege_marker,
                blocked_statuses=DEFAULT_BLOCKED_STATUSES) -> dict:
    """Confirm AUTHENTICATION / AUTHORIZATION BYPASS — an identity or role obtained that the
    legitimate path does not grant. The single highest-value class the prior oracle set missed
    (login SQLi auth bypass, JWT forgery to admin, mass-assignment role=admin privesc).

    ``escalated`` is the response that should prove elevated access: a login/registration response
    that returns a session for a principal the supplied credentials don't entitle you to, OR a
    privileged action/read performed with the illegitimately-obtained identity. ``control`` is the
    LEGITIMATE path that must NOT grant it: the same login with wrong credentials (rejected), a
    normal-role request to the same privileged action (denied), or a normal registration without the
    injected field. ``privilege_marker`` is distinctive evidence of the elevated principal or role
    that the escalated response carries and the control does not — e.g. ``admin@juice-sh.op``, an
    admin-only field, ``"role":"admin"``, or the victim principal's id.

    Confirmed iff the marker appears in the escalated (non-blocked) response AND is ABSENT from the
    control — so a field everyone legitimately receives, or a bare 2xx with no privileged effect,
    cannot pass. Each response is ``{status, body}``."""
    escalated = escalated or {}
    marker = str(privilege_marker or "")
    eb = str(escalated.get("body") or "")
    cb = str((control or {}).get("body") or "") if control else ""
    leaked = bool(marker) and marker in eb
    control_clean = (control is None) or (marker not in cb)
    estatus = escalated.get("status")
    not_blocked = estatus is not None and estatus not in blocked_statuses
    confirmed = bool(leaked and control_clean and not_blocked)
    return {"oracle": "auth_bypass", "confirmed": confirmed,
            "marker_leaked": leaked, "control_clean": control_clean,
            "escalated_status": estatus, "marker": marker if confirmed else ""}


def behavioral_differential(control, test, marker=None, control_metric=None, test_metric=None,
                            min_delta=None, replayed: bool = True) -> dict:
    """Class-AGNOSTIC proof shape: a stable, attacker-controlled discriminator produces a
    reproducible control-vs-test divergence. This is the general oracle for a novel bug whose class
    has no dedicated oracle yet — confirm the influence without naming the mechanism.

    A divergence may be shown two ways (either suffices):
      * marker: a distinctive string present in ``test`` body but absent from ``control`` body, OR
      * metric: ``abs(test_metric - control_metric) >= min_delta`` (response length, row count, a
        parsed numeric — any attacker-attributable measurement).

    ``replayed`` MUST be True: a second independent measurement reproduced the divergence (guards
    against one-off noise/jitter). Confirmed iff a signal exists AND it was replayed. Each response
    is ``{status, body}``."""
    control = control or {}
    test = test or {}
    marker = str(marker or "")
    tb = str(test.get("body") or "")
    cb = str(control.get("body") or "")
    marker_divergence = bool(marker) and marker in tb and marker not in cb
    metric_divergence = False
    delta = None
    try:
        if control_metric is not None and test_metric is not None and min_delta is not None:
            delta = abs(float(test_metric) - float(control_metric))
            metric_divergence = delta >= float(min_delta)
    except (TypeError, ValueError):
        metric_divergence = False
    # CONFIRMATION requires a genuine MARKER divergence — a distinctive out-of-boundary string that
    # appears in the test but not the control. A bare metric/length delta is NOT proof: different
    # inputs (a different {name}, a valid-vs-nonexistent resource) naturally produce different-sized
    # responses, which is expected API behaviour, not a vulnerability. Metric divergence is retained
    # only as a corroborating lead signal. (This is the fix for the false positives an unqualified
    # length-delta produced against a real multi-tenant API.)
    has_signal = bool(marker_divergence)
    confirmed = bool(has_signal and replayed)
    return {"oracle": "behavioral_differential", "confirmed": confirmed,
            "has_signal": has_signal, "replayed": bool(replayed),
            "marker_divergence": marker_divergence, "metric_divergence": metric_divergence,
            "metric_lead_only": bool(metric_divergence and not marker_divergence),
            "delta": round(delta, 3) if isinstance(delta, float) else None,
            "marker": marker if marker_divergence else ""}


def single_use(allowed_uses, observed_successes, control_enforced: bool) -> dict:
    """Business-logic invariant: confirm a ONE-TIME / limited action was redeemed more times than
    allowed (coupon reuse, single-use token replay, one-per-account bypass). ``control_enforced``
    must be True — a straightforward repeat of the action was rejected, so a limit genuinely exists —
    otherwise the app simply has no limit (a different finding, not a bypass)."""
    try:
        allowed = int(allowed_uses); obs = int(observed_successes)
    except (TypeError, ValueError):
        return {"oracle": "single_use", "confirmed": False, "invariant_breached": False,
                "allowed_uses": allowed_uses, "observed_successes": observed_successes,
                "control_enforced": bool(control_enforced)}
    breached = obs > allowed
    return {"oracle": "single_use", "confirmed": bool(control_enforced and breached),
            "invariant_breached": breached, "allowed_uses": allowed,
            "observed_successes": obs, "control_enforced": bool(control_enforced)}


def value_floor(observed_value, floor=0, persisted: bool = True) -> dict:
    """Business-logic invariant: confirm a quantity / price / balance was driven below a legal floor
    and the illegal value was ACCEPTED/PERSISTED (negative-quantity basket, negative-price item,
    balance underflow). Confirmed iff the observed value is below ``floor`` and ``persisted`` (a
    read-back shows the illegal value took effect), not merely that the request was sent."""
    try:
        val = float(observed_value); flr = float(floor)
    except (TypeError, ValueError):
        return {"oracle": "value_floor", "confirmed": False, "below_floor": False,
                "observed_value": observed_value, "floor": floor, "persisted": bool(persisted)}
    below = val < flr
    return {"oracle": "value_floor", "confirmed": bool(below and persisted),
            "below_floor": below, "observed_value": val, "floor": flr, "persisted": bool(persisted)}


def monotonic_state(from_rank, to_rank, control_enforced: bool,
                    from_state: str = "", to_state: str = "") -> dict:
    """Business-logic invariant: confirm an illegal BACKWARD state regression on a forward-only
    workflow (e.g. a delivered order moved back to 'in cart', a closed ticket re-opened past policy).
    ``from_rank``/``to_rank`` are the ordinal positions in the legal forward sequence; confirmed iff
    ``to_rank < from_rank`` (regressed) AND ``control_enforced`` (the legal forward-only guard is
    normally applied), so an app with no ordering cannot pass."""
    try:
        fr = int(from_rank); tr = int(to_rank)
    except (TypeError, ValueError):
        return {"oracle": "monotonic_state", "confirmed": False, "regressed": False,
                "from_rank": from_rank, "to_rank": to_rank, "control_enforced": bool(control_enforced),
                "from_state": from_state or "", "to_state": to_state or ""}
    regressed = tr < fr
    return {"oracle": "monotonic_state", "confirmed": bool(regressed and control_enforced),
            "regressed": regressed, "from_rank": fr, "to_rank": tr,
            "control_enforced": bool(control_enforced),
            "from_state": from_state or "", "to_state": to_state or ""}


def cross_identity_access(victim, attacker, victim_marker, control=None,
                          blocked_statuses=DEFAULT_BLOCKED_STATUSES) -> dict:
    """Confirm IDOR / BOLA / broken-access-control / privilege-escalation by a CROSS-IDENTITY
    differential — the single most common real-world class, which no other oracle covers.

    ``victim`` is the response when the resource is fetched by its rightful owner / a privileged
    identity (it establishes ``victim_marker``: the owner's distinctive data — their email, id, an
    admin-only field). ``attacker`` is the SAME resource fetched as a DIFFERENT, lower-privilege or
    other-tenant identity (or anon). Optional ``control`` is the attacker fetching its OWN resource.

    Confirmed iff the victim's distinctive marker appears in the attacker's (non-blocked) response AND
    is NOT present in the attacker's own control (so a field everyone legitimately sees cannot pass).
    Each response is ``{status, body}``."""
    attacker = attacker or {}
    marker = str(victim_marker or "")
    ab = str(attacker.get("body") or "")
    cb = str((control or {}).get("body") or "") if control else ""
    leaked = bool(marker) and marker in ab
    control_clean = (control is None) or (marker not in cb)
    astatus = attacker.get("status")
    not_blocked = astatus is not None and astatus not in blocked_statuses
    confirmed = bool(leaked and control_clean and not_blocked)
    return {"oracle": "cross_identity_access", "confirmed": confirmed,
            "marker_leaked": leaked, "control_clean": control_clean,
            "attacker_status": astatus, "marker": marker if confirmed else ""}


# --- LEVEL-4 oracles (destructive / availability / real-data extraction) --------------------------

def data_exfiltration(records, min_records: int = 5, sensitive_classes=None) -> dict:
    """Confirm LEVEL-4 BULK REAL-DATA EXTRACTION: an attacker pulled a volume of DISTINCT real records
    carrying sensitive data they must not have (SQLi UNION dump, mass IDOR enumeration, unauth bulk
    export). ``records`` is the list of records the VERIFIER parsed from the re-issued response;
    ``sensitive_classes`` is the DLP classification of that body (e.g. ["pii","secret"]). Confirmed
    iff there are >= ``min_records`` DISTINCT records AND at least one sensitive class is present --
    so a public list endpoint returning many non-sensitive rows (the classic FP) cannot pass."""
    recs = records or []
    seen, distinct = set(), 0
    for r in recs:
        k = json.dumps(r, sort_keys=True, default=str) if isinstance(r, (dict, list)) else str(r)
        if k not in seen:
            seen.add(k)
            distinct += 1
    classes = [str(c) for c in (sensitive_classes or [])]
    enough = distinct >= max(1, int(min_records))
    confirmed = bool(enough and classes)
    return {"oracle": "data_exfiltration", "confirmed": confirmed, "record_count": distinct,
            "min_records": int(min_records), "sensitive_classes": classes}


def destructive_persist(pre_readback, post_readback, marker="",
                        blocked_statuses=(401, 403, 404, 410)) -> dict:
    """Confirm LEVEL-4 DESTRUCTIVE IMPACT: a resource that EXISTED was irreversibly destroyed/corrupted
    by the attacker's operation. ``pre_readback`` must show it present (2xx and, if a marker is given,
    containing it); ``post_readback`` (AFTER the destroy op) must show it gone (404/410/blocked) or the
    marker removed. Confirmed iff present-then-gone -- guards the FP where the resource never existed
    (pre absent) so a no-op 'delete' of a missing id cannot pass."""
    pre, post = pre_readback or {}, post_readback or {}
    m = str(marker or "")
    pre_status = pre.get("status")
    pre_present = (isinstance(pre_status, int) and 200 <= pre_status < 300
                   and (not m or m in str(pre.get("body") or "")))
    post_status = post.get("status")
    gone = (post_status in (404, 410) or post_status in blocked_statuses
            or (bool(m) and isinstance(post_status, int) and 200 <= post_status < 300
                and m not in str(post.get("body") or "")))
    confirmed = bool(pre_present and gone)
    return {"oracle": "destructive_persist", "confirmed": confirmed, "pre_present": pre_present,
            "destroyed": gone, "pre_status": pre_status, "post_status": post_status}


def availability_degradation(steps, baseline_ms=None) -> dict:
    """Confirm LEVEL-4 AVAILABILITY IMPACT: under a bounded, authorized load ramp the service degrades
    (latency knee / rising error rate). ``steps`` is [{concurrency, p95_ms, error_rate}] measured by
    the verifier itself. Confirmed iff a degradation knee is detected (delegates to loadknee.analyze,
    which encodes the baseline-relative inflection guard)."""
    from common import loadknee
    a = loadknee.analyze(steps or [])
    return {"oracle": "availability_degradation", "confirmed": bool(a.get("degraded")),
            "knee_at": a.get("knee_at"), "baseline_ms": a.get("baseline_ms"),
            "steps": len(steps or []), "summary": a.get("summary", "")}
