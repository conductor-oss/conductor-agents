"""The in-sandbox sc helper enforces the manifest (forbidden_operations / protected_records).

``sandbox_sc._forbids`` is a deliberate self-contained port of ``common.authz.forbids``
(the hardened container can't import common); this test pins them to the same behavior so
they cannot silently drift apart.
"""
from codeexec import sandbox_sc
from common import authz


def test_forbids_matches_forbidden_operation(monkeypatch):
    monkeypatch.setattr(sandbox_sc, "MANIFEST",
                        {"forbidden_operations": ["DELETE */api/cluster/*"]})
    assert sandbox_sc._forbids("DELETE", "https://app.test/api/cluster/9") is True
    assert sandbox_sc._forbids("GET", "https://app.test/api/cluster/9") is False


def test_forbids_matches_protected_record(monkeypatch):
    monkeypatch.setattr(sandbox_sc, "MANIFEST", {"protected_records": ["api_key"]})
    assert sandbox_sc._forbids("GET", "https://app.test/secrets/api_key/1") is True
    assert sandbox_sc._forbids("GET", "https://app.test/secrets/other/1") is False


def test_forbids_empty_manifest_allows(monkeypatch):
    monkeypatch.setattr(sandbox_sc, "MANIFEST", {})
    assert sandbox_sc._forbids("DELETE", "https://app.test/anything") is False


def test_oob_destination_is_not_a_sandbox_scope_target(monkeypatch):
    monkeypatch.setattr(sandbox_sc, "OOB_BASE", "https://oob.example.test")
    monkeypatch.setattr(sandbox_sc, "SCOPE", {"in_scope_hosts": ["app.test"]})
    assert sandbox_sc.in_scope("https://app.test/api/workflows") is True
    assert sandbox_sc.in_scope("https://oob.example.test/c/canary") is False


def test_sandbox_forbids_agrees_with_canonical_authz(monkeypatch):
    manifest = {"forbidden_operations": ["DELETE */api/cluster/*"],
                "protected_records": ["token_"]}
    monkeypatch.setattr(sandbox_sc, "MANIFEST", manifest)
    for method, url in [
        ("DELETE", "https://app.test/api/cluster/1"),
        ("GET", "https://app.test/api/cluster/1"),
        ("GET", "https://app.test/v1/token_abc"),
        ("POST", "https://app.test/orders"),
    ]:
        assert sandbox_sc._forbids(method, url) == authz.forbids(method, url, manifest)


def test_inband_probe_returns_actionable_ack_not_none():
    # Regression (run a903e328): inband_probe returned None, so agents thought it failed and thrashed
    # ("probe returns None regardless of marker"). It must return an ack naming the planted oracle and
    # the fields it received, so a non-None ack reads as "queued" and a missing field is visible.
    sandbox_sc._state["inband"] = []
    ack = sandbox_sc.inband_probe("cross_identity_access",
                                  victim={"path": "/rest/basket/5"}, attacker={"path": "/rest/basket/5"},
                                  victim_marker="bob@x.io", control={"path": "/rest/basket/6"})
    assert ack is not None
    assert ack["planted"] == "cross_identity_access"
    assert ack["status"] == "recorded"
    assert set(ack["fields"]) == {"victim", "attacker", "victim_marker", "control"}
    # and it actually queued the plan for the verifier
    assert sandbox_sc._state["inband"][-1]["oracle"] == "cross_identity_access"


def test_inband_probe_ack_surfaces_missing_field():
    sandbox_sc._state["inband"] = []
    ack = sandbox_sc.inband_probe("cross_identity_access", victim={"path": "/a"})  # missing fields
    assert "attacker" not in ack["fields"] and "victim" in ack["fields"]
