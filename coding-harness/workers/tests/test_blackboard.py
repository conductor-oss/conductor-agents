"""The swarm blackboard: append-only facts, artifacts, lock-free budget, message bus, and the
configuration seam."""
import os

from common import blackboard as bb
from common.blackboard import _LocalBackend


def _be(tmp_path):
    return _LocalBackend(str(tmp_path))


def test_facts_are_append_only_and_readback(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("camp1", "wf-a", "progress", {"item": "x", "state": "in-progress"}, backend=be)
    bb.post_fact("camp1", "wf-b", "lesson", {"note": "schema is shared"}, backend=be)
    facts = bb.read_facts("camp1", backend=be)
    assert len(facts) == 2
    assert {f["author"] for f in facts} == {"wf-a", "wf-b"}


def test_concurrent_writers_never_collide(tmp_path):
    be = _be(tmp_path)
    for i in range(50):
        bb.post_fact("camp2", "wf-a", "progress", {"n": i}, backend=be)
    assert len(bb.read_facts("camp2", kind="progress", backend=be)) == 50


def test_read_facts_filters_by_kind(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "w", "ownership", {"file": "a.py"}, backend=be)
    bb.post_fact("c", "w", "lesson", {"note": "x"}, backend=be)
    assert len(bb.read_facts("c", kind="ownership", backend=be)) == 1
    assert len(bb.read_facts("c", kind="lesson", backend=be)) == 1


def test_key_layout_is_per_author_unique(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "wf-a", "progress", {"n": 1}, backend=be)
    bb.post_fact("c", "wf-a", "progress", {"n": 2}, backend=be)
    keys = be.list("facts/c/")
    assert len(keys) == 2 and len(set(keys)) == 2
    for k in keys:
        assert k.startswith("facts/c/progress/wf-a__") and k.endswith(".json")


def test_fact_with_non_dict_data_is_wrapped(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "w", "note", "a bare string", backend=be)
    f = bb.read_facts("c", backend=be)[0]
    assert f["data"] == {"value": "a bare string"}


def test_artifact_put_get_roundtrip_returns_reference(tmp_path):
    be = _be(tmp_path)
    ref = bb.put_artifact("c", "wf-a", b"big blob", ext="txt", backend=be)
    assert ref["blackboard_ref"] and ref["size"] == len(b"big blob")
    assert bb.get_artifact(ref, backend=be) == b"big blob"


def test_get_artifact_bad_ref_is_none(tmp_path):
    be = _be(tmp_path)
    assert bb.get_artifact({}, backend=be) is None
    assert bb.get_artifact({"blackboard_ref": "nope/missing"}, backend=be) is None


def test_budget_is_lock_free_and_sums_across_authors(tmp_path):
    be = _be(tmp_path)
    bb.bump_budget("c", "wf-a", requests=3, bytes=100, backend=be)
    bb.bump_budget("c", "wf-b", requests=2, bytes=50, backend=be)
    total = bb.bump_budget("c", "wf-a", requests=1, bytes=10, backend=be)
    assert total == {"requests": 6, "bytes": 160}
    assert bb.read_budget("c", backend=be) == {"requests": 6, "bytes": 160}


def test_direct_message_reaches_addressed_recipient(tmp_path):
    be = _be(tmp_path)
    mid = bb.send_message("c", "wf-a", to="wf-b", body="rebased onto head", sender_role="impl",
                          intent="share", backend=be)
    box_b = bb.read_inbox("c", "wf-b", "impl", backend=be)
    box_c = bb.read_inbox("c", "wf-c", "impl", backend=be)
    assert box_b["available"] and "rebased onto head" in box_b["digest"]
    assert box_c["available"] is False
    assert box_b["inbox"][0]["from"] == "wf-a" and box_b["inbox"][0]["thread"] == mid


def test_role_addressed_and_broadcast_messages(tmp_path):
    be = _be(tmp_path)
    bb.send_message("c", "wf-a", to="role:reviewer", body="who reviews #12?", intent="ask", backend=be)
    bb.send_message("c", "wf-a", to="all", body="contract frozen", intent="alert", backend=be)
    rev = bb.read_inbox("c", "wf-z", "reviewer", backend=be)
    impl = bb.read_inbox("c", "wf-y", "impl", backend=be)
    assert "who reviews #12" in rev["digest"] and "contract frozen" in rev["digest"]
    assert "contract frozen" in impl["digest"] and "who reviews #12" not in impl["digest"]


def test_sender_does_not_see_own_message(tmp_path):
    be = _be(tmp_path)
    bb.send_message("c", "wf-a", to="all", body="hi", backend=be)
    assert bb.read_inbox("c", "wf-a", "impl", backend=be)["available"] is False


def test_reply_threads_a_conversation(tmp_path):
    be = _be(tmp_path)
    mid = bb.send_message("c", "wf-a", to="wf-b", body="own schema.py?", intent="ask", backend=be)
    bb.send_message("c", "wf-b", to="wf-a", body="yes until #3 lands", intent="answer",
                    thread=mid, reply_to=mid, backend=be)
    ans = bb.read_inbox("c", "wf-a", "impl", backend=be)["inbox"][0]
    assert ans["intent"] == "answer" and ans["thread"] == mid and ans["reply_to"] == mid


def test_configure_changes_env_var_names(tmp_path, monkeypatch):
    # With a custom dir env name, backend_from_env must read THAT name.
    bb.configure(dir_env="XX_BB_DIR")
    try:
        target = str(tmp_path / "custom")
        monkeypatch.setenv("XX_BB_DIR", target)
        monkeypatch.delenv("SWARM_BLACKBOARD_S3_BUCKET", raising=False)
        be = bb.backend_from_env()
        assert isinstance(be, _LocalBackend) and be.root == target
    finally:
        bb.configure(dir_env="SWARM_BLACKBOARD_DIR")  # restore default for other tests


def test_configure_default_dir_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_BLACKBOARD_S3_BUCKET", raising=False)
    monkeypatch.delenv("SWARM_BLACKBOARD_DIR", raising=False)
    bb.configure(default_dir=lambda: str(tmp_path / "provided"))
    try:
        be = bb.backend_from_env()
        assert isinstance(be, _LocalBackend) and be.root == str(tmp_path / "provided")
    finally:
        bb.configure(default_dir=None)  # restore


# ─────────────────── WP4 (H6): incremental fold cache + Postgres backend ───────────────────
def test_cache_parses_each_fact_once_and_isolates_callers(tmp_path):
    be = _be(tmp_path)
    bb.clear_cache()
    for i in range(20):
        bb.post_fact("c-cache", "a", "item", {"n": i}, backend=be)
    before = bb.STATS["parsed"]
    first = bb.read_facts("c-cache", kind="item", backend=be)
    assert bb.STATS["parsed"] - before == 20
    first[0]["data"]["n"] = "mutated"  # a caller mutating its copy must not corrupt the cache
    bb.post_fact("c-cache", "a", "item", {"n": 99}, backend=be)
    again = bb.read_facts("c-cache", kind="item", backend=be)
    assert bb.STATS["parsed"] - before == 21  # only the one new fact was parsed
    assert len(again) == 21 and again[0]["data"]["n"] == 0


def test_since_returns_only_newer_facts(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c-since", "a", "item", {"n": 1}, backend=be)
    ts = bb.read_facts("c-since", backend=be)[-1]["ts"]
    bb.post_fact("c-since", "a", "item", {"n": 2}, backend=be)
    newer = bb.read_facts("c-since", kind="item", since=ts, backend=be)
    assert [f["data"]["n"] for f in newer] == [2]


def test_large_board_folds_with_one_parse_per_fact(tmp_path):
    be = _be(tmp_path)
    bb.clear_cache()
    for i in range(1000):
        bb.post_fact("c-big", f"w{i % 50}", "item", {"id": f"i{i}"}, backend=be)
    before = bb.STATS["parsed"]
    for _ in range(10):  # ten "waves" of folding
        assert len(bb.read_facts("c-big", kind="item", backend=be)) == 1000
    assert bb.STATS["parsed"] - before == 1000


_PG_DSN = os.environ.get("SWARM_TEST_PG_DSN")


def test_postgres_backend_contract():
    import pytest
    if not _PG_DSN:
        pytest.skip("SWARM_TEST_PG_DSN not set; Postgres backend contract not exercised")
    import uuid as _uuid
    be = bb._PostgresBackend(_PG_DSN)
    cid = f"pg-{_uuid.uuid4().hex[:8]}"
    keys = [bb.post_fact(cid, "a_b", "item", {"n": i}, backend=be) for i in range(5)]
    bb.post_fact(cid, "x", "lesson", {"t": "l"}, backend=be)
    assert sorted(be.list(f"facts/{cid}/item/")) == sorted(keys)
    facts = bb.read_facts(cid, kind="item", backend=be)
    assert [f["data"]["n"] for f in facts] == [0, 1, 2, 3, 4]
    assert len(bb.read_facts(cid, backend=be)) == 6
    # LIKE metacharacters in a prefix are literal ('_' must not match any char).
    bb.post_fact(cid.replace("-", "_"), "a", "item", {"n": 9}, backend=be)
    assert len(bb.read_facts(cid, kind="item", backend=be)) == 5
    bb.bump_budget(cid, "w", requests=2, backend=be)
    assert bb.bump_budget(cid, "w", requests=3, backend=be)["requests"] == 5  # upsert per author
