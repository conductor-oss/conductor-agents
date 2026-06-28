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

