"""Persistent cross-run knowledge store (spec 13)."""
import os

import pytest

from common import memory


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    yield


def _f(title, category="bola", sev="High"):
    return {"title": title, "category": category, "severity": sev,
            "evidence": "x", "poc_request": {"method": "GET", "url": "/a"}}


def test_load_absent_returns_skeleton():
    s = memory.load(memory.fingerprint("localhost"))
    assert s["all_confirmed"] == []
    assert s["runs"] == 0


def test_fingerprint_stable_and_host_keyed():
    a = memory.fingerprint("app.example.com")
    b = memory.fingerprint("app.example.com")
    c = memory.fingerprint("other.example.com")
    assert a == b and a != c
    assert "app.example.com" in a  # human-readable


def test_save_then_load_roundtrips():
    fp = memory.fingerprint("localhost")
    state, _ = memory.merge_run(memory.empty_state(), fp=fp, host="localhost",
                                app_version="v1", new_confirmed=[_f("A")],
                                new_rejected=[], new_blind=[], new_tried=["bola|x|userA"],
                                gaps=["g1"], coverage={}, run_id="r1")
    memory.save(fp, state)
    reloaded = memory.load(fp)
    assert len(reloaded["all_confirmed"]) == 1
    assert reloaded["tried_signatures"] == ["bola|x|userA"]
    assert reloaded["runs"] == 1


def test_merge_dedupes_by_signature():
    fp = memory.fingerprint("h")
    s1, _ = memory.merge_run(memory.empty_state(), fp=fp, host="h", app_version="v1",
                             new_confirmed=[_f("Same finding")], new_rejected=[], new_blind=[],
                             new_tried=["sig1"], gaps=[], coverage={}, run_id="r1")
    s2, stats = memory.merge_run(s1, fp=fp, host="h", app_version="v1",
                                 new_confirmed=[_f("Same finding")], new_rejected=[], new_blind=[],
                                 new_tried=["sig1", "sig2"], gaps=[], coverage={}, run_id="r2")
    assert len(s2["all_confirmed"]) == 1                # deduped
    assert s2["tried_signatures"] == ["sig1", "sig2"]   # unioned
    assert stats["reconfirmed"] == 1


def test_release_marks_prior_findings_stale():
    fp = memory.fingerprint("h")
    s1, _ = memory.merge_run(memory.empty_state(), fp=fp, host="h", app_version="v1",
                             new_confirmed=[_f("Old bug")], new_rejected=[], new_blind=[],
                             new_tried=[], gaps=[], coverage={}, run_id="r1")
    # New release (v2), the old bug is NOT re-observed this run.
    s2, stats = memory.merge_run(s1, fp=fp, host="h", app_version="v2",
                                 new_confirmed=[_f("Brand new")], new_rejected=[], new_blind=[],
                                 new_tried=[], gaps=[], coverage={}, run_id="r2")
    by_title = {f["title"]: f for f in s2["all_confirmed"]}
    assert by_title["Old bug"]["lifecycle"] == "stale"
    assert by_title["Brand new"]["lifecycle"] == "confirmed"
    assert stats["stale_revalidated"] == 1
    assert stats["released"] is True


def test_reoccurrence_after_release_reconfirms():
    fp = memory.fingerprint("h")
    s1, _ = memory.merge_run(memory.empty_state(), fp=fp, host="h", app_version="v1",
                             new_confirmed=[_f("Persisting bug")], new_rejected=[], new_blind=[],
                             new_tried=[], gaps=[], coverage={}, run_id="r1")
    s2, stats = memory.merge_run(s1, fp=fp, host="h", app_version="v2",
                                 new_confirmed=[_f("Persisting bug")], new_rejected=[], new_blind=[],
                                 new_tried=[], gaps=[], coverage={}, run_id="r2")
    f = s2["all_confirmed"][0]
    assert f["lifecycle"] == "confirmed"
    assert stats["reconfirmed"] == 1


def test_provenance_fields_stamped():
    fp = memory.fingerprint("h")
    s, _ = memory.merge_run(memory.empty_state(), fp=fp, host="h", app_version="v1",
                            new_confirmed=[_f("X")], new_rejected=[], new_blind=[],
                            new_tried=[], gaps=[], coverage={}, run_id="r1")
    f = s["all_confirmed"][0]
    assert f["provenance"] == "observed"
    assert f["fingerprint"] == fp
    assert f["timestamp"] and f["confidence"]


def test_atomic_write_leaves_no_partial(tmp_path):
    fp = memory.fingerprint("h")
    memory.save(fp, memory.empty_state())
    d = os.path.join(os.environ["STATE_DIR"], memory._safe(fp))
    # no leftover temp file
    assert not any(name.endswith(".tmp") for name in os.listdir(d))
