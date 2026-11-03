"""Victim-marker resolver: cross-identity/cross-tenant oracles need a distinctive datum from the
VICTIM identity. Decoding it from the JWT claims up front saves the agent from burning its whole
budget rediscovering 'which resource does identity B own' (observed live, run 17cb0c9d)."""
import base64
import json

from common import identity as idm


def _jwt(payload: dict) -> str:
    def seg(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{seg({'alg':'HS256','typ':'JWT'})}.{seg(payload)}.sig"


def test_jwt_claims_decodes_top_level_and_strips_scheme():
    tok = _jwt({"sub": "u-9", "email": "a@x.io"})
    assert idm.jwt_claims(tok)["sub"] == "u-9"
    assert idm.jwt_claims("Bearer " + tok)["email"] == "a@x.io"   # scheme stripped
    assert idm.jwt_claims("X-Authorization " + tok)["sub"] == "u-9"


def test_jwt_claims_never_raises_on_garbage():
    for bad in (None, "", "not.a.jwt.at.all", "onlyonesegment", "a.b", 12345, "....."):
        assert idm.jwt_claims(bad) == {} or isinstance(idm.jwt_claims(bad), dict)


def test_identity_markers_harvests_nested_data_block():
    # Juice Shop nests the user under `data`.
    idents = {
        "anon": {},
        "alice": {"value": "Bearer " + _jwt({"data": {"id": 156, "email": "alice@x.io"}})},
        "bob": {"value": _jwt({"data": {"id": 157, "email": "bob@x.io"}})},
    }
    m = idm.identity_markers(idents)
    assert m["alice"]["id"] == "156" and m["alice"]["email"] == "alice@x.io"
    assert m["bob"]["id"] == "157"
    assert "anon" not in m


def test_identity_markers_skips_non_jwt_and_empty():
    idents = {"anon": {}, "keyonly": {"value": "opaque-api-key-no-dots"}}
    assert idm.identity_markers(idents) == {}


def test_victim_marker_directive_excludes_attacker_and_names_victim():
    idents = {
        "alice": {"value": _jwt({"data": {"id": 156, "email": "alice@x.io"}})},
        "bob": {"value": _jwt({"data": {"id": 157, "email": "bob@x.io"}})},
    }
    d = idm.victim_marker_directive(idents, exclude="alice")
    assert "bob" in d and "157" in d
    assert "alice" not in d and "156" not in d   # attacker's own markers not offered as victim markers
    assert "victim_marker" in d


def test_victim_marker_directive_empty_when_single_identity():
    idents = {"alice": {"value": _jwt({"data": {"id": 1}})}}
    assert idm.victim_marker_directive(idents, exclude="alice") == ""


def test_tenant_tag_carried_as_marker():
    idents = {"t1": {"value": _jwt({"sub": "u1"}), "tenant": "acme"},
              "t2": {"value": _jwt({"sub": "u2"}), "tenant": "globex"}}
    m = idm.identity_markers(idents)
    assert m["t1"]["tenant"] == "acme"
