"""Attacker personas as first-class objects (spec 8)."""
from common import personas


def test_anon_always_anonymous_attacker():
    out = personas.build({"anon": {}})
    assert len(out) == 1
    assert out[0]["persona"] == "anonymous internet attacker"
    assert out[0]["start_position"] == "unauthenticated"


def test_admin_maps_to_compromised_privileged():
    out = personas.build({"anon": {}, "admin": {"header": "Authorization", "value": "Bearer x"}})
    labels = {p["label"]: p for p in out}
    assert labels["admin"]["persona"] == "compromised privileged user"


def test_ordinary_user_default_template():
    out = personas.build({"anon": {}, "userA": {"value": "Bearer a"}})
    ua = next(p for p in out if p["label"] == "userA")
    assert "ordinary" in ua["persona"]


def test_cross_tenant_objectives_need_two_identities():
    one = personas.build({"anon": {}, "userA": {"value": "a"}})
    ua = next(p for p in one if p["label"] == "userA")
    assert any("2nd distinct-tenant" in c for c in ua["constraints"])

    two = personas.build({"anon": {}, "userA": {"value": "a"}, "userB": {"value": "b"}})
    ub = next(p for p in two if p["label"] == "userB")
    assert not any("2nd distinct-tenant" in c for c in ub["constraints"])


def test_manifest_allowed_personas_filters():
    out = personas.build({"anon": {}, "userA": {"value": "a"}},
                         manifest={"allowed_personas": ["anon"]})
    assert {p["label"] for p in out} == {"anon"}
