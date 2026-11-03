"""sc -- the in-sandbox helper the exploit agent's Python imports.

This file is copied into the ephemeral container's workspace as ``sc.py`` and is
the ONLY sanctioned way for agent-authored code to touch the target. It gives the
agent everything it needs to *operate the product* (drive real multi-step flows
as a documented user) and then *attack* it, while keeping the run safe and
auditable:

  - ``sc.session(identity)``  pre-authed, scope-enforced ``requests.Session``
                              (refuses out-of-scope hosts; never sends creds off-target)
  - ``sc.api(...)``           thin REST helper on top of the session (returns parsed JSON+meta)
  - ``sc.tag(kind)``          a unique ``sc-pentest-<runid>-...`` name for any object you create
  - ``sc.oob(label)``         mint a unique OOB canary URL to plant in HTTP tasks/webhooks/etc.
  - ``sc.created(method,url)`` record a resource you created so the harness can tear it down
  - ``sc.evidence(...)`` / ``sc.finding(...)`` structured output the agent loop reads back

Everything the agent prints or returns is captured; results are written to
``/work/out.json`` so the worker can parse them deterministically. State (created
ids, a logged-in session) persists across steps via the mounted workspace.

Config arrives via env (set by the code_exec worker): SC_TARGET, SC_SCOPE (json),
SC_IDENTITIES (json label->{header,value}), SC_DEFAULT_IDENTITY, SC_OOB_BASE,
SC_RUN_ID, SC_WORK (workspace dir).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import time
import uuid
from urllib.parse import urlparse, urlsplit, urlunsplit

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _reachable(url: str) -> str:
    """The sandbox runs INSIDE a container, so a localhost target actually lives on the Docker
    HOST. Rewrite localhost/127.0.0.1 -> host.docker.internal for the actual request (scope is still
    enforced on the ORIGINAL url, mirroring the http_request worker's reachable_url)."""
    p = urlsplit(url if "://" in url else f"//{url}", scheme="http")
    if (p.hostname or "").lower() in _LOCAL_HOSTS:
        port = f":{p.port}" if p.port else ""
        return urlunsplit((p.scheme or "http", f"host.docker.internal{port}", p.path, p.query, p.fragment))
    return url

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TARGET = os.environ.get("SC_TARGET", "").rstrip("/")
target = TARGET  # lowercase alias: agent code / prompt examples use sc.target
SCOPE = json.loads(os.environ.get("SC_SCOPE") or "{}")
MANIFEST = json.loads(os.environ.get("SC_MANIFEST") or "{}")
IDENTITIES = json.loads(os.environ.get("SC_IDENTITIES") or "{}")
DEFAULT_IDENTITY = os.environ.get("SC_DEFAULT_IDENTITY") or "anon"
OOB_BASE = (os.environ.get("SC_OOB_BASE") or "").rstrip("/")
RUN_ID = os.environ.get("SC_RUN_ID") or "run"
WORK = os.environ.get("SC_WORK") or "/work"

_PREFIX = f"sc-pentest-{RUN_ID[:8]}-"

# Accumulators persist across the exploit agent's code-exec turns.  ``out.json`` is the
# current step's deterministic result; ``state.json`` survives the worker clearing
# ``out.json`` before the next container starts.
_STATE_FILE = os.path.join(WORK, "state.json")
try:
    with open(_STATE_FILE) as _fh:
        _state = json.load(_fh)
except (OSError, ValueError):
    _state = {}
for _key in ("created", "evidence", "findings", "oob", "operations", "inband"):
    if not isinstance(_state.get(_key), list):
        _state[_key] = []


def _host(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"//{url}", scheme="https").netloc
    return (netloc.split("@")[-1].split(":")[0] or "").lower()


def in_scope(url: str) -> bool:
    """True iff url's host is an allowed target-scope host.

    OOB URLs are values to plant in the target application, never destinations
    that generated sandbox code may fetch directly.  This prevents a sandbox
    step from manufacturing the callback used as blind-exploitation evidence.
    """
    h = _host(url)
    if not h:
        return False
    hosts = [x.lower() for x in (SCOPE.get("in_scope_hosts") or [])]
    if h in hosts:
        return True
    if SCOPE.get("allow_subdomains"):
        return any(h == x or h.endswith("." + x) for x in hosts)
    return False


class OutOfScope(Exception):
    pass


class Forbidden(Exception):
    pass


def _forbids(method: str, url: str) -> bool:
    """True iff ``method url`` matches a manifest forbidden_operations glob or touches a
    protected_records token. A self-contained port of ``common.authz.forbids`` -- the
    hardened sandbox container cannot import the common package, so this MUST stay in
    sync with that canonical implementation."""
    if not isinstance(MANIFEST, dict):
        return False
    m = (method or "GET").upper()
    path = urlparse(url).path or url
    candidates = (f"{m} {path}".lower(), f"{m} {url}".lower(), url.lower())
    for pat in MANIFEST.get("forbidden_operations") or []:
        p = str(pat).lower()
        if any(fnmatch.fnmatch(c, p) for c in candidates):
            return True
    for rec in MANIFEST.get("protected_records") or []:
        if str(rec).lower() in url.lower():
            return True
    return False


class _ScopedSession(requests.Session):
    """A requests.Session that refuses any out-of-scope host -- the hard safety
    boundary even though agent code is arbitrary -- and any manifest-forbidden
    operation / protected record."""

    def request(self, method, url, **kw):
        if not in_scope(url):
            raise OutOfScope(f"refusing out-of-scope request: {method} {url}")
        if _forbids(method, url):
            raise Forbidden(f"refusing manifest-forbidden operation: {method} {url}")
        kw.setdefault("timeout", 30)
        kw.setdefault("verify", False)
        kw.setdefault("allow_redirects", False)
        # Scope was checked on the original url; send to the container-reachable host.
        r = super().request(method, _reachable(url), **kw)
        # Record product operations at the SESSION layer so the campaign operation_ledger
        # captures every workflow define/start/write -- regardless of whether the agent used
        # sc.api() or a raw sc.session() request. (Previously recording lived only in sc.api(),
        # so an agent that drove the engine via sc.session() left operations empty even though
        # it created resources via sc.created(); feature_exercise then reported 0 workflows.)
        try:
            body = r.text or ""
            try:
                parsed = r.json()
            except Exception:
                parsed = None
            _record_api_operation(method.upper(), url, kw.get("json"), r.status_code, parsed, body)
        except Exception:
            pass
        return r


def session(identity: str | None = None) -> _ScopedSession:
    """Return a scope-enforced session pre-authed as ``identity`` (default: the
    first supplied credential, else anon)."""
    label = identity or DEFAULT_IDENTITY
    s = _ScopedSession()
    s.headers["User-Agent"] = "security-conductor/0.2 (+authorized-pentest)"
    cred = IDENTITIES.get(label) or {}
    if cred.get("header") and cred.get("value"):
        s.headers[cred["header"]] = cred["value"]
    return s


def api(method: str, path_or_url: str, identity: str | None = None, **kw):
    """REST convenience: resolve a bare path against TARGET, fire as ``identity``,
    return {status, json, text, url}. Never raises on HTTP status."""
    url = path_or_url if "://" in path_or_url else f"{TARGET}/{path_or_url.lstrip('/')}"
    s = session(identity)
    try:
        r = s.request(method.upper(), url, **kw)
    except (OutOfScope, Forbidden):
        raise
    body = r.text or ""
    try:
        parsed = r.json()
    except Exception:
        parsed = None
    # Operation recording now happens in _ScopedSession.request (above), so it covers raw
    # sc.session() use too; api() no longer records explicitly to avoid double-counting.
    return {"status": r.status_code, "json": parsed, "text": body[:8000], "url": r.url}


def identities() -> list[str]:
    """Labels of the identities available this run (e.g. ['anon','admin'])."""
    return list(IDENTITIES.keys())


def tag(kind: str = "obj") -> str:
    """A unique, scoped name for any object you create, so it is identifiable and
    auto-cleanable: ``sc-pentest-<runid>-<kind>-<rand>``."""
    return f"{_PREFIX}{kind}-{uuid.uuid4().hex[:8]}"


def oob(label: str = "probe") -> str:
    """Mint a unique OOB canary URL to plant in an HTTP task / webhook / queue
    config. A later inbound hit on it confirms blind SSRF/exfil/exec. Returns the
    URL; returns '' if no collaborator is configured."""
    if not OOB_BASE:
        return ""
    token = f"{RUN_ID[:8]}-{label}-{uuid.uuid4().hex[:10]}"
    url = f"{OOB_BASE}/c/{token}"
    _state["oob"].append({"token": token, "label": label, "url": url})
    flush()
    return url


def inband_probe(oracle: str, **spec):
    """Record an IN-BAND oracle PLAN for the trusted verifier to RE-ISSUE independently (it never
    trusts this sandbox's own responses -- only a verifier receipt can confirm). ``spec`` carries the
    request spec(s) + markers/operands; the verifier re-sends and grades. Supported oracles:
      inband_probe('ssrf_differential', control={req}, test={req}, markers=[...])
      inband_probe('rce_arithmetic', request={req}, operand_a=1337, operand_b=7919)
      inband_probe('time_blind_sql', test={req}, control={req}, injected_delay_ms=5000)
      inband_probe('sql_error_differential', control={req_benign}, test={req_injection})
      inband_probe('boolean_sqli', baseline={req}, true={req_AND_1=1}, false={req_AND_1=2})
      inband_probe('cross_identity_access', victim={req}, attacker={req}, victim_marker='...', control={req})
      inband_probe('path_traversal_canary', test={req}, control={req}, canary='root:x:0:0')
      inband_probe('race_invariant', boundary_request={req}, allowed_successes=1, burst=10)
      inband_probe('auth_state_illegal_transition', control={req}, test={req}, readback={req},
                   persisted_marker='refunded', from_state='shipped', to_state='refunded')
      inband_probe('stored_xss_victim_context', store={req}, view={req}, marker='<unique>',
                   author_identity='userA', victim_identity='admin',
                   executed_in_victim_context=<True only with an OOB/browser exec signal>)
      inband_probe('cross_tenant_write_persist', write={req_as_A}, victim_read={req_as_B}, control={req_before},
                   injected_marker='https://attacker.example/v1')
                   # SHARED-RESOURCE / CROSS-TENANT POISONING: tenant A WRITEs a marker into a GLOBAL
                   # shared resource (a shared integration-provider definition, a global registry/config
                   # entry) that has no ownership check; tenant B READs the same resource and sees A's
                   # marker. Use for "any tenant can overwrite a shared provider endpoint / registry
                   # entry visible to everyone". write/victim_read carry different `identity`s.
      inband_probe('auth_bypass', escalated={req}, control={req}, privilege_marker='admin@juice-sh.op')
                   # AUTH/AUTHZ BYPASS: escalated = the illegitimate path that grants access (an
                   # injected login, a forged-JWT request, a registration carrying role=admin);
                   # control = the legitimate path that must NOT grant it (wrong password -> 401,
                   # a normal-role request -> 403, a normal registration). privilege_marker = the
                   # distinctive proof of elevation present ONLY on escalated (admin email/id, an
                   # admin-only field, "role":"admin"). Use for login SQLi, JWT forgery, mass-assignment.
      inband_probe('behavioral_differential', control={req}, test={req}, marker='<leaked-str>',
                   min_delta=32)   # class-AGNOSTIC: prove a reproducible control-vs-test divergence
                   # (a leaked marker, or a body-length delta >= min_delta) when no named oracle fits.
      inband_probe('single_use', action={req}, control={req_that_should_be_blocked},
                   allowed_uses=1, attempts=3)   # coupon reuse / one-time-token replay
      inband_probe('value_floor', mutate={req}, readback={req}, value_path='data.0.quantity', floor=0)
                   # negative quantity/price/balance: verifier reads value_path out of the readback
      inband_probe('monotonic_state', before={req}, transition={req}, after={req}, control={req},
                   state_path='data.status', state_order=['in_cart','paid','shipped','delivered'])

    LEVEL-4 oracles (destructive / availability / real-data extraction) -- the verifier executes
    these ONLY when the campaign is authorized to capability 4; plant them only for a cap-4 run:
      inband_probe('data_exfiltration', extract={req}, record_path='data', min_records=5)
                   # BULK REAL-DATA EXTRACTION: the one request that dumps a volume of real records
                   # (SQLi UNION over a user/credential table, mass IDOR enumeration, unauth export).
                   # record_path = the JSON path to the row list; the verifier re-issues, counts
                   # DISTINCT rows, and classifies the body for sensitive data (pii/secret/pci/...).
      inband_probe('destructive_persist', pre_readback={req}, destroy={req}, post_readback={req},
                   marker='<distinctive-content-of-the-resource>')
                   # IRREVERSIBLE DESTRUCTION: prove the resource EXISTS (pre 2xx w/ marker), run the
                   # destroy op (DELETE/corrupt), prove it is GONE (post 404/410 or marker removed).
      inband_probe('availability_degradation', request={req}, concurrency_ramp=[1,2,4,8])
                   # AVAILABILITY/DoS: a bounded authorized load ramp; verifier measures the p95/error
                   # knee. Needs capability 4 AND manifest allowed_classes:[resilience].
    where each ``req`` is {method, path|url, headers, body, identity}.

    RETURNS an acknowledgement dict (NOT a confirmation): the plan is now queued for the trusted
    verifier, which RE-ISSUES the requests itself and grades them — confirmation arrives as a verifier
    receipt in your next status brief, never as this return value. A non-None ack means "planted OK";
    do NOT re-plant the same probe because this returned no verdict. That is by design."""
    rec = {"oracle": str(oracle), **spec}
    _state["inband"].append(rec)
    flush()
    return {"planted": str(oracle), "status": "recorded",
            "fields": sorted(k for k in spec.keys()),
            "note": "Verifier will RE-ISSUE this independently; the verdict appears in your next status "
                    "brief as a verifier receipt, not here. Do NOT re-plant — move to the next rung or "
                    "conclude. A missing field would show here as an absent key."}


def created(method: str, url: str, identity: str | None = None):
    """Record a resource you created (usually so a DELETE can tear it down)."""
    _state["created"].append({"method": (method or "DELETE").upper(), "url": url,
                              "identity": identity or DEFAULT_IDENTITY})
    flush()


def evidence(note: str, **fields):
    """Record a structured observation the agent loop reads back."""
    _state["evidence"].append({"ts": int(time.time()), "note": str(note)[:2000], **fields})
    flush()


def finding(title: str, severity: str = "Info", category: str = "other", **fields):
    """Record a candidate finding with evidence the verifier can re-check."""
    _state["findings"].append({"title": str(title)[:300], "severity": severity,
                               "category": category, **fields})
    flush()


def operation(kind: str, **fields):
    """Record a bounded, non-secret product operation for the campaign ledger."""
    allowed = {
        "method", "path", "status", "workflow_name", "execution_id", "task_types",
        "note", "cve_id", "dependency", "blocked_reason", "family",
    }
    rec = {"type": str(kind)[:80]}
    for key in allowed:
        if key in fields and fields[key] not in (None, ""):
            rec[key] = fields[key]
    _state["operations"].append(rec)
    flush()


# Technique families for the exploit-deepening loop (design/DEEP_EXPLOITATION.md §2/§6): a small, fixed
# vocabulary so per-family coverage is machine-groupable. Unknown/absent -> "other".
_FAMILY_TOKENS = {
    "reflection-breakout", "alternate-engine", "encoding-bypass", "gadget-chain", "oob-exfil",
    "syntactic", "channel-variant", "timing", "chain", "other",
}


def _norm_family(family: str) -> str:
    f = str(family or "").strip().lower()
    return f if f in _FAMILY_TOKENS else "other"


def cve_attempt(cve_id: str, dependency: str = "", note: str = "", family: str = ""):
    """Explicitly mark a crafted CVE attempt after the payload was issued."""
    operation("cve_attempt", cve_id=cve_id, dependency=dependency, note=note,
              family=_norm_family(family))


def injection_attempt(sink: str = "", note: str = "", family: str = ""):
    """Mark a crafted code/expression/template/command/deserialization-injection attempt AFTER
    sending the payload, so the operation ledger distinguishes an ACTIVE exploit attempt from a
    static SAST report. Plant ``sc.oob()`` inside the payload and confirm via the OOB hit (the
    decisive proof of server-side execution for a blind sink). Pass ``family=`` (one of the
    deepening technique families) so per-family coverage can drive reflect to try untried families;
    an absent/unknown family normalizes to ``other``."""
    detail = f"{sink}: {note}".strip(": ") if (sink or note) else "injection payload issued"
    operation("injection_attempt", note=detail, family=_norm_family(family))


def _task_types(obj) -> list[str]:
    out = set()

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("type"), str):
                out.add(value["type"].upper())
            for key in ("tasks", "forkTasks", "loopOver", "decisionCases", "defaultCase", "workflowDef"):
                child = value.get(key)
                if child is not None:
                    walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return sorted(out)


def _execution_id(parsed, body: str) -> str:
    if isinstance(parsed, str):
        return parsed
    if isinstance(parsed, dict):
        for key in ("workflowId", "workflow_id", "id"):
            if parsed.get(key):
                return str(parsed[key])
    text = (body or "").strip().strip('"')
    return text if re.fullmatch(r"[A-Za-z0-9._:-]{8,}", text) else ""


# Feature-operation classification rules — DATA-DRIVEN, supplied per target via SC_FEATURE_OPS
# (a profile, e.g. profiles/conductor.json `feature_operations`, declares how THAT product's
# "define a primitive / run it / poll it" calls map to ledger op types, so the §E11 feature-
# exercise metric is product-neutral). The ENGINE hardcodes NO product's API: with no rules, a
# state-changing call is simply a generic ``product_write``. Each rule:
#   {type, method, path | path_regex,
#    capture: {name_field, name_group, task_types, execution_id_response, execution_id_group}}
try:
    _FEATURE_OPS = json.loads(os.environ.get("SC_FEATURE_OPS") or "[]")
    _FEATURE_OPS = _FEATURE_OPS if isinstance(_FEATURE_OPS, list) else []
except Exception:
    _FEATURE_OPS = []


def _record_api_operation(method: str, url: str, request_json, status: int, parsed, body: str):
    path = urlparse(url).path
    rpath = path.rstrip("/")
    for rule in _FEATURE_OPS:
        if not isinstance(rule, dict) or str(rule.get("method", "")).upper() != method:
            continue
        rx = rule.get("path_regex")
        m = re.fullmatch(rx, rpath) if rx else None
        if not (m if rx else (rpath == rule.get("path"))):
            continue
        cap = rule.get("capture") or {}
        base = {"method": method, "path": path, "status": status}
        if m and cap.get("name_group"):
            base["workflow_name"] = m.group(int(cap["name_group"]))
        if cap.get("execution_id_group") and m:
            base["execution_id"] = m.group(int(cap["execution_id_group"]))
        elif cap.get("execution_id_response"):
            base["execution_id"] = _execution_id(parsed, body)
        # Body-derived captures (define a primitive): one op per definition when the body is a list.
        if cap.get("name_field") or cap.get("task_types"):
            defs = request_json if isinstance(request_json, list) else [request_json]
            for d in defs:
                if not isinstance(d, dict):
                    continue
                f = dict(base)
                if cap.get("name_field"):
                    f["workflow_name"] = str(d.get(cap["name_field"]) or "")
                if cap.get("task_types"):
                    f["task_types"] = _task_types(d)
                operation(rule.get("type") or "product_call", **f)
        else:
            operation(rule.get("type") or "product_call", **base)
        return

    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        operation("product_write", method=method, path=path, status=status)


def flush():
    """Persist accumulators to /work/out.json (also called automatically)."""
    try:
        with open(_STATE_FILE, "w") as fh:
            json.dump(_state, fh)
        with open(os.path.join(WORK, "out.json"), "w") as fh:
            json.dump(_state, fh)
    except Exception:
        pass


import atexit  # noqa: E402

atexit.register(flush)
