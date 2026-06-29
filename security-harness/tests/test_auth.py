"""Auth resolution for authenticated scans."""
from common import auth


def test_direct_bearer_token():
    a = auth.resolve_auth({"auth_token": "abc123"})
    assert a == {"header": "Authorization", "value": "Bearer abc123"}
    assert auth.auth_headers(a) == {"Authorization": "Bearer abc123"}


def test_custom_header_and_scheme():
    a = auth.resolve_auth({"auth_token": "k", "auth_header": "X-API-Key", "auth_scheme": ""})
    assert a == {"header": "X-API-Key", "value": "k"}
    assert auth.auth_headers(a) == {"X-API-Key": "k"}


def test_no_auth_configured():
    assert auth.resolve_auth({}) == {}
    assert auth.resolve_auth(None) == {}
    assert auth.auth_headers({}) == {}
    assert auth.auth_headers(None) == {}


def test_key_secret_without_token_url_is_noop():
    # No token_url -> cannot exchange -> empty (no network call attempted)
    assert auth.resolve_auth({"auth_key": "k", "auth_secret": "s"}) == {}


def test_acquire_token_direct():
    assert auth.acquire_token({"auth_token": "tok"}) == "tok"
    assert auth.acquire_token({}) is None


def test_present_orkes_vs_bearer():
    # X-Authorization carries the RAW token (no Bearer) -- the Orkes convention that the
    # old default got wrong; Authorization defaults to Bearer.
    assert auth._present("J", "X-Authorization", "") == {"header": "X-Authorization", "value": "J"}
    assert auth._present("J", "Authorization", None) == {"header": "Authorization", "value": "Bearer J"}


def test_pick_chooses_authenticating_candidate(monkeypatch):
    # Simulate a target where ONLY X-Authorization authenticates (tri-state _probe).
    xauth = auth._present("J", "X-Authorization", "")
    bearer = auth._present("J", "Authorization", "Bearer")
    monkeypatch.setattr(auth, "_probe",
                        lambda base, a, scope, paths: True if a["header"] == "X-Authorization" else False)
    chosen, verified, note = auth._pick("https://x", [bearer, xauth], None, [])
    assert chosen["header"] == "X-Authorization" and verified == "true"


def test_pick_protected_rejects_all_is_false(monkeypatch):
    # A protected path was seen but no scheme worked -> definitively unauthenticated.
    monkeypatch.setattr(auth, "_probe", lambda base, a, scope, paths: False)
    cands = [auth._present("J", "X-Authorization", ""), auth._present("J", "Authorization", "Bearer")]
    chosen, verified, note = auth._pick("https://x", cands, None, [])
    assert verified == "false" and "UNAUTHENTICATED" in note


def test_pick_no_protected_path_is_unknown(monkeypatch):
    # No access-controlled endpoint observed -> unknown, NOT a false alarm (generic apps).
    monkeypatch.setattr(auth, "_probe", lambda base, a, scope, paths: None)
    cands = [auth._present("J", "Authorization", "Bearer")]
    chosen, verified, note = auth._pick("https://x", cands, None, [])
    assert verified == "unknown"


def test_resolve_auth_autodetects_with_base_url(monkeypatch):
    monkeypatch.setattr(auth, "_probe",
                        lambda base, a, scope, paths: True if a["header"] == "X-Authorization" else False)
    a = auth.resolve_auth({"auth_token": "J"}, base_url="https://x")
    assert a["header"] == "X-Authorization" and a["value"] == "J" and a["verified"] == "true"


def test_probe_paths_are_generic_not_conductor():
    # The default probe set must NOT bake in Conductor resource endpoints.
    joined = " ".join(auth._GENERIC_PROBE_PATHS)
    for conductor_path in ("/api/metadata", "/api/secrets", "/api/applications", "/api/workflow"):
        assert conductor_path not in joined



# ── Autonomous probe-path derivation + re-verification ──────────────────────────
def _surface(*paths):
    return {"endpoints": [{"url": "https://app.test" + p, "method": "GET"} for p in paths]}


def test_derive_probe_paths_prefers_api_and_drops_static():
    surface = _surface("/api/users/me", "/static/app.js", "/dashboard", "/api/orders", "/logo.png")
    out = auth.derive_probe_paths(surface, {"in_scope_hosts": ["app.test"]})
    assert "/api/users/me" in out and "/api/orders" in out      # API endpoints kept
    assert "/static/app.js" not in out and "/logo.png" not in out  # static assets dropped
    assert out.index("/api/users/me") < out.index("/dashboard")    # API-looking ranked first


def test_derive_probe_paths_respects_scope_and_dedup():
    surface = {"endpoints": [
        {"url": "https://app.test/api/x", "method": "GET"},
        {"url": "https://app.test/api/x?q=1", "method": "GET"},
        {"url": "https://evil.test/api/y", "method": "GET"},
    ]}
    out = auth.derive_probe_paths(surface, {"in_scope_hosts": ["app.test"]})
    assert "/api/y" not in " ".join(out)          # out-of-scope dropped
    assert "/api/x" in out and "/api/x?q=1" in out  # query variant is distinct


def test_aggregate_verified_tristate():
    assert auth.aggregate_verified([]) == ("true", "no credentials supplied (anonymous baseline only)")
    assert auth.aggregate_verified([{"value": "t", "verified": "true"}])[0] == "true"
    assert auth.aggregate_verified([{"value": "t", "verified": "false"}])[0] == "false"
    assert auth.aggregate_verified([{"value": "t", "verified": "unknown"}])[0] == "unknown"
    # any true wins over false
    assert auth.aggregate_verified([{"value": "a", "verified": "false"},
                                    {"value": "b", "verified": "true"}])[0] == "true"


def test_reverify_upgrades_unknown_and_never_downgrades_true(monkeypatch):
    # stub _probe: authenticates only for the 'good' token
    def fake_probe(base_url, authd, scope, probe_paths):
        return True if authd.get("value") == "good" else False
    monkeypatch.setattr(auth, "_probe", fake_probe)
    ids = {
        "anon": {},
        "ok": {"header": "X-Authorization", "value": "good", "verified": "unknown"},
        "bad": {"header": "X-Authorization", "value": "nope", "verified": "unknown"},
        "trusted": {"header": "X-Authorization", "value": "nope", "verified": "true"},  # don't downgrade
    }
    res = auth.reverify(ids, "https://app.test", None, ["/api/x"])
    assert res["identities"]["ok"]["verified"] == "true"
    assert res["identities"]["bad"]["verified"] == "false"
    assert res["identities"]["trusted"]["verified"] == "true"   # prior true preserved
    assert res["identities"]["anon"] == {}
    assert res["auth_verified"] == "true"  # at least one cred authenticated
