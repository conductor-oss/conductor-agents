"""Static analysis workers — run when the operator points the scan at source.

sast_semgrep  – semgrep (p/owasp-top-ten + p/security-audit) -> code findings.
sast_secrets  – gitleaks -> hardcoded-secret findings (when the binary present).
sast_deps     – trivy fs -> dependency CVE findings (when the binary present).
route_extract – framework-agnostic regex sweep that pulls HTTP routes out of the
                source so they can SEED the live scan (the "use the source to
                find the target" feature).

All read a local source path; semgrep is pip-installed so this runs on the host.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

from conductor.client.worker.worker_task import worker_task

from common.findings import HIGH, INFO, LOW, MEDIUM, finding

log = logging.getLogger(__name__)

SKIP_DIRS = {".git", "node_modules", "dist", "build", "vendor", "venv", ".venv",
             "__pycache__", "target", ".next", "out", "coverage"}
MAX_FILE_BYTES = 400_000
MAX_FILES = 4000

# Route patterns across common frameworks: (regex, method_group, path_group)
ROUTE_PATTERNS = [
    # Express / Node:  app.get('/x'  router.post("/y"
    (re.compile(r"""\b(?:app|router|api)\.(get|post|put|delete|patch|options|head|all)\s*\(\s*['"`]([^'"`]+)""", re.I), 1, 2),
    # Flask / FastAPI:  @app.route('/x'  @router.get("/y")
    (re.compile(r"""@\w+\.(get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)""", re.I), 1, 2),
    (re.compile(r"""@\w+\.route\s*\(\s*['"]([^'"]+)"""), None, 1),
    # Spring:  @GetMapping("/x")  @RequestMapping(value="/y")
    (re.compile(r"""@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?['"]([^'"]+)"""), 1, 2),
    # Django:  path('x/', ...)  re_path(r'^y$', ...)
    (re.compile(r"""\b(?:path|re_path|url)\s*\(\s*r?['"]([^'"]+)"""), None, 1),
    # Rails routes.rb:  get 'x', post "y"
    (re.compile(r"""^\s*(get|post|put|patch|delete)\s+['"]([^'"]+)""", re.M), 1, 2),
]


def _src(inp):
    p = str(inp.get("source_path") or "").strip()
    return p if p and os.path.isdir(p) else ""


def _iter_files(root):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield fp
            n += 1
            if n >= MAX_FILES:
                return


def _tool(name):
    """Resolve a CLI tool, checking the venv's bin dir (where pip-installed tools
    like semgrep live) as well as PATH — the worker process's PATH may not
    include the venv bin."""
    cand = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(cand):
        return cand
    return shutil.which(name)


def _run(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("sast tool failed %s: %s", cmd[:2], exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
@worker_task(task_definition_name="sast_semgrep", thread_count=2)
def sast_semgrep(task):
    inp = task.input_data or {}
    src = _src(inp)
    if not src:
        return {"findings": [], "meta": {"skipped": "no source_path"}}
    semgrep = _tool("semgrep")
    if not semgrep:
        return {"findings": [finding(title="semgrep not installed (SAST skipped)",
                                     source_tool="semgrep", severity_hint=INFO, location=src,
                                     description="Install semgrep or run the sast-worker image.")],
                "meta": {"skipped": "no semgrep"}}
    proc = _run([semgrep, "--config", "p/owasp-top-ten", "--config", "p/security-audit",
                 "--json", "--quiet", "--timeout", "120", "--max-target-bytes", "1000000", src], timeout=600)
    findings = []
    if proc and proc.stdout:
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        sev_map = {"ERROR": HIGH, "WARNING": MEDIUM, "INFO": LOW}
        for r in data.get("results", []):
            extra = r.get("extra", {})
            meta = extra.get("metadata", {})
            cwe = meta.get("cwe")
            cwe = (cwe[0] if isinstance(cwe, list) and cwe else cwe) or ""
            owasp = meta.get("owasp")
            owasp = (owasp[0] if isinstance(owasp, list) and owasp else owasp) or ""
            rel = os.path.relpath(r.get("path", ""), src)
            findings.append(finding(
                title=f"semgrep: {r.get('check_id', '').split('.')[-1]}",
                source_tool="semgrep", severity_hint=sev_map.get(extra.get("severity"), LOW),
                location=f"{rel}:{r.get('start', {}).get('line', '?')}",
                cwe=str(cwe), owasp=str(owasp),
                evidence=(extra.get("lines") or "")[:300],
                description=extra.get("message", "")[:500],
                raw={"check_id": r.get("check_id")}))
    return {"findings": findings, "meta": {"count": len(findings), "source": src}}


# ─────────────────────────────────────────────────────────────────────────────
@worker_task(task_definition_name="sast_secrets", thread_count=2)
def sast_secrets(task):
    inp = task.input_data or {}
    src = _src(inp)
    gitleaks = _tool("gitleaks")
    if not src or not gitleaks:
        return {"findings": [], "meta": {"skipped": "no source or gitleaks"}}
    # gitleaks writes the JSON report to --report-path (a real file), not stdout.
    rpt = os.path.join(tempfile.gettempdir(), f"gitleaks-{os.getpid()}.json")
    _run([gitleaks, "detect", "--source", src, "--no-git", "--report-format", "json",
          "--report-path", rpt, "--exit-code", "0"], timeout=300)
    findings = []
    try:
        with open(rpt) as fh:
            data = json.load(fh)
        for s in data or []:
            findings.append(finding(
                title=f"Hardcoded secret: {s.get('RuleID', 'secret')}",
                source_tool="gitleaks", severity_hint=HIGH,
                location=f"{os.path.relpath(s.get('File', ''), src)}:{s.get('StartLine', '?')}",
                cwe="CWE-798", owasp="A07:2021 - Identification and Authentication Failures",
                evidence=(s.get("Match") or "")[:120],
                description=s.get("Description", "Hardcoded credential detected.")))
    except (OSError, json.JSONDecodeError):
        pass
    finally:
        try:
            os.remove(rpt)
        except OSError:
            pass
    return {"findings": findings, "meta": {"count": len(findings)}}


# ─────────────────────────────────────────────────────────────────────────────
@worker_task(task_definition_name="sast_deps", thread_count=2)
def sast_deps(task):
    inp = task.input_data or {}
    src = _src(inp)
    trivy = _tool("trivy")
    if not src or not trivy:
        return {"findings": [], "meta": {"skipped": "no source or trivy"}}
    proc = _run([trivy, "fs", "--quiet", "--format", "json", "--scanners", "vuln", src], timeout=420)
    findings = []
    if proc and proc.stdout:
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        sev_map = {"CRITICAL": HIGH, "HIGH": HIGH, "MEDIUM": MEDIUM, "LOW": LOW, "UNKNOWN": INFO}
        for res in data.get("Results", []):
            for v in (res.get("Vulnerabilities") or [])[:50]:
                findings.append(finding(
                    title=f"Vulnerable dependency: {v.get('PkgName')} {v.get('InstalledVersion')} ({v.get('VulnerabilityID')})",
                    source_tool="trivy", severity_hint=sev_map.get(v.get("Severity"), LOW),
                    location=res.get("Target", ""), cwe=",".join(v.get("CweIDs", []) or []),
                    owasp="A06:2021 - Vulnerable and Outdated Components",
                    evidence=f"{v.get('VulnerabilityID')} fixed in {v.get('FixedVersion', 'n/a')}",
                    description=(v.get("Title") or v.get("Description") or "")[:400]))
    return {"findings": findings, "meta": {"count": len(findings)}}


# ─────────────────────────────────────────────────────────────────────────────
@worker_task(task_definition_name="route_extract", thread_count=2)
def route_extract(task):
    """Regex-sweep the source for HTTP routes; returns endpoint paths to seed the scan."""
    inp = task.input_data or {}
    src = _src(inp)
    if not src:
        return {"routes": [], "meta": {"skipped": "no source_path"}}
    routes, seen = [], set()
    for fp in _iter_files(src):
        try:
            with open(fp, "r", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for rx, m_grp, p_grp in ROUTE_PATTERNS:
            for m in rx.finditer(text):
                path = m.group(p_grp)
                method = (m.group(m_grp).upper() if m_grp else "GET")
                if not path or not path.startswith("/") or len(path) > 200:
                    continue
                key = (method, path)
                if key in seen:
                    continue
                seen.add(key)
                routes.append({"path": path, "method": method,
                               "file": os.path.relpath(fp, src)})
    return {"routes": routes[:300], "meta": {"count": len(routes), "source": src}}
