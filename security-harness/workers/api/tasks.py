"""API discovery worker.

api_discover probes for exposed API specs (OpenAPI/Swagger, GraphQL
introspection) on the target, extracts the declared endpoints (which feed the
attack-surface so the planner + active scan cover them), and flags the exposure.
"""

import json
import logging
from urllib.parse import urljoin

import requests
import urllib3
from conductor.client.worker.worker_task import worker_task

from common import scope as scope_mod
from common.auth import auth_headers
from common.findings import LOW, MEDIUM, finding
from common.net import reachable_url

try:
    import yaml  # optional, for *.yaml specs
except Exception:  # pragma: no cover
    yaml = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

SPEC_PATHS = [
    "/swagger.json", "/openapi.json", "/v2/api-docs", "/v3/api-docs",
    "/api-docs", "/api-docs/swagger.json", "/api-docs/swagger.yaml",
    "/swagger/v1/swagger.json", "/swagger.yaml", "/openapi.yaml",
    "/.well-known/openapi.json",
]
USER_AGENT = "security-conductor/0.1 (+authorized-scan)"
TIMEOUT = 12


@worker_task(task_definition_name="api_discover", thread_count=2)
def api_discover(task):
    inp = task.input_data or {}
    base_url = str(inp.get("base_url") or "").strip()
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    scope = scope or scope_mod.derive_scope(base_url)
    if not base_url:
        return {"endpoints": [], "findings": [], "meta": {"skipped": "no base_url"}}

    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})
    sess.headers.update(auth_headers(inp.get("auth")))
    sess.verify = False
    endpoints, findings, specs = [], [], []

    for path in SPEC_PATHS:
        url = urljoin(base_url + "/", path.lstrip("/"))
        if not scope_mod.in_scope(url, scope):
            continue
        try:
            r = sess.get(reachable_url(url), timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code != 200 or not r.text.strip():
            continue
        spec = _parse_spec(r.text)
        if not spec or "paths" not in spec:
            continue
        specs.append(path)
        findings.append(finding(
            title="API documentation publicly exposed", source_tool="api_discover",
            severity_hint=LOW, location=url, cwe="CWE-200",
            owasp="A05:2021 - Security Misconfiguration",
            evidence=f"OpenAPI/Swagger spec served at {path} ({len(spec.get('paths', {}))} paths)",
            description="An unauthenticated API specification exposes the full endpoint surface."))
        for p, methods in (spec.get("paths") or {}).items():
            if not isinstance(methods, dict):
                continue
            for method in methods:
                if method.lower() in ("get", "post", "put", "delete", "patch"):
                    endpoints.append({"url": urljoin(base_url, p), "method": method.upper()})

    findings += _graphql(sess, base_url, scope)

    # de-dupe endpoints
    seen, uniq = set(), []
    for e in endpoints:
        k = (e["method"], e["url"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return {"endpoints": uniq, "findings": findings,
            "meta": {"specs": specs, "endpoint_count": len(uniq)}}


def _parse_spec(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if yaml is not None:
        try:
            d = yaml.safe_load(text)
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    return None


def _graphql(sess, base_url, scope):
    url = urljoin(base_url + "/", "graphql")
    if not scope_mod.in_scope(url, scope):
        return []
    q = {"query": "{__schema{queryType{name}}}"}
    try:
        r = sess.post(reachable_url(url), json=q, timeout=TIMEOUT)
    except requests.RequestException:
        return []
    if r.status_code == 200 and "__schema" in r.text:
        return [finding(
            title="GraphQL introspection enabled", source_tool="api_discover",
            severity_hint=MEDIUM, location=url, cwe="CWE-200",
            owasp="A05:2021 - Security Misconfiguration",
            evidence="POST /graphql returned a populated __schema for an introspection query",
            description="Introspection lets an attacker enumerate the entire GraphQL schema; "
                        "disable it in production.")]
    return []
