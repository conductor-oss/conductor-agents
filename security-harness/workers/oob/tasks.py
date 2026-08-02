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
    return {"status": r.status_code, "body": r.text[:20000]}


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
        else:
            return {**unavailable, "note": f"unknown oracle '{oracle}'"}
        return {"verification": evidence_mod.INBAND_VERIFIER, "available": True, **res}
    except Exception as exc:
        return {**unavailable, "error": str(exc)}
