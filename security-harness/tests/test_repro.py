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


def test_endpoint_key_normalizes_ids_but_preserves_real_segments():
    assert repro.endpoint_key("GET", "/api/metadata/webhook/{id}") == repro.endpoint_key("get", "/api/metadata/webhook/7f2a9c1b4e8d4a6f9b2c")
    assert repro.endpoint_key("GET", "/api/audit/5") == repro.endpoint_key("GET", "/api/audit/{id}")
    # distinct real segments are NOT merged
    assert repro.endpoint_key("POST", "/api/secrets-v2") != repro.endpoint_key("POST", "/api/secrets")
    # full URL host is stripped
    assert repro.endpoint_key("GET", "https://t.example/api/x/9") == repro.endpoint_key("GET", "/api/x/{id}")


def test_exact_request_renders_runnable_curl():
    f = {"title": "SSRF via POST /api/metadata/workflow", "signature": "ssrf|post/api/metadata/workflow|viren,virenx",
         "evidence": "verified SSRF differential",
         "reproduction_request": {"method": "POST", "path": "/api/metadata/workflow", "identity": "viren",
                                  "body": {"tasks": [{"type": "HTTP", "inputParameters": {"uri": "http://169.254.169.254/"}}]}}}
    r = repro.build(f)
    assert r["exact"] is True
    assert "169.254.169.254" in r["curl"] and "$JWT_viren" in r["curl"] and "-X POST" in r["curl"]
    md = repro.markdown(f)
    assert "Exact confirming request" in md
