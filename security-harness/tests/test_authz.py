"""Authorization manifest + capability levels — the white-hat governance core."""
from datetime import datetime, timedelta, timezone

from common import authz


def _manifest(**over):
    now = datetime.now(timezone.utc)
    base = {
        "approvers": ["security@example.com"],
        "in_scope_hosts": ["app.example.com"],
        "window": {
            "start": (now - timedelta(hours=1)).isoformat(),
            "expiry": (now + timedelta(hours=1)).isoformat(),
        },
        "capability_max": 2,
    }
    base.update(over)
    return base


def test_valid_manifest_authorizes():
    v = authz.validate(_manifest(), "https://app.example.com/login")
    assert v["ok"] is True
    assert v["capability_max"] == 2


def test_no_manifest_fails_closed():
    v = authz.validate(None, "https://app.example.com")
    assert v["ok"] is False
    assert v["capability_max"] == 0


def test_missing_required_field_fails_closed():
    m = _manifest()
    del m["approvers"]
    v = authz.validate(m, "https://app.example.com")
    assert v["ok"] is False
    assert "approvers" in v["reason"]


def test_expired_window_fails_closed():
    now = datetime.now(timezone.utc)
    m = _manifest(window={"start": (now - timedelta(days=2)).isoformat(),
                          "expiry": (now - timedelta(days=1)).isoformat()})
    v = authz.validate(m, "https://app.example.com")
    assert v["ok"] is False
    assert "expired" in v["reason"]


def test_not_yet_started_fails_closed():
    now = datetime.now(timezone.utc)
    m = _manifest(window={"start": (now + timedelta(hours=1)).isoformat(),
                          "expiry": (now + timedelta(hours=2)).isoformat()})
    v = authz.validate(m, "https://app.example.com")
    assert v["ok"] is False


def test_out_of_scope_target_fails_closed():
    v = authz.validate(_manifest(), "https://evil.example.org/x")
    assert v["ok"] is False
    assert "not in the manifest scope" in v["reason"]


def test_subdomain_gated():
    m = _manifest(allow_subdomains=True)
    assert authz.validate(m, "https://api.app.example.com")["ok"] is True
    m2 = _manifest(allow_subdomains=False)
    assert authz.validate(m2, "https://api.app.example.com")["ok"] is False


def test_capability_clamped_0_4():
    assert authz.validate(_manifest(capability_max=9), "https://app.example.com")["capability_max"] == 4
    assert authz.validate(_manifest(capability_max=-3), "https://app.example.com")["capability_max"] == 0


def test_action_capability_by_method():
    assert authz.action_capability("GET") == 1
    assert authz.action_capability("HEAD") == 1
    assert authz.action_capability("POST") == 2
    assert authz.action_capability("DELETE") == 2
    assert authz.action_capability("POST", is_sensitive=True) == 3
    assert authz.action_capability("", is_code_exec=True) == 2
    assert authz.action_capability("", is_code_exec=True, is_sensitive=True) == 3


def test_forbids_glob_and_protected_records():
    m = _manifest(forbidden_operations=["DELETE /api/users/*", "POST /api/billing/*"],
                  protected_records=["userId:1"])
    assert authz.forbids("DELETE", "https://app.example.com/api/users/42", m) is True
    assert authz.forbids("POST", "https://app.example.com/api/billing/charge", m) is True
    assert authz.forbids("GET", "https://app.example.com/api/users/42", m) is False
    assert authz.forbids("GET", "https://app.example.com/api/orders?owner=userId:1", m) is True
    assert authz.forbids("GET", "https://app.example.com/api/orders", m) is False


def test_technique_allowed_default_permissive():
    assert authz.technique_allowed("ssrf", _manifest()) is True  # no allowed_techniques -> all
    m = _manifest(allowed_techniques=["bola", "race"])
    assert authz.technique_allowed("bola", m) is True
    assert authz.technique_allowed("ssrf", m) is False
