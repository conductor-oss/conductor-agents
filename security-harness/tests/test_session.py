"""SSO session capture distillation (common/session.py): proves the pure credential picker turns a
captured browser session into the right {header,scheme,token}, with sniffed-bearer > localStorage-
JWT > cookie precedence, and host-scoped cookie assembly."""
from common import session

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1MSJ9.c2ln"   # 3-segment JWT shape
STORAGE = {
    "cookies": [
        {"name": "SESSION", "value": "abc", "domain": "app.example.com"},
        {"name": "csrf", "value": "z", "domain": "app.example.com"},
        {"name": "other", "value": "nope", "domain": "tracker.net"},
    ],
    "origins": [{"origin": "https://app.example.com",
                 "localStorage": [{"name": "access_token", "value": JWT}, {"name": "ui", "value": "dark"}]}],
}


def test_looks_like_jwt():
    assert session.looks_like_jwt(JWT) and not session.looks_like_jwt("not-a-jwt") and not session.looks_like_jwt("")


def test_cookie_header_is_host_scoped():
    ch = session.cookie_header(STORAGE, "app.example.com")
    assert "SESSION=abc" in ch and "csrf=z" in ch and "other=nope" not in ch   # tracker.net excluded


def test_cookie_header_matches_subdomain():
    st = {"cookies": [{"name": "s", "value": "1", "domain": ".example.com"}]}
    assert session.cookie_header(st, "app.example.com") == "s=1"


def test_token_from_local_storage():
    assert session.token_from_local_storage(STORAGE, "app.example.com") == JWT
    assert session.token_from_local_storage({"origins": []}) is None


def test_pick_prefers_sniffed_bearer():
    cred = session.pick_credential([{"header": "Authorization", "value": f"Bearer {JWT}"}], STORAGE, "app.example.com")
    assert cred == {"kind": "bearer-sniffed", "header": "Authorization", "scheme": "Bearer", "token": JWT}


def test_pick_sniffed_raw_token_keeps_scheme_empty():
    cred = session.pick_credential([{"header": "X-Authorization", "value": JWT}], None, "app.example.com")
    assert cred["kind"] == "bearer-sniffed" and cred["scheme"] == "" and cred["token"] == JWT


def test_pick_falls_back_to_localstorage_then_cookie():
    # no sniffed auth header -> localStorage JWT
    cred = session.pick_credential([{"header": "Accept", "value": "*/*"}], STORAGE, "app.example.com")
    assert cred["kind"] == "bearer-storage" and cred["token"] == JWT and cred["scheme"] == "Bearer"
    # no token anywhere -> cookie
    cookie_only = {"cookies": [{"name": "SESSION", "value": "abc", "domain": "app.example.com"}]}
    cred2 = session.pick_credential([], cookie_only, "app.example.com")
    assert cred2 == {"kind": "cookie", "header": "Cookie", "scheme": "", "token": "SESSION=abc"}


def test_pick_none_when_empty():
    assert session.pick_credential([], {}, "app.example.com") == {"kind": "none"}


def test_build_session_doc_shape():
    cred = {"kind": "bearer-sniffed", "header": "X-Authorization", "scheme": "", "token": JWT}
    doc = session.build_session_doc(cred, label="orgA", target="https://app.example.com",
                                    captured_at="2026-06-24T00:00:00Z", storage_state=STORAGE)
    assert doc["auth_token"] == JWT and doc["auth_header"] == "X-Authorization" and doc["auth_scheme"] == ""
    assert doc["label"] == "orgA" and doc["credential_kind"] == "bearer-sniffed"
    assert doc["storage_state"]["cookies"]                      # full session retained for the browser hand


# ── API-seen auth header preferred over an /auth (id_token) one ──────────────────
def test_pick_credential_prefers_api_seen_token():
    sniffed = [
        {"header": "x-authorization", "value": "ID_TOKEN", "path": "/auth/callback", "api": False},
        {"header": "x-authorization", "value": "API_TOKEN", "path": "/api/users/me", "api": True},
    ]
    cred = session.pick_credential(sniffed, {}, "app.example.com")
    assert cred["kind"] == "bearer-sniffed"
    assert cred["token"] == "API_TOKEN"        # the /api one, not the id_token from /auth
    assert cred["header"] == "x-authorization"


def test_pick_credential_sniffed_still_beats_localstorage():
    sniffed = [{"header": "x-authorization", "value": "RAW", "path": "/api/x", "api": True}]
    cred = session.pick_credential(sniffed, STORAGE, "app.example.com")
    assert cred["kind"] == "bearer-sniffed" and cred["token"] == "RAW"


def test_pick_credential_falls_back_when_no_api_flag():
    # backward-compat: entries without an 'api' key still work (treated as non-API)
    cred = session.pick_credential([{"header": "authorization", "value": "Bearer Z"}], {}, "h")
    assert cred["kind"] == "bearer-sniffed" and cred["token"] == "Z" and cred["scheme"] == "Bearer"
