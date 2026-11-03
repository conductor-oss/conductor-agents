"""Reproduction-step synthesis for confirmed findings."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))
from common import repro  # noqa: E402


def test_classifies_from_title_not_oracle_note():
    # The finding's evidence records the oracle that fired (often SSRF-differential); the title is
    # authoritative. SSTI must not be mislabelled SSRF.
    f = {"title": "SSTI / secret leak via prompt template testing (POST /api/prompts/test)",
         "evidence": "verified SSRF differential (direct_reach_reflection)", "signature": "x|viren,virenx"}
    assert repro.oracle_class(f) == "injection"


def test_class_coverage():
    cases = {
        "BPM importer XXE: POST /api/metadata/workflow-importer/import-bpm": "xxe",
        "SSRF via gateway/registry service config pointed at internal address": "ssrf",
        "Privilege escalation via self-granted authorization (POST /api/auth/authorization)": "privesc",
        "Cross-tenant env disclosure via GET /api/environment": "bola",
        "LIST /api/integrations/provider returns cross-tenant plaintext api_keys": "secret",
    }
    for title, want in cases.items():
        assert repro.oracle_class({"title": title}) == want, (title, repro.oracle_class({"title": title}))


def test_build_has_steps_curl_and_endpoint():
    f = {"title": "Webhook GET /api/metadata/webhook/{id} authorization IDOR",
         "signature": "idor|get/api/metadata/webhook/{id}|viren,virenx",
         "evidence": "verified cross-identity access"}
    r = repro.build(f)
    assert r["endpoint"].endswith("/api/metadata/webhook/{id}")
    assert r["identities"] == ["viren", "virenx"]
    assert len(r["steps"]) >= 3 and r["curl"].startswith("curl")
    assert r["verify"] and r["fix"]


def test_markdown_sections_present():
    md = repro.markdown({"title": "SSRF via HTTP task", "signature": "ssrf|post/api/workflow|a,b",
                         "evidence": "verified SSRF differential"})
    for sect in ("Steps to reproduce", "Validate:", "Fix:", "Verify the fix:"):
        assert sect in md, sect


def test_no_endpoint_degrades_gracefully():
    r = repro.build({"title": "some finding with no path", "evidence": ""})
    assert r["steps"] and r["curl"] == ""  # no curl when no endpoint, but still steps
