"""Optional target profiles + generic declarative cleanup-sweep."""
import json
import os

from common import auth, profiles
from httptool import tasks as httptool


def test_load_missing_is_empty():
    assert profiles.load(None) == {}
    assert profiles.load("does-not-exist") == {}


def test_conductor_profile_loads():
    p = profiles.load("conductor")
    assert p.get("name") == "conductor"
    assert p["auth"]["header"] == "X-Authorization"
    assert any("metadata" in x for x in p["auth"]["probe_paths"])
    assert any(f["type"] == "secret" for f in p["cleanup_families"])
    assert any(url.startswith("https://") for url in p["documentation"])
    playbook = p["feature_exploitation_playbook"]
    assert {"INFRA-SSRF", "INFRA-RCE-INJECTION", "CONF-CROSS-TENANT-READ"} <= set(playbook["must_exercise"])


def test_conductor_knowledge_not_in_engine():
    # The engine's generic probe set must not bake in Conductor endpoints; those live
    # ONLY in the profile.
    assert all("metadata" not in x for x in auth._GENERIC_PROBE_PATHS)


def test_token_exchange_shape_is_data_driven():
    # acquire_token honors a declarative exchange body/token_field (here: no network,
    # just confirm a direct token still works and the default path is intact).
    assert auth.acquire_token({"auth_token": "T"}) == "T"


def test_sweep_skipped_without_families():
    class T:
        input_data = {"base_url": "https://x", "scope": {"in_scope_hosts": ["x"]}}
    out = httptool.sweep_resources(T())
    assert out["available"] is False
    assert "skipped" in out["summary"]


def test_fam_name_and_delete_template():
    # string-valued list (secrets): name_key null -> item is the name
    fam_secret = {"type": "secret", "name_key": None, "delete": "/api/secrets/{name}"}
    assert httptool._fam_name("sc-pentest-abc", fam_secret) == "sc-pentest-abc"
    assert httptool._fam_delete_path("sc-pentest-abc", "sc-pentest-abc", fam_secret) == "/api/secrets/sc-pentest-abc"

    # dict list with templated version default
    fam_wf = {"type": "workflow_def", "name_key": "name",
              "delete": "/api/metadata/workflow/{name}/{version}", "defaults": {"version": 1}}
    o = {"name": "sc-pentest-wf"}
    assert httptool._fam_name(o, fam_wf) == "sc-pentest-wf"
    assert httptool._fam_delete_path(o, "sc-pentest-wf", fam_wf) == "/api/metadata/workflow/sc-pentest-wf/1"

    # id_key indirection (applications)
    fam_app = {"type": "application", "name_key": "name", "id_key": "id",
               "delete": "/api/applications/{id}"}
    oa = {"name": "sc-pentest-app", "id": "abc-123"}
    assert httptool._fam_delete_path(oa, "sc-pentest-app", fam_app) == "/api/applications/abc-123"


def test_cleanup_resolves_relative_ledger_paths(monkeypatch):
    # Regression: relative ledger paths must be resolved against base_url before the
    # scope check, or they are wrongly skipped as "out of scope" (the bug the Orkes run
    # surfaced). We stub the HTTP call and assert the path is treated as in-scope.
    calls = {}

    class _Resp:
        status_code = 200

    def _fake_request(method, url, **kw):
        calls["url"] = url
        return _Resp()

    monkeypatch.setattr(httptool.requests, "request", _fake_request)

    class T:
        input_data = {
            "ledger": [{"method": "DELETE", "url": "/api/metadata/workflow/sc-pentest-x/1"}],
            "scope": {"in_scope_hosts": ["app.example.com"]},
            "base_url": "https://app.example.com",
        }
    out = httptool.cleanup_resources(T())
    assert len(out["deleted"]) == 1            # resolved + deleted, NOT skipped
    assert out["residue"] == []
    assert "app.example.com" in calls["url"]


def test_repo_conductor_profile_is_valid_json():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "profiles", "conductor.json")) as fh:
        json.load(fh)  # raises if invalid
