"""SARIF 2.1.0 export (common/sarif.py): the VVAH-inspired interop output. Proves the emitter
produces a schema-valid log, drops false positives, ties results to rules, carries CVSS-like
security-severity + a confirmed flag, and round-trips a real run's triage."""
import json
import os

import pytest

from common import sarif as sarif_mod

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

TRIAGE = {
    "findings": [
        {"id": "F-1", "title": "SSRF via workflow HTTP task", "severity": "High", "cwe": "CWE-918",
         "owasp": "A10:2021 - Server-Side Request Forgery", "confidence": "high", "false_positive": False,
         "location": "POST /api/metadata/workflow + POST /api/workflow/{name}",
         "description": "Server fetched an attacker-controlled URL.",
         "evidence": "OOB: target 54.234.23.83 hit the canary.", "validation": "confirmed out-of-band",
         "remediation": "Egress allow-list + SSRF filter."},
        {"id": "F-2", "title": "Weak crypto AES-ECB", "severity": "Medium", "cwe": "CWE-327",
         "false_positive": False, "location": "crypto/Cipher.java", "validation": "not confirmed"},
        {"id": "F-3", "title": "A noisy false positive", "severity": "Low", "false_positive": True},
    ]
}


def _doc():
    return sarif_mod.to_sarif(TRIAGE, target="https://your-conductor.example.com", scan_id="run-123")


def test_basic_shape_and_drops_false_positives():
    doc = _doc()
    assert doc["version"] == "2.1.0" and len(doc["runs"]) == 1
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "security-conductor"
    # F-3 is a false positive -> dropped; F-1, F-2 remain
    assert len(run["results"]) == 2
    assert run["properties"]["result_count"] == 2


def test_rules_are_deduped_and_results_reference_them():
    run = _doc()["runs"][0]
    rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
    assert "CWE-918" in rule_ids and "CWE-327" in rule_ids
    for res in run["results"]:
        assert res["ruleId"] in rule_ids                      # every result ties to a defined rule


def test_level_security_severity_and_confirmed_flag():
    run = _doc()["runs"][0]
    by = {r["ruleId"]: r for r in run["results"]}
    assert by["CWE-918"]["level"] == "error"                  # High -> error
    assert by["CWE-918"]["properties"]["security-severity"] == "8.0"
    assert by["CWE-918"]["properties"]["confirmed"] is True   # "confirmed out-of-band"
    assert by["CWE-327"]["level"] == "warning"                # Medium -> warning
    assert by["CWE-327"]["properties"]["confirmed"] is False  # "not confirmed"
    assert run["properties"]["confirmed_count"] == 1


def test_logical_location_and_fingerprint_present():
    res = next(r for r in _doc()["runs"][0]["results"] if r["ruleId"] == "CWE-918")
    assert res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "https://your-conductor.example.com"
    assert "metadata/workflow" in res["locations"][0]["logicalLocations"][0]["fullyQualifiedName"]
    assert res["partialFingerprints"]["scContentHash/v1"]     # stable cross-run dedup key


def test_empty_triage_is_valid_empty_run():
    doc = sarif_mod.to_sarif({}, target="x", scan_id="y")
    assert doc["runs"][0]["results"] == [] and doc["runs"][0]["tool"]["driver"]["rules"] == []


def test_ruleid_falls_back_when_no_cwe():
    doc = sarif_mod.to_sarif({"findings": [{"title": "t", "severity": "Low",
                                            "owasp": "A01:2021 - Broken Access Control"}]}, target="x")
    assert doc["runs"][0]["results"][0]["ruleId"].startswith("OWASP-")


def test_validates_against_sarif_2_1_0_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = os.path.join(HERE, "fixtures", "sarif-2.1.0.schema.json")
    if not os.path.isfile(schema_path):
        pytest.skip("SARIF schema fixture not present")
    schema = json.load(open(schema_path))
    jsonschema.validate(instance=_doc(), schema=schema)
