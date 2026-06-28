"""Generic HTTP request worker — the exploit/explore agent's primary "hands".

Unlike the fixed active-check probes, this lets the LLM agent craft ANY request
(method, url, headers, json/form/raw body) as ANY supplied identity, so it can
test app-specific logic: flip IDs (IDOR/BOLA), call admin ops as a low-priv user
(privesc), craft mass-assignment bodies, chain create→run→read, etc.

Safety: every request is scope-enforced (refuses out-of-scope hosts), the worker
NEVER raises (errors come back in the result so an agent step can't crash the
loop), and recorded request evidence has auth/cookie headers redacted so tokens
don't leak into findings/reports. In a container, localhost targets are rewritten
to host.docker.internal (findings keep the original URL).
"""

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests
import urllib3
from conductor.client.worker.worker_task import worker_task

from common import auditlog
from common import authz
from common import halt as halt_mod
from common import loadknee
from common import scope as scope_mod
from common import sensitive
from common.auth import auth_headers
from common.net import reachable_url

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

USER_AGENT = "security-conductor/0.1 (+authorized-pentest)"
TIMEOUT = 20
BODY_LIMIT = 4000
BURST_MAX = 20  # cap concurrent requests so race testing can't become a flood/DoS
_SENSITIVE = {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token", "token"}


def _redact(headers):
    return {k: ("<redacted>" if k.lower() in _SENSITIVE else v) for k, v in (headers or {}).items()}


def _operation(method, url, identity):
    """Return a secret-free ledger record for one direct product interaction."""
    try:
        path = sensitive.redact(urlsplit(url).path or "/", 500)
    except ValueError:
        path = ""
    return {
        "type": "http_request",
        "method": method,
        "path": path,
        "identity": identity,
    }


def _fire(method, target, kwargs):
    """Issue one request; return a (status, body, size, elapsed_ms, final_url, error) tuple."""
    try:
        r = requests.request(method, target, **kwargs)
        return (r.status_code, r.text or "", len(r.content),
                int(r.elapsed.total_seconds() * 1000), r.url, dict(r.headers), None)
    except requests.RequestException as exc:
        return (None, "", 0, 0, "", {}, f"request error: {exc}")


@worker_task(task_definition_name="http_request", thread_count=8)
def http_request(task):
    inp = task.input_data or {}
    method = (inp.get("method") or "GET").upper()
    url = str(inp.get("url") or "").strip()
    # Resolve a bare/relative path (e.g. "/api/x") against the target base so an agent action
    # that omits the scheme/host still fires instead of failing with "no url provided".
    target = str(inp.get("target") or "").rstrip("/")
    if url and "://" not in url and target:
        url = f"{target}/{url.lstrip('/')}"
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    identities = inp.get("identities") if isinstance(inp.get("identities"), dict) else {}
    identity = inp.get("identity") or "anon"
    extra = inp.get("headers") if isinstance(inp.get("headers"), dict) else {}
    json_body = inp.get("json")
    raw_body = inp.get("data")
    follow = inp.get("follow_redirects")
    follow = True if follow is None else bool(follow)
    manifest = inp.get("manifest") if isinstance(inp.get("manifest"), dict) else {}
    # Conductor passes an absent numeric input as "" -> default, don't crash.
    try:
        capability_max = int(inp.get("capability_max")) if str(inp.get("capability_max") or "").strip() else 1
    except (TypeError, ValueError):
        capability_max = 1

    result = {"identity": identity, "request": {"method": method, "url": url},
              "response": {}, "error": "", "summary": "",
              "operation": _operation(method, url, identity)}

    if not url:
        result["error"] = "no url provided"
        result["operation"]["blocked_reason"] = result["error"]
        return result
    if scope and not scope_mod.in_scope(url, scope):
        result["error"] = "refused: out of scope"
        result["operation"]["blocked_reason"] = result["error"]
        result["summary"] = f"{method} {url} [{identity}] -> REFUSED (out of scope)"
        return result
    # Capability gate (spec 15.1): the harness cannot exceed its authorized level.
    needed = authz.action_capability(method, is_code_exec=False, is_sensitive=False)
    if needed > capability_max:
        result["error"] = "refused: capability"
        result["refused_reason"] = (f"{method} needs capability level {needed} but the campaign "
                                    f"is authorized to level {capability_max}")
        result["operation"]["blocked_reason"] = (
            f"{method} requires capability {needed}; campaign has {capability_max}"
        )
        result["summary"] = f"{method} {url} [{identity}] -> REFUSED (capability {needed}>{capability_max})"
        return result
    if authz.forbids(method, url, manifest):
        result["error"] = "refused: forbidden operation"
        result["refused_reason"] = f"{method} {url} matches a manifest forbidden_operations/protected_records rule"
        result["operation"]["blocked_reason"] = "matches a manifest forbidden/protected-record rule"
        result["summary"] = f"{method} {url} [{identity}] -> REFUSED (forbidden)"
        return result

    headers = dict(extra)
    headers.update(auth_headers(identities.get(identity)))
    headers.setdefault("User-Agent", USER_AGENT)

    kwargs = {"timeout": TIMEOUT, "verify": False, "allow_redirects": follow, "headers": headers}
    if json_body is not None:
        kwargs["json"] = json_body
    elif raw_body is not None:
        kwargs["data"] = raw_body

    result["request"] = {
        "method": method, "url": url, "headers": _redact(headers),
        "body": json_body if json_body is not None
        else (raw_body[:BODY_LIMIT] if isinstance(raw_body, str) else None),
    }
    target = reachable_url(url)

    # burst mode: fire N concurrent identical requests to probe race conditions /
    # TOCTOU / double-spend (e.g. redeem a one-time coupon 10x at once). Capped.
    try:
        burst = int(inp.get("burst")) if str(inp.get("burst") or "").strip() else 1
    except (TypeError, ValueError):
        burst = 1
    burst_cap = BURST_MAX
    try:
        mb = (manifest.get("rate") or {}).get("burst_max")
        if mb:
            burst_cap = min(burst_cap, int(mb))
    except (TypeError, ValueError):
        pass
    burst = max(1, min(burst, burst_cap))

    if burst > 1:
        with ThreadPoolExecutor(max_workers=burst) as pool:
            fires = list(pool.map(lambda _: _fire(method, target, kwargs), range(burst)))
        statuses = [f[0] for f in fires]
        dist = {str(k): v for k, v in Counter("ERR" if s is None else s for s in statuses).items()}
        rep = next((f for f in fires if f[0] and 200 <= f[0] < 300), fires[0])
        status, body, size, elapsed, final_url, rhdrs, err = rep
        distinct = []
        for f in fires:
            ex_b = (f[1] or f[6] or "")[:300]
            if ex_b and ex_b not in distinct:
                distinct.append(ex_b)
            if len(distinct) >= 3:
                break
        ok2xx = sum(1 for s in statuses if s and 200 <= s < 300)
        result["response"] = {
            "status": status, "headers": rhdrs,
            "body_excerpt": sensitive.redact(body or err or "", BODY_LIMIT),
            "size": size, "elapsed_ms": elapsed, "final_url": final_url,
            "sensitive": {**sensitive.scan(body or ""), "classes": sensitive.classify(body or "")["classes"]},
        }
        result["burst"] = {"count": burst, "status_distribution": dist,
                           "ok_2xx": ok2xx, "distinct_bodies": distinct}
        result["operation"].update({
            "status": status,
            "note": f"bounded burst x{burst}; status distribution {dist}",
        })
        result["summary"] = (f"BURST x{burst} {method} {url} [{identity}] -> "
                             f"status_distribution={dist}, 2xx_successes={ok2xx}")
        _maybe_halt(result, method, url, final_url, manifest, scope)
        return result

    status, body, size, elapsed, final_url, rhdrs, err = _fire(method, target, kwargs)
    if err:
        result["error"] = err
        result["operation"]["blocked_reason"] = "request error"
        result["summary"] = f"{method} {url} [{identity}] -> ERROR"
        return result
    # Scan the FULL body for leaked secrets/PII (impact signal for data-exposure
    # findings), but store a REDACTED excerpt so we never persist customer data in
    # our own findings/reports.
    sens = sensitive.scan(body)
    sens["classes"] = sensitive.classify(body)["classes"]  # DLP data-class view (E2)
    result["response"] = {
        "status": status,
        "headers": rhdrs,
        "body_excerpt": sensitive.redact(body, BODY_LIMIT),
        "size": size,
        "truncated": len(body) > BODY_LIMIT,
        "elapsed_ms": elapsed,
        "final_url": final_url,
        "sensitive": sens,
    }
    result["summary"] = (f"{method} {url} [{identity}] -> {status} ({size}b)"
                         + (f" SENSITIVE={list(sens['secrets']) + list(sens['pii'])}" if sens["found"] else ""))
    result["operation"]["status"] = status
    _maybe_halt(result, method, url, final_url, manifest, scope)
    return result


def _host_of(url):
    from urllib.parse import urlparse
    netloc = urlparse(url if "://" in url else f"//{url}", scheme="http").netloc
    return (netloc.split("@")[-1].split(":")[0] or "").lower()


def _maybe_halt(result, method, url, final_url, manifest, scope):
    """Evaluate spec-15.2 halt conditions on this action and, if tripped, set
    result['halt_requested'] = {reason}. The workflow's safety governor reads this
    flag and terminates the campaign at the next pass boundary. Also writes a
    tamper-evident audit record for the action (best-effort)."""
    try:
        resp = result.get("response") or {}
        auditlog.append(_host_of(url), {
            "action": "http_request", "method": method, "url": url,
            "identity": result.get("identity"), "status": resp.get("status"),
            "summary": result.get("summary"),
        })
    except Exception:
        pass
    try:
        verdict = halt_mod.evaluate(
            {"method": method, "url": url,
             "final_url": (result.get("response") or {}).get("final_url") or final_url,
             "sensitive": (result.get("response") or {}).get("sensitive") or {}},
            manifest, scope)
        if verdict.get("halt"):
            result["halt_requested"] = {"reason": verdict.get("reason", "halt condition met")}
            result["summary"] += f"  [HALT REQUESTED: {verdict.get('reason')}]"
    except Exception:  # safety check must never crash the action
        pass


LOAD_RAMP = [1, 2, 4, 8]   # bounded ramp; never floods (max 8 concurrent)


@worker_task(task_definition_name="load_probe", thread_count=2)
def load_probe(task):
    """Resilience probe (ROADMAP E3): ramp a SMALL, bounded amount of concurrency at one
    endpoint and measure the latency/error knee, ABORTING the instant degradation appears
    (safe-by-construction — we locate the knee, we don't push past it). Gated: runs only
    when the manifest authorizes the 'resilience' class. Never raises, never floods."""
    inp = task.input_data or {}
    method = (inp.get("method") or "GET").upper()
    url = str(inp.get("url") or "").strip()
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    identities = inp.get("identities") if isinstance(inp.get("identities"), dict) else {}
    identity = inp.get("identity") or "anon"
    manifest = inp.get("manifest") if isinstance(inp.get("manifest"), dict) else {}
    json_body = inp.get("json")

    result = {"url": url, "steps": [], "analysis": {}, "summary": ""}
    if not url:
        result["summary"] = "no url"
        return result
    if not authz.resilience_allowed(manifest):
        result["summary"] = "refused: resilience class not authorized (add 'resilience' to manifest allowed_classes)"
        result["refused"] = True
        return result
    if scope and not scope_mod.in_scope(url, scope):
        result["summary"] = "refused: out of scope"
        return result

    cap = LOAD_RAMP[-1]
    try:
        mb = (manifest.get("rate") or {}).get("burst_max")
        if mb:
            cap = min(cap, int(mb))
    except (TypeError, ValueError):
        pass
    ramp = [c for c in LOAD_RAMP if c <= cap] or [1]

    headers = {"User-Agent": USER_AGENT}
    headers.update(auth_headers(identities.get(identity)))
    kwargs = {"timeout": TIMEOUT, "verify": False, "allow_redirects": False, "headers": headers}
    if json_body is not None:
        kwargs["json"] = json_body
    target = reachable_url(url)

    baseline_ms = 0.0
    for c in ramp:
        with ThreadPoolExecutor(max_workers=c) as pool:
            fires = list(pool.map(lambda _: _fire(method, target, kwargs), range(c)))
        lat = sorted(f[3] for f in fires)
        errs = sum(1 for f in fires if f[0] is None or (f[0] and f[0] >= 500))
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else 0
        step = {"concurrency": c, "p95_ms": p95, "error_rate": round(errs / max(1, len(fires)), 3)}
        result["steps"].append(step)
        if baseline_ms == 0.0:
            baseline_ms = float(p95) or 1.0
        v = loadknee.step_verdict(step, baseline_ms)
        if v["abort"]:
            result["aborted"] = v["reason"]
            break  # protect the target: stop ramping immediately
    result["analysis"] = loadknee.analyze(result["steps"])
    result["summary"] = result["analysis"]["summary"] + (f"  [{result.get('aborted')}]" if result.get("aborted") else "")
    return result


@worker_task(task_definition_name="cleanup_resources", thread_count=4)
def cleanup_resources(task):
    """Best-effort removal of resources the exploit agents created (the
    `created_resources` ledger): each entry is a {method,url,identity} cleanup
    request (usually DELETE). Scope-enforced, never raises; returns what was
    removed and what residue remains for the report to flag."""
    inp = task.input_data or {}
    ledger = inp.get("ledger") if isinstance(inp.get("ledger"), list) else []
    identities = inp.get("identities") if isinstance(inp.get("identities"), dict) else {}
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    base = str(inp.get("base_url") or inp.get("target") or "").rstrip("/")
    # a sensible default identity for deletes: the first non-anon credential
    default_ident = next((k for k, v in identities.items() if isinstance(v, dict) and v.get("value")), "anon")

    seen, deleted, residue = set(), [], []
    for item in ledger[:100]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        # Agents often ledger a RELATIVE path (e.g. /api/metadata/workflow/...). Resolve
        # it against the target base URL so the scope check has a host -- otherwise a
        # relative path has no host and is wrongly judged "out of scope" (and skipped).
        if url and "://" not in url and base:
            url = base + (url if url.startswith("/") else "/" + url)
        method = (item.get("method") or "DELETE").upper()
        ident = item.get("identity") or default_ident
        key = (method, url)
        if not url or key in seen:
            continue
        seen.add(key)
        if scope and not scope_mod.in_scope(url, scope):
            residue.append({"url": url, "reason": "out of scope"})
            continue
        headers = {"User-Agent": USER_AGENT}
        headers.update(auth_headers(identities.get(ident)))
        try:
            r = requests.request(method, reachable_url(url), headers=headers,
                                 timeout=TIMEOUT, verify=False, allow_redirects=False)
            if 200 <= r.status_code < 400 or r.status_code == 404:
                deleted.append({"url": url, "status": r.status_code})
            else:
                residue.append({"url": url, "status": r.status_code})
        except requests.RequestException as exc:
            residue.append({"url": url, "error": str(exc)})

    return {"attempted": len(seen), "deleted": deleted, "residue": residue,
            "summary": f"cleanup: removed {len(deleted)}, residue {len(residue)}"}


class _SafeMap(dict):
    """format_map helper: missing keys -> '' instead of KeyError (declarative templates)."""
    def __missing__(self, key):
        return ""


def _fam_name(obj, fam):
    """Extract an object's name per a declarative family spec. ``name_key: null`` means
    the listed item IS the name (string-valued lists, e.g. secret names)."""
    nk = fam.get("name_key")
    if nk is None:
        return obj if isinstance(obj, str) else (obj.get("name") if isinstance(obj, dict) else None)
    return obj.get(nk) if isinstance(obj, dict) else None


def _fam_delete_path(obj, name, fam):
    """Render the delete path template (e.g. '/api/metadata/workflow/{name}/{version}')
    against the object's fields + declared defaults. Generic — no platform code."""
    fields = _SafeMap(fam.get("defaults") or {})
    if isinstance(obj, dict):
        fields.update(obj)
    fields["name"] = name
    if fam.get("id_key") and isinstance(obj, dict):
        fields["id"] = obj.get(fam["id_key"])
    return str(fam.get("delete") or "").format_map(fields)


@worker_task(task_definition_name="sweep_resources", thread_count=2)
def sweep_resources(task):
    """Ledger-INDEPENDENT cleanup: enumerate every object whose name starts with the
    `sc-pentest-` prefix and delete it, regardless of whether the agent ledgered it
    (the reliable safety net since every test artifact is prefix-tagged).

    GENERIC: the resource families to sweep are supplied as data via the
    ``cleanup_families`` input (typically from an opt-in target profile, e.g.
    profiles/conductor.json). With NO families supplied this is a no-op (the
    ledger-based ``cleanup_resources`` is the generic default cleanup). Each family is
    a declarative dict {type, list, name_key, delete, defaults?, id_key?}. Self-skipping
    per family (a LIST that doesn't 2xx -> skip), scope-enforced, never raises."""
    inp = task.input_data or {}
    base = str(inp.get("base_url") or inp.get("target") or "").rstrip("/")
    identities = inp.get("identities") if isinstance(inp.get("identities"), dict) else {}
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    prefix = str(inp.get("prefix") or "sc-pentest-")
    families = inp.get("cleanup_families") if isinstance(inp.get("cleanup_families"), list) else []
    ident = inp.get("identity") or next(
        (k for k, v in identities.items() if isinstance(v, dict) and v.get("value")), "anon")
    if not base:
        return {"available": False, "swept": [], "residue": [], "summary": "no target for sweep"}
    if not families:
        return {"available": False, "swept": [], "residue": [],
                "summary": "no cleanup_families (no target profile) -> prefix-sweep skipped; "
                           "ledger cleanup still applied"}

    headers = {"User-Agent": USER_AGENT}
    headers.update(auth_headers(identities.get(ident)))
    swept, residue = [], []

    def _list(path):
        try:
            r = requests.get(reachable_url(base + path), headers=headers, timeout=TIMEOUT,
                             verify=False, allow_redirects=False)
            if not (200 <= r.status_code < 300):
                return None
            data = r.json()
            return data if isinstance(data, list) else (data.get("results") or data.get("schedules") or [])
        except (requests.RequestException, ValueError):
            return None

    for fam in families:
        if not isinstance(fam, dict) or not fam.get("list") or not fam.get("delete"):
            continue
        objs = _list(fam["list"])
        if objs is None:
            continue  # family unsupported on this target -> skip silently
        for o in objs:
            try:
                name = _fam_name(o, fam)
            except Exception:
                name = None
            if not name or not str(name).startswith(prefix):
                continue
            del_url = base + _fam_delete_path(o, name, fam)
            if scope and not scope_mod.in_scope(del_url, scope):
                residue.append({"type": fam.get("type"), "name": name, "reason": "out of scope"})
                continue
            try:
                dr = requests.delete(reachable_url(del_url), headers=headers, timeout=TIMEOUT,
                                     verify=False, allow_redirects=False)
                if 200 <= dr.status_code < 400 or dr.status_code == 404:
                    swept.append({"type": fam.get("type"), "name": name, "status": dr.status_code})
                else:
                    residue.append({"type": fam.get("type"), "name": name, "status": dr.status_code})
            except requests.RequestException as exc:
                residue.append({"type": fam.get("type"), "name": name, "error": str(exc)})

    return {"available": True, "swept": swept, "residue": residue,
            "summary": f"prefix-sweep: removed {len(swept)}, residue {len(residue)}"}
