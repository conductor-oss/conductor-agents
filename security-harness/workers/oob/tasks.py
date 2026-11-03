"""oob_check -- query the OOB collaborator for canary hits.

The blind-confirmation half of the OOB channel: given a canary token (or several),
ask the local listener whether the target's server reached it. verify_finding calls
this to turn a blind SSRF/exfil/exec hypothesis into a confirmed (inbound hit
observed) or refuted (no hit) finding.

Reads the listener's local port from workers/oob/state.json (written by
oob-setup.sh) and queries 127.0.0.1 directly -- never through the tunnel. Never
raises; a missing collaborator just yields hit=false so the verifier stays sound
(absence of a hit is "not confirmed", not an error).
"""

import json
import logging
import os

import requests
from conductor.client.worker.worker_task import worker_task

from common import evidence as evidence_mod
from common import inband_oracle
from common.auth import auth_headers
from common.net import reachable_url

log = logging.getLogger(__name__)
STATE = os.path.join(os.path.dirname(__file__), "state.json")
TIMEOUT = 10


def _port() -> int | None:
    try:
        with open(STATE) as fh:
            return int(json.load(fh).get("local_port"))
    except (OSError, ValueError, TypeError):
        return None


def _hits(port: int, token: str) -> list:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/_oob/hits",
                         params={"token": token}, timeout=TIMEOUT)
        return r.json().get("hits", []) if r.ok else []
    except requests.RequestException:
        return []


@worker_task(task_definition_name="oob_check", thread_count=2)
def oob_check(task):
    inp = task.input_data or {}
    tokens = inp.get("tokens")
    if isinstance(tokens, str):
        tokens = [tokens]
    if not isinstance(tokens, list):
        single = inp.get("token")
        tokens = [single] if single else []
    # Accept full canary URLs too: take the path segment after /c/.
    norm = []
    for t in tokens:
        if isinstance(t, dict):
            t = t.get("token") or t.get("url") or ""
        t = str(t)
        if "/c/" in t:
            t = t.split("/c/", 1)[1].split("/")[0].split("?")[0]
        if t:
            norm.append(t)

    port = _port()
    if port is None:
        return {"verification": "oob_check/v1", "available": False, "hit": False, "count": 0, "tokens": norm,
                "hits": [], "note": "no OOB collaborator configured (workers/oob/state.json missing)"}

    # Cf-Connecting-Ip / X-Forwarded-For carry the REAL source (through the tunnel),
    # and User-Agent betrays a server-side client (e.g. okhttp/Java) vs our own Python
    # sandbox -- strong SSRF attribution.
    def _src(h):
        hd = h.get("headers", {}) or {}
        return hd.get("Cf-Connecting-Ip") or hd.get("X-Forwarded-For") or h.get("client_ip")

    def _ua(h):
        return (h.get("headers", {}) or {}).get("User-Agent") or ""

    # A canary hit only proves SSRF if the TARGET fetched it. The sandbox itself can
    # reach the canary (egress allows the OOB host), so a hit from our own tooling is
    # a self-hit, NOT evidence -- exclude it so it can't be promoted to a finding.
    def _is_self(h):
        return "security-conductor" in _ua(h).lower()

    all_hits, self_hits, per = [], [], {}
    for t in norm:
        hs = _hits(port, t)
        tgt = [h for h in hs if not _is_self(h)]
        per[t] = len(tgt)                       # per_token counts TARGET hits only
        all_hits.extend(tgt)
        self_hits.extend([h for h in hs if _is_self(h)])

    sample = [{"token": h.get("token"), "method": h.get("method"), "path": h.get("path"),
               "source_ip": _src(h), "user_agent": _ua(h), "ts": h.get("ts")} for h in all_hits[:20]]
    return {
        "verification": "oob_check/v1", "available": True,
        "hit": len(all_hits) > 0,               # target-originated hits only
        "count": len(all_hits),
        "self_hits_excluded": len(self_hits),    # transparency: sandbox's own probes
        "tokens": norm,
        "per_token": per,
        "source_ips": sorted({_src(h) for h in all_hits if _src(h)}),
        "user_agents": sorted({_ua(h) for h in all_hits if _ua(h)}),
        "hits": sample,
    }


def _issue(req: dict, identities: dict, base_url: str) -> dict:
    """Re-issue ONE request from a recorded spec {method, path|url, headers, body, identity} using
    the same auth resolution as the product HTTP tool, and return {status, body}. This runs in the
    trusted verifier -- it does NOT reuse any response the sandbox captured."""
    req = req or {}
    method = str(req.get("method") or "GET").upper()
    url = req.get("url") or ((base_url or "").rstrip("/") + "/" + str(req.get("path") or "").lstrip("/"))
    headers = dict(req.get("headers") or {})
    ident = req.get("identity")
    if ident and isinstance(identities, dict):
        headers.update(auth_headers(identities.get(ident)))
    kwargs = {"headers": headers, "timeout": TIMEOUT, "allow_redirects": False, "verify": False}
    body = req.get("body")
    if isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif body is not None:
        kwargs["data"] = body
    r = requests.request(method, reachable_url(url), **kwargs)
    return {"status": r.status_code, "body": r.text[:20000],
            "elapsed_ms": int(r.elapsed.total_seconds() * 1000)}


def _issue_burst(req: dict, identities: dict, base_url: str, n: int) -> list:
    """Fire ``n`` concurrent identical requests (bounded) and return their status codes — used by the
    race-condition verifier to re-measure the concurrency boundary itself."""
    from concurrent.futures import ThreadPoolExecutor
    n = max(1, min(int(n or 1), 20))
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda _: _issue(req, identities, base_url), range(n)))
    return [r.get("status") for r in results]


_BLOCKED = (401, 403, 404, 405, 409, 423)


def _dig(body, path):
    """Walk a dotted path (``a.b.0.c``) into a JSON response body; returns None if absent/unparseable.
    Used by the value-floor / monotonic-state verifiers to read a numeric or state field out of a
    re-issued response ITSELF, so confirmation never trusts an agent-supplied value."""
    if not path:
        return None
    try:
        obj = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return None
    for part in str(path).split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return obj


@worker_task(task_definition_name="inband_check", thread_count=2)
def inband_check(task):
    """Independently RE-ISSUE an in-band exploitation oracle and emit a typed inband_check/v1
    receipt (the only in-band proof `deepen.detect_confirmation` accepts). The exploit sandbox
    records the oracle PLAN via sc.inband_probe() (which requests to send + markers/operands); this
    trusted worker re-sends them ITSELF and computes the verdict with inband_oracle, so confirmation
    never depends on sandbox stdout. Never raises -- an error/absence is 'not confirmed', not a
    failure (keeps the verifier sound)."""
    inp = task.input_data or {}
    probe = inp.get("probe")
    if isinstance(probe, list):
        probe = probe[0] if probe else None
    unavailable = {"verification": evidence_mod.INBAND_VERIFIER, "available": False, "confirmed": False}
    if not isinstance(probe, dict):
        return {**unavailable, "note": "no in-band probe plan recorded"}
    identities = inp.get("identities") if isinstance(inp.get("identities"), dict) else {}
    base = str(inp.get("base_url") or "")
    oracle = probe.get("oracle")
    try:
        if oracle == "ssrf_differential":
            ctl = _issue(probe.get("control") or {}, identities, base)
            tst = _issue(probe.get("test") or {}, identities, base)
            res = inband_oracle.ssrf_differential(ctl, tst, probe.get("markers") or [])
        elif oracle == "rce_arithmetic":
            resp = _issue(probe.get("request") or {}, identities, base)
            res = inband_oracle.rce_arithmetic(resp.get("body"), probe.get("operand_a"), probe.get("operand_b"))
        elif oracle == "time_blind_sql":
            # Re-measure the conditional-delay differential ourselves (test vs control).
            tst = _issue(probe.get("test") or {}, identities, base)
            ctl = _issue(probe.get("control") or {}, identities, base)
            res = inband_oracle.time_blind_sql(tst.get("elapsed_ms"), ctl.get("elapsed_ms"),
                                               probe.get("injected_delay_ms"))
        elif oracle == "path_traversal_canary":
            tst = _issue(probe.get("test") or {}, identities, base)
            ctl = _issue(probe.get("control") or {}, identities, base)
            res = inband_oracle.path_traversal_canary(tst.get("body"), probe.get("canary"),
                                                      ctl.get("body"))
        elif oracle == "race_invariant":
            # Establish the rule sequentially (one success, then a reject), then re-run the burst.
            boundary = probe.get("boundary_request") or probe.get("test") or {}
            first = _issue(boundary, identities, base)
            second = _issue(boundary, identities, base)
            control_enforced = (first.get("status") in range(200, 300)
                                and second.get("status") in _BLOCKED)
            statuses = _issue_burst(boundary, identities, base, probe.get("burst") or 10)
            observed = sum(1 for s in statuses if s and 200 <= s < 300)
            res = inband_oracle.race_invariant(probe.get("allowed_successes", 1), observed,
                                               control_enforced,
                                               status_distribution={str(s): statuses.count(s) for s in set(statuses)})
        elif oracle == "auth_state_illegal_transition":
            # The legal guard must be enforced (control blocked), the illegal transition issued, and
            # a read-back must show it persisted.
            ctl = _issue(probe.get("control") or {}, identities, base)
            control_enforced = ctl.get("status") in _BLOCKED
            _issue(probe.get("test") or {}, identities, base)
            readback = _issue(probe.get("readback") or {}, identities, base)
            marker = probe.get("persisted_marker") or ""
            illegal_persisted = bool(marker) and str(marker) in str(readback.get("body") or "")
            res = inband_oracle.auth_state_illegal_transition(
                illegal_persisted, control_enforced,
                from_state=probe.get("from_state", ""), to_state=probe.get("to_state", ""))
        elif oracle == "stored_xss_victim_context":
            # Re-verify DELIVERY to the victim in-band (marker persisted + visible cross-identity);
            # EXECUTION proof is the OOB/browser signal the caller supplies (sound: in-band alone
            # cannot prove script execution).
            author_id = probe.get("author_identity")
            victim_id = probe.get("victim_identity")
            if probe.get("store"):
                _issue(probe.get("store"), identities, base)
            view = _issue(probe.get("view") or {}, identities, base)
            marker = str(probe.get("marker") or "")
            delivered = bool(marker) and marker in str(view.get("body") or "")
            executed = bool(probe.get("executed_in_victim_context")) and delivered
            res = inband_oracle.stored_xss_victim_context(author_id, victim_id, executed, marker)
        elif oracle == "sql_error_differential":
            ctl = _issue(probe.get("control") or {}, identities, base)
            tst = _issue(probe.get("test") or {}, identities, base)
            res = inband_oracle.sql_error_differential(ctl, tst, probe.get("markers"))
        elif oracle == "boolean_sqli":
            baseline = _issue(probe.get("baseline") or {}, identities, base)
            tr = _issue(probe.get("true") or probe.get("true_req") or {}, identities, base)
            fa = _issue(probe.get("false") or probe.get("false_req") or {}, identities, base)
            res = inband_oracle.boolean_sqli(baseline, tr, fa)
        elif oracle == "cross_identity_access":
            victim = _issue(probe.get("victim") or {}, identities, base)
            attacker = _issue(probe.get("attacker") or {}, identities, base)
            control = _issue(probe.get("control"), identities, base) if probe.get("control") else None
            res = inband_oracle.cross_identity_access(victim, attacker, probe.get("victim_marker"), control)
        elif oracle == "cross_tenant_write_persist":
            # Attacker (tenant A) writes a marker into a SHARED resource; victim (tenant B) reads the
            # same resource and must see A's marker. control = the resource before/without the write.
            control = _issue(probe.get("control"), identities, base) if probe.get("control") else None
            attacker_write = _issue(probe.get("write") or probe.get("attacker") or {}, identities, base)
            victim_read = _issue(probe.get("victim_read") or probe.get("victim") or {}, identities, base)
            res = inband_oracle.cross_tenant_write_persist(
                attacker_write, victim_read, probe.get("injected_marker") or probe.get("marker"), control)
        elif oracle == "auth_bypass":
            # Re-issue the escalated path (e.g. the injected login) and the legitimate control (wrong
            # creds / normal role), then confirm the privilege marker appears only on the escalated one.
            escalated = _issue(probe.get("escalated") or probe.get("test") or {}, identities, base)
            control = _issue(probe.get("control"), identities, base) if probe.get("control") else None
            res = inband_oracle.auth_bypass(escalated, control, probe.get("privilege_marker") or probe.get("marker"))
        elif oracle == "behavioral_differential":
            # Measure the control-vs-test divergence TWICE ourselves; `replayed` iff it reproduces.
            t1 = _issue(probe.get("test") or {}, identities, base)
            c1 = _issue(probe.get("control") or {}, identities, base)
            t2 = _issue(probe.get("test") or {}, identities, base)
            c2 = _issue(probe.get("control") or {}, identities, base)
            marker = probe.get("marker")
            min_delta = probe.get("min_delta")
            if min_delta is None:
                min_delta = 32  # default: a 32+ char body-length divergence
            def _mk(resp):
                return bool(marker) and str(marker) in str(resp.get("body") or "")
            def _delta(a, b):
                return abs(len(str(a.get("body") or "")) - len(str(b.get("body") or "")))
            marker_rep = _mk(t1) and _mk(t2) and not _mk(c1) and not _mk(c2)
            metric_rep = _delta(t1, c1) >= float(min_delta) and _delta(t2, c2) >= float(min_delta)
            res = inband_oracle.behavioral_differential(
                c1, t1, marker=marker, control_metric=len(str(c1.get("body") or "")),
                test_metric=len(str(t1.get("body") or "")), min_delta=min_delta,
                replayed=bool(marker_rep or metric_rep))
        elif oracle == "single_use":
            # Issue the one-time action sequentially and count successes; a provided control that is
            # BLOCKED establishes the limit genuinely exists.
            action = probe.get("action") or probe.get("test") or {}
            attempts = max(1, min(int(probe.get("attempts") or 2), 10))
            statuses = [_issue(action, identities, base).get("status") for _ in range(attempts)]
            observed = sum(1 for s in statuses if s and 200 <= s < 300)
            ctl = _issue(probe.get("control"), identities, base) if probe.get("control") else None
            control_enforced = bool(ctl and ctl.get("status") in _BLOCKED)
            res = inband_oracle.single_use(probe.get("allowed_uses", 1), observed, control_enforced)
        elif oracle == "value_floor":
            # Apply the mutation, read back, and parse the value from the response OURSELVES.
            if probe.get("mutate"):
                _issue(probe.get("mutate"), identities, base)
            readback = _issue(probe.get("readback") or probe.get("test") or {}, identities, base)
            observed_value = _dig(readback.get("body"), probe.get("value_path"))
            persisted = observed_value is not None and readback.get("status") not in _BLOCKED
            res = inband_oracle.value_floor(observed_value, probe.get("floor", 0), persisted)
        elif oracle == "monotonic_state":
            # Read the state before and after the illegal transition; map states to ranks ourselves.
            order = probe.get("state_order") or []
            rank = {str(s): i for i, s in enumerate(order)}
            before = _issue(probe.get("before") or {}, identities, base)
            if probe.get("transition"):
                _issue(probe.get("transition"), identities, base)
            after = _issue(probe.get("after") or probe.get("readback") or {}, identities, base)
            from_state = str(_dig(before.get("body"), probe.get("state_path")) or "")
            to_state = str(_dig(after.get("body"), probe.get("state_path")) or "")
            ctl = _issue(probe.get("control"), identities, base) if probe.get("control") else None
            control_enforced = bool(ctl and ctl.get("status") in _BLOCKED)
            res = inband_oracle.monotonic_state(rank.get(from_state, -1), rank.get(to_state, -1),
                                                control_enforced, from_state=from_state, to_state=to_state)
        else:
            return {**unavailable, "note": f"unknown oracle '{oracle}'"}
        return {"verification": evidence_mod.INBAND_VERIFIER, "available": True, **res}
    except Exception as exc:
        return {**unavailable, "error": str(exc)}
