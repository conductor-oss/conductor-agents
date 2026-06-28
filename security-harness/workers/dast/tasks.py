"""Active DAST worker.

`active_check` runs ONE planned check (from the LLM attack-planner) against the
target and returns normalized raw findings. The security_scan workflow fans
these out in parallel with FORK_JOIN_DYNAMIC, one task per planned check.

Each check_type maps to a precise, non-destructive Python HTTP probe; heavier
or intrusive tooling (nuclei / sqlmap / dalfox / ffuf) is invoked via subprocess
when the binary is present (and, for intrusive tools, only when the operator
opted in). Every request target is scope-enforced; in a container, localhost
targets are rewritten to host.docker.internal for reachability (findings still
report the original URL).
"""

import json
import logging
import re
import shutil
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import urllib3
from conductor.client.worker.worker_task import worker_task

from common import scope as scope_mod
from common.auth import auth_headers
from common.findings import CRITICAL, HIGH, INFO, LOW, MEDIUM, finding
from common.net import reachable_url

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

USER_AGENT = "security-conductor/0.1 (+authorized-scan)"
TIMEOUT = 12
SQL_ERRORS = re.compile(
    r"(SQL syntax|SQLITE_ERROR|sqlite3\.|mysql_fetch|valid MySQL result|"
    r"ORA-\d{4,5}|PostgreSQL.*ERROR|Unclosed quotation|SQLException|"
    r"pg_query\(\)|near \".*\": syntax error)", re.I)
PASSWD = re.compile(r"root:.*:0:0:")


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    s.verify = False
    return s


def _set_param(url, param, value):
    """Return url with query ?param=value set/replaced (param appended if absent)."""
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q[param or "q"] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


# ─────────────────────────────────────────────────────────────────────────────
@worker_task(task_definition_name="active_check", thread_count=8)
def active_check(task):
    inp = task.input_data or {}
    check = inp.get("check") or {}
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    base_url = str(inp.get("base_url") or "")
    intrusive_ok = bool(inp.get("intrusive_allowed"))
    scope = scope or scope_mod.derive_scope(base_url)

    ctype = (check.get("check_type") or "").lower()
    target = str(check.get("target") or base_url)
    if target.startswith("/"):
        target = base_url.rstrip("/") + target
    param = check.get("param") or ""

    if not scope_mod.in_scope(target, scope):
        return {"findings": [], "meta": {"check_id": check.get("id"), "skipped": "out_of_scope",
                                         "target": target}}

    sess = _session()
    sess.headers.update(auth_headers(inp.get("auth")))
    findings = []
    executed = True
    tool_used = "http_probe"
    try:
        if ctype == "nuclei":
            findings, tool_used, executed = _run_nuclei(target, inp.get("auth"))
        elif ctype == "open_redirect":
            findings = _probe_open_redirect(sess, target, param)
        elif ctype in ("auth", "info_disclosure", "idor"):
            findings = _probe_access(sess, target, ctype)
        elif ctype == "path_traversal":
            findings = _probe_path_traversal(sess, target, param)
        elif ctype in ("sqli",):
            findings, tool_used = _probe_sqli(sess, target, param, intrusive_ok)
        elif ctype in ("xss",):
            findings, tool_used = _probe_xss(sess, target, param, intrusive_ok)
        elif ctype in ("misconfig",):
            findings = _probe_cors(sess, target)
        else:
            executed = False  # ssrf/csrf/injection_other/api_fuzz: not auto-confirmed here
            findings = [finding(
                title=f"Planned {ctype or 'check'} not auto-executed",
                source_tool="active_check", severity_hint=INFO, location=target,
                evidence=f"check_type={ctype!r} requires manual or out-of-band testing",
                description=check.get("technique", ""),
                owasp=check.get("owasp", ""))]
    except Exception as exc:
        # A single check must NEVER fail the fan-out / workflow. Contain it.
        executed = False
        log.warning("active_check %s (%s) errored, returning empty: %s",
                    check.get("id"), ctype, exc)
        findings = []

    return {"findings": findings,
            "meta": {"check_id": check.get("id"), "check_type": ctype, "target": target,
                     "executed": executed, "tool": tool_used}}


# ── Python probes ────────────────────────────────────────────────────────────
def _probe_open_redirect(sess, target, param):
    evil = "evil.example"
    out = []
    for payload in (f"https://{evil}/", f"//{evil}/", f"https:/{evil}"):
        url = _set_param(target, param, payload) if param else _set_param(target, "to", payload)
        try:
            r = sess.get(reachable_url(url), timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException:
            continue
        loc = r.headers.get("Location", "")
        if r.status_code in (301, 302, 303, 307, 308) and evil in urlsplit(loc).netloc:
            out.append(finding(
                title="Open redirect", source_tool="active_check", severity_hint=MEDIUM,
                location=url, cwe="CWE-601", owasp="A01:2021 - Broken Access Control",
                evidence=f"{r.status_code} Location: {loc}  (payload param={param or 'to'})",
                description="The endpoint redirects to an attacker-controlled external URL."))
            break
    return out


def _probe_access(sess, target, ctype):
    try:
        r = sess.get(reachable_url(target), timeout=TIMEOUT)
    except requests.RequestException:
        return []
    body = r.text[:4000]
    out = []
    sensitive_path = any(k in target.lower() for k in
                         ("admin", "config", "internal", "secret", "/users", "/accounts", "debug"))
    if r.status_code == 200 and body.strip() and (ctype == "auth" or sensitive_path):
        if sensitive_path:
            out.append(finding(
                title="Sensitive endpoint reachable without authentication",
                source_tool="active_check", severity_hint=HIGH, location=target,
                cwe="CWE-284", owasp="A01:2021 - Broken Access Control",
                evidence=f"HTTP {r.status_code}, {len(r.content)} bytes; body starts: {body[:200]!r}",
                description="An administrative/sensitive endpoint returns data to an unauthenticated client."))
    leaks = re.findall(r"(?i)(stack trace|at [\w.$]+\([\w.]+:\d+\)|Exception in|"
                       r"/(?:home|var|usr)/[\w/.-]+|[A-Za-z0-9_]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", body)
    if leaks and ctype == "info_disclosure":
        out.append(finding(
            title="Information disclosure in response body", source_tool="active_check",
            severity_hint=LOW, location=target, cwe="CWE-200",
            owasp="A05:2021 - Security Misconfiguration",
            evidence="; ".join(sorted(set(leaks))[:5]),
            description="The response leaks paths, stack traces, or contact data."))
    return out


def _probe_path_traversal(sess, target, param):
    out = []
    for payload in ("../../../../../../etc/passwd", "....//....//....//etc/passwd",
                    "%2e%2e%2f%2e%2e%2fetc%2fpasswd"):
        url = _set_param(target, param, payload) if param else target.rstrip("/") + "/" + payload
        try:
            r = sess.get(reachable_url(url), timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if PASSWD.search(r.text):
            out.append(finding(
                title="Path traversal / local file disclosure", source_tool="active_check",
                severity_hint=CRITICAL, location=url, cwe="CWE-22",
                owasp="A01:2021 - Broken Access Control",
                evidence=f"Response contained /etc/passwd signature for payload {payload!r}",
                description="The parameter is used to read arbitrary files from the server."))
            break
    return out


def _probe_sqli(sess, target, param, intrusive_ok):
    out, tool = [], "http_probe"
    for payload in ("'", "\"", "1' OR '1'='1", "1) OR (1=1"):
        url = _set_param(target, param, payload) if param else _set_param(target, "q", payload)
        try:
            r = sess.get(reachable_url(url), timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if SQL_ERRORS.search(r.text):
            out.append(finding(
                title="Possible SQL injection (database error surfaced)",
                source_tool="active_check", severity_hint=HIGH, location=url, cwe="CWE-89",
                owasp="A03:2021 - Injection",
                evidence=f"DB error signature returned for payload {payload!r}",
                description="Injecting SQL meta-characters elicits a database error, "
                            "indicating unsanitized input reaches a SQL query."))
            break
    if intrusive_ok and shutil.which("sqlmap") and param:
        tool = "sqlmap"
        out += _run_sqlmap(target, param)
    return out, tool


def _probe_xss(sess, target, param, intrusive_ok):
    out, tool = [], "http_probe"
    marker = "scx9t1z"
    payload = f"{marker}<svg/onload=alert(1)>"
    url = _set_param(target, param, payload) if param else _set_param(target, "q", payload)
    try:
        r = sess.get(reachable_url(url), timeout=TIMEOUT)
        if f"{marker}<svg/onload=" in r.text:
            out.append(finding(
                title="Reflected XSS (payload reflected unencoded)", source_tool="active_check",
                severity_hint=HIGH, location=url, cwe="CWE-79", owasp="A03:2021 - Injection",
                evidence=f"Payload {payload!r} reflected without HTML-encoding",
                description="User input is reflected into the response without encoding, "
                            "allowing script execution."))
    except requests.RequestException:
        pass
    if not out and shutil.which("dalfox") and param:
        tool = "dalfox"
        out += _run_dalfox(target, param)
    return out, tool


def _probe_cors(sess, target):
    out = []
    evil = "https://evil.example"
    try:
        r = sess.get(reachable_url(target), timeout=TIMEOUT, headers={"Origin": evil})
    except requests.RequestException:
        return out
    acao = r.headers.get("Access-Control-Allow-Origin", "")
    acac = r.headers.get("Access-Control-Allow-Credentials", "")
    if acao == evil or (acao == "*" and acac.lower() == "true"):
        out.append(finding(
            title="Permissive CORS policy", source_tool="active_check", severity_hint=MEDIUM,
            location=target, cwe="CWE-942", owasp="A05:2021 - Security Misconfiguration",
            evidence=f"Access-Control-Allow-Origin: {acao}; Allow-Credentials: {acac}",
            description="The server reflects arbitrary Origins (with credentials), exposing "
                        "authenticated data to cross-origin attackers."))
    return out


# ── External tools (used when present) ───────────────────────────────────────
def _run(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("tool run failed %s: %s", cmd[:2], exc)
        return None


def _run_nuclei(target, auth=None):
    if not shutil.which("nuclei"):
        return ([finding(title="nuclei not installed (template scan skipped)",
                         source_tool="nuclei", severity_hint=INFO, location=target,
                         evidence="Run the dast-worker Docker image for full tooling.",
                         description="")], "none", False)
    cmd = ["nuclei", "-u", reachable_url(target), "-silent", "-jsonl",
           "-severity", "low,medium,high,critical", "-timeout", "5",
           "-rate-limit", "50", "-no-color"]
    for k, v in auth_headers(auth).items():
        cmd += ["-H", f"{k}: {v}"]
    proc = _run(cmd, timeout=240)
    out = []
    if proc and proc.stdout:
        for line in proc.stdout.splitlines():
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = j.get("info", {})
            out.append(finding(
                title=f"nuclei: {info.get('name', j.get('template-id', 'finding'))}",
                source_tool="nuclei", severity_hint=info.get("severity", "info").title(),
                location=j.get("matched-at", target), cwe="",
                owasp="A06:2021 - Vulnerable and Outdated Components",
                evidence=f"template={j.get('template-id')} matched={j.get('matched-at')}",
                description=info.get("description", ""), raw=j))
    return out, "nuclei", True


def _run_sqlmap(target, param):
    proc = _run(["sqlmap", "-u", reachable_url(_set_param(target, param, "1")),
                 "-p", param, "--batch", "--level", "1", "--risk", "1",
                 "--smart", "--flush-session", "--answers=quit=N"], timeout=300)
    if proc and proc.stdout and re.search(r"is vulnerable|sqlmap identified", proc.stdout, re.I):
        return [finding(
            title="SQL injection confirmed by sqlmap", source_tool="sqlmap",
            severity_hint=CRITICAL, location=target, cwe="CWE-89", owasp="A03:2021 - Injection",
            evidence=proc.stdout[-600:], description=f"sqlmap confirmed injection in parameter {param!r}.")]
    return []


def _run_dalfox(target, param):
    proc = _run(["dalfox", "url", _set_param(target, param, "FUZZ"), "--silence",
                 "--no-color", "--format", "plain"], timeout=180)
    if proc and proc.stdout and re.search(r"\[POC\]|\[VULN\]", proc.stdout):
        return [finding(
            title="XSS confirmed by dalfox", source_tool="dalfox", severity_hint=HIGH,
            location=target, cwe="CWE-79", owasp="A03:2021 - Injection",
            evidence=proc.stdout[:600], description=f"dalfox confirmed XSS in parameter {param!r}.")]
    return []
