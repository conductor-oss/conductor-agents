"""Multi-agent voting on the unconfirmable tail (common/voting.py). Proves: dynamically-confirmed
findings bypass voting; the unconfirmable tail survives only on a strict majority of refute-by-
default skeptic votes; ties and no-votes are dropped (conservative)."""
from common import voting

CONFIRMED_OOB = {"id": "F-1", "title": "SSRF", "oob_confirmed": True}
CONFIRMED_TXT = {"id": "F-2", "title": "BOLA", "validation": "confirmed via cross-identity re-run"}
BLIND = {"id": "F-3", "title": "blind SSTI", "validation": "not confirmed (no OOB)"}
SAST = {"id": "F-4", "title": "AES-ECB", "provenance": "source"}


def test_partition_separates_confirmed_from_unconfirmable():
    p = voting.partition([CONFIRMED_OOB, CONFIRMED_TXT, BLIND, SAST])
    assert [f["id"] for f in p["confirmed"]] == ["F-1", "F-2"]
    assert [f["id"] for f in p["unconfirmable"]] == ["F-3", "F-4"]


def test_majority_strict_and_refute_by_default():
    assert voting.majority([{"real": True}, {"real": True}, {"real": False}])["survives"] is True   # 2/3
    assert voting.majority([{"real": True}, {"real": False}])["survives"] is False                  # tie -> no
    assert voting.majority([{"refuted": False}, {"refuted": False}, {"refuted": True}])["survives"] is True
    assert voting.majority([])["survives"] is False                                                 # no votes -> no
    # ambiguous verdicts count as "not real" (refute-by-default)
    assert voting.majority([{"maybe": 1}, {"real": True}])["survives"] is False


def test_label_attaches_verdict_and_summary():
    voted = voting.label(BLIND, [{"real": True}, {"real": True}, {"real": False}])
    assert voted["verdict"] == "voted" and voted["vote_summary"] == {"survives": True, "real": 2, "total": 3}
    out = voting.label(BLIND, [{"real": False}, {"real": False}])
    assert out["verdict"] == "voted_out"


def test_apply_confirmed_bypasses_voting():
    res = voting.apply([CONFIRMED_OOB, BLIND], {"F-3": [{"real": True}, {"real": True}]})
    by = {f["id"]: f for f in res}
    assert by["F-1"]["verdict"] == "confirmed"          # never voted, even with no votes supplied
    assert by["F-3"]["verdict"] == "voted"


def test_apply_missing_votes_is_voted_out():
    res = voting.apply([SAST], {})                       # no votes for F-4
    assert res[0]["verdict"] == "voted_out"


def test_survivors_keeps_confirmed_and_voted_drops_noise():
    findings = [CONFIRMED_OOB, BLIND, SAST]
    votes = {"F-3": [{"real": True}, {"real": True}], "F-4": [{"real": False}, {"real": True}]}  # F-4 tie
    keep = {f["id"] for f in voting.survivors(findings, votes)}
    assert keep == {"F-1", "F-3"}                        # confirmed + voted; F-4 (tie) dropped
