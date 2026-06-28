"""SARIF 2.1.0 emission for security-conductor findings (interop borrowed from Visa VVAH).

Turns the triaged finding set into a standard SARIF log so confirmed findings flow into the
tool ecosystem (GitHub code scanning, DefectDojo, SIEM) instead of living only in our Markdown/
PDF/dossier. Unlike a SAST tool, our findings are DYNAMIC (endpoints/flows, not file:line), so the
target/route lives in ``logicalLocations`` and the base target is the ``artifactLocation`` URI.

Pure (no I/O, no deps beyond stdlib + ``findings.content_hash``); the ``persist`` worker writes the
returned dict to ``reports/<id>/report.sarif``. Unit-tested against the SARIF 2.1.0 schema.
"""

from __future__ import annotations

import re

from . import findings as findings_mod

_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
_INFO_URI = "https://github.com/conductoross/security-conductor"

# SARIF level is one of none|note|warning|error. GitHub code scanning also reads a numeric
# `security-severity` (0.0-10.0, CVSS-like) from rule/result properties to rank alerts.
_LEVEL = {"Critical": "error", "High": "error", "Medium": "warning", "Low": "note", "Info": "note"}
_SEC_SEV = {"Critical": "9.5", "High": "8.0", "Medium": "5.5", "Low": "3.0", "Info": "0.0"}


def _norm_sev(sev: str) -> str:
    s = str(sev or "").strip().capitalize()
    return s if s in _LEVEL else "Info"


def _rule_id(f: dict) -> str:
    """Stable ruleId: prefer the CWE, then OWASP, else a category/objective slug. SARIF ties each
    result to a rule definition by this id."""
    cwe = str(f.get("cwe") or "").strip().upper()
    if re.fullmatch(r"CWE-\d+", cwe):
        return cwe
    owasp = str(f.get("owasp") or "").strip()
    if owasp:
        return "OWASP-" + re.sub(r"[^A-Za-z0-9]+", "-", owasp).strip("-")[:40]
    slug = str(f.get("objective_id") or f.get("category") or "GENERIC").strip()
    return "SC-" + re.sub(r"[^A-Za-z0-9]+", "-", slug).strip("-").upper()[:40] or "SC-GENERIC"


def _cwe_help_uri(cwe: str) -> str | None:
    m = re.fullmatch(r"CWE-(\d+)", str(cwe or "").strip().upper())
    return f"https://cwe.mitre.org/data/definitions/{m.group(1)}.html" if m else None


def _is_confirmed(f: dict) -> bool:
    """A finding is 'confirmed' if the verifier validated it (OOB hit / re-run PoC), not just
    triaged. We read the finding's own ``validation``/``status`` text conservatively."""
    blob = (str(f.get("validation") or "") + " " + str(f.get("status") or "")).lower()
    return ("confirmed" in blob or "verified" in blob) and "not confirmed" not in blob


def to_sarif(triage: dict | None, *, target: str = "", scan_id: str = "",
             tool_version: str = "0.2") -> dict:
    """Build a SARIF 2.1.0 log from a triage result ({findings:[...]}). Drops false positives.
    Each finding -> one result; distinct ruleIds -> the run's rule catalog."""
    flist = [f for f in ((triage or {}).get("findings") or []) if not f.get("false_positive")]
    base_uri = target or "urn:sc:target"

    rules: dict[str, dict] = {}
    results: list[dict] = []
    for f in flist:
        sev = _norm_sev(f.get("severity") or f.get("severity_hint"))
        rid = _rule_id(f)
        if rid not in rules:
            help_uri = _cwe_help_uri(f.get("cwe"))
            rule = {
                "id": rid,
                "name": re.sub(r"[^A-Za-z0-9]+", "", (f.get("cwe") or rid).title()) or rid,
                "shortDescription": {"text": (f.get("title") or rid)[:200]},
                "properties": {"security-severity": _SEC_SEV[sev],
                               "tags": [t for t in ["security", f.get("owasp"), f.get("cwe")] if t]},
            }
            if help_uri:
                rule["helpUri"] = help_uri
            rules[rid] = rule

        msg = (f.get("title") or "").strip()
        if f.get("description"):
            msg = f"{msg} — {str(f['description'])[:600]}"
        location = str(f.get("location") or f.get("target") or "").strip()
        loc = {"physicalLocation": {"artifactLocation": {"uri": base_uri}}}
        if location:
            loc["logicalLocations"] = [{"fullyQualifiedName": location[:300], "kind": "member"}]

        result = {
            "ruleId": rid,
            "level": _LEVEL[sev],
            "message": {"text": msg[:1200] or rid},
            "locations": [loc],
            "partialFingerprints": {"scContentHash/v1": findings_mod.content_hash(f)},
            "properties": {
                "severity": sev,
                "security-severity": _SEC_SEV[sev],
                "confirmed": _is_confirmed(f),
                "confidence": f.get("confidence") or "",
                "cwe": f.get("cwe") or "",
                "owasp": f.get("owasp") or "",
                "evidence": str(f.get("evidence") or "")[:1500],
                "remediation": str(f.get("remediation") or "")[:1000],
            },
        }
        results.append(result)

    return {
        "$schema": _SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "security-conductor",
                "informationUri": _INFO_URI,
                "version": str(tool_version),
                "rules": list(rules.values()),
            }},
            "results": results,
            "properties": {
                "target": target,
                "scan_id": scan_id,
                "confirmed_count": sum(1 for r in results if r["properties"]["confirmed"]),
                "result_count": len(results),
            },
        }],
    }
