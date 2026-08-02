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
