"""filter_techniques worker: manifest allowed_techniques gates hypotheses at selection."""
import types

from recon import tasks as rt


def _task(**inp):
    return types.SimpleNamespace(input_data=inp)


def _hyps():
    return [
        {"id": "H-SSRF", "category": "ssrf"},
        {"id": "H-BOLA", "category": "bola"},
        {"id": "H-SQLI", "category": "sqli", "mandatory": True},
    ]


def test_no_allowed_techniques_is_permissive():
    out = rt.filter_techniques(_task(hypotheses=_hyps(), manifest={}))
    assert out["count"] == 3
    assert out["blocked_count"] == 0


def test_filters_to_allowed_set():
    out = rt.filter_techniques(_task(
        hypotheses=_hyps(), manifest={"allowed_techniques": ["ssrf", "bola"]}))
    kept = {h["id"] for h in out["hypotheses"]}
    assert kept == {"H-SSRF", "H-BOLA"}
    assert out["blocked_count"] == 1
    assert out["blocked"][0]["id"] == "H-SQLI"


def test_disallowed_technique_blocked_even_when_mandatory():
    # Authorization outranks coverage: a 'mandatory' hypothesis is still dropped.
    out = rt.filter_techniques(_task(
        hypotheses=[{"id": "H-SQLI", "category": "sqli", "mandatory": True}],
        manifest={"allowed_techniques": ["ssrf"]}))
    assert out["count"] == 0
    assert out["blocked_count"] == 1


def test_missing_category_treated_as_other():
    out = rt.filter_techniques(_task(
        hypotheses=[{"id": "H-X"}], manifest={"allowed_techniques": ["ssrf"]}))
    assert out["count"] == 0  # "other" not in allowed -> dropped


def test_never_raises_on_garbage():
    out = rt.filter_techniques(_task(hypotheses="not-a-list", manifest=None))
    assert out["hypotheses"] == []
