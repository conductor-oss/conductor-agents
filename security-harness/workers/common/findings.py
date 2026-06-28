"""Normalized finding schema shared by every scanner worker.

Workers emit *raw* findings in this shape; the ``triage`` LLM task is what
validates them, assigns final severity, and cuts false positives. Keeping a
single shape here means every tool (recon, nuclei, sqlmap, semgrep, ...) feeds
triage the same structure.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Severity hints (the LLM triage step makes the final call).
CRITICAL = "Critical"
HIGH = "High"
MEDIUM = "Medium"
LOW = "Low"
INFO = "Info"


def content_hash(finding_like: dict) -> str:
    """Tamper-evidence hash over a finding's stable, security-relevant fields (spec 16/23).

    Computed over title/location/evidence/poc_request only (not volatile metadata) so it
    is stable across re-serialization but changes if the substance is altered. The
    persistent store recomputes this on load to detect memory-poisoning."""
    payload = {
        "title": str(finding_like.get("title") or ""),
        "location": str(finding_like.get("location") or finding_like.get("target") or ""),
        "evidence": str(finding_like.get("evidence") or ""),
        "poc_request": finding_like.get("poc_request") or {},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def finding(
    *,
    title: str,
    source_tool: str,
    severity_hint: str = INFO,
    location: str = "",
    evidence: str = "",
    description: str = "",
    cwe: str = "",
    owasp: str = "",
    provenance: str = "",
    raw: Any = None,
) -> dict:
    """Build one normalized raw finding. ``provenance`` (§2/§12) defaults to the kind
    implied by ``source_tool`` (observed/documented/source/inferred)."""
    from common import provenance as _prov
    return {
        "title": title,
        "source_tool": source_tool,
        "severity_hint": severity_hint,
        "location": location,
        "evidence": evidence,
        "description": description,
        "cwe": cwe,
        "owasp": owasp,
        "provenance": provenance or _prov.classify(source_tool),
        "raw": raw if raw is not None else {},
    }
