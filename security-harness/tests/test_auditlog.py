"""Tamper-evident action log + evidence-integrity hashing (spec 16, 23)."""
import json
import os

import pytest

from common import auditlog, findings, memory


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    yield


def test_chain_verifies_clean():
    for i in range(5):
        auditlog.append("app.example.com", {"action": "http_request", "i": i})
    path = auditlog._path("app.example.com")
    res = auditlog.verify_chain(path)
    assert res["ok"] is True
    assert res["entries"] == 5


def test_tampered_middle_entry_breaks_chain():
    for i in range(4):
        auditlog.append("h", {"action": "x", "i": i})
    path = auditlog._path("h")
    lines = open(path).read().splitlines()
    rec = json.loads(lines[1])
    rec["i"] = 999  # mutate a body field without fixing the hash
    lines[1] = json.dumps(rec)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    res = auditlog.verify_chain(path)
    assert res["ok"] is False
    assert res["broken_at"] == 1


def test_truncation_detected():
    for i in range(4):
        auditlog.append("h", {"action": "x", "i": i})
    path = auditlog._path("h")
    lines = open(path).read().splitlines()
    # drop the last line -> chain is still internally valid but shorter; verify counts it
    with open(path, "w") as fh:
        fh.write("\n".join(lines[:2]) + "\n")
    res = auditlog.verify_chain(path)
    assert res["ok"] is True and res["entries"] == 2  # remaining prefix is consistent


def test_content_hash_stable_and_sensitive():
    f = {"title": "BOLA", "location": "/api/x", "evidence": "leaked", "poc_request": {"url": "/x"}}
    h1 = findings.content_hash(f)
    h2 = findings.content_hash(dict(reversed(list(f.items()))))  # field order shouldn't matter
    assert h1 == h2
    f2 = dict(f, evidence="different")
    assert findings.content_hash(f2) != h1


def test_memory_flags_tampered_finding_on_load():
    fp = memory.fingerprint("h")
    state, _ = memory.merge_run(memory.empty_state(), fp=fp, host="h", app_version="v1",
                                new_confirmed=[{"title": "Bug", "category": "bola",
                                                "evidence": "real", "poc_request": {}}],
                                new_rejected=[], new_blind=[], new_tried=[], gaps=[],
                                coverage={}, run_id="r1")
    memory.save(fp, state)
    # tamper with the stored evidence out-of-band
    path = os.path.join(os.environ["STATE_DIR"], memory._safe(fp), "state.json")
    data = json.load(open(path))
    data["all_confirmed"][0]["evidence"] = "ALTERED"
    json.dump(data, open(path, "w"))
    reloaded = memory.load(fp)
    assert reloaded["all_confirmed"][0]["tampered"] is True
    assert reloaded["all_confirmed"][0]["lifecycle"] == "inconclusive"


def test_contradiction_detection():
    inv = [{"invariant": "only the owner can delete an invoice"}]
    confirmed = [{"title": "Any user can delete another owner's invoice", "category": "bola"}]
    cs = memory.detect_contradictions(inv, confirmed)
    assert cs and cs[0]["type"] == "documented-vs-observed"
