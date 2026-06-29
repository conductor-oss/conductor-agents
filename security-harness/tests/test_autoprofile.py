"""Autonomous target profiling: reverify_identities + derive_profile workers, and
normalize_target auto-loading a generated profile. No network — auth._probe is stubbed."""
import json
import os
import types

import pytest

from common import auth as auth_mod
from common import memory
from recon import tasks as rt


def _task(**inp):
    return types.SimpleNamespace(input_data=inp)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))


def _surface(*paths):
    return {"endpoints": [{"url": "https://app.test" + p, "method": "GET"} for p in paths]}


# ── reverify_identities ─────────────────────────────────────────────────────────
def test_reverify_identities_confirms_against_discovered_surface(monkeypatch):
    # token authenticates only on the discovered /api path, which static probing never tried
    def fake_probe(base_url, authd, scope, probe_paths):
        return True if "/api/data" in probe_paths else False
    monkeypatch.setattr(auth_mod, "_probe", fake_probe)

    out = rt.reverify_identities(_task(
        identities={"anon": {}, "u": {"header": "X-Authorization", "value": "t", "verified": "false"}},
        surface=_surface("/api/data", "/assets/x.js"),
        base_url="https://app.test",
        scope={"in_scope_hosts": ["app.test"]},
        auth_probe_paths=[],
    ))
    assert out["auth_verified"] == "true"
    assert out["identities"]["u"]["verified"] == "true"
    assert "/api/data" in out["derived_probe_paths"]


def test_reverify_identities_never_raises_without_base_url():
    out = rt.reverify_identities(_task(identities={"u": {"value": "t"}}, surface={}, base_url=""))
    assert out["identities"] == {"u": {"value": "t"}}  # echoes input, no crash


# ── derive_profile + auto-load round-trip ───────────────────────────────────────
def test_derive_profile_writes_editable_profile(tmp_path):
    out = rt.derive_profile(_task(
        host="app.test",
        app_model={"archetype": "academy-portal", "purpose": "learning", "tech": ["Webflow"]},
        identities={"anon": {}, "u": {"header": "X-Authorization", "value": "t", "verified": "true"}},
        derived_probe_paths=["/api/users/me", "/api/orders"],
        run_id="r1",
    ))
    path = out["written"]
    assert path and os.path.exists(path)
    prof = json.loads(open(path).read())
    assert prof["name"] == "app.test" and prof["generated"] is True
    assert prof["archetype"] == "academy-portal"
    assert prof["auth"]["header"] == "X-Authorization"
    assert prof["auth"]["probe_paths"] == ["/api/users/me", "/api/orders"]


def test_derive_profile_no_host_is_noop():
    assert rt.derive_profile(_task(host="")).get("written") == ""


def test_normalize_target_autoloads_generated_profile(monkeypatch):
    # write a generated profile, then prove normalize_target picks it up and probes its paths
    prof = {"name": "app.test", "generated": True,
            "auth": {"header": "X-Authorization", "scheme": "", "probe_paths": ["/api/secret"]}}
    d = os.path.join(memory.state_dir(), "profiles")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "app.test.json"), "w").write(json.dumps(prof))

    seen_paths = {}

    def fake_probe(base_url, authd, scope, probe_paths):
        seen_paths["paths"] = list(probe_paths)
        return True
    monkeypatch.setattr(auth_mod, "_probe", fake_probe)

    out = rt.normalize_target(_task(
        target="https://app.test", authorized=True,
        identities=[{"label": "u", "auth_token": "t", "auth_header": "X-Authorization", "auth_scheme": ""}],
    ))
    # the generated profile's probe path was merged into the pre-surface probe
    assert "/api/secret" in seen_paths.get("paths", [])
    assert out["target_profile"].get("generated") is True
