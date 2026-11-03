"""Swarm blackboard: append-only facts, artifacts, lock-free budget, and the read model."""
from common import blackboard as bb
from common.blackboard import _LocalBackend


def _be(tmp_path):
    return _LocalBackend(str(tmp_path))


def test_facts_are_append_only_and_readback(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("camp1", "wf-a", "hypothesis", {"signature": "sqli|/x|anon"}, backend=be)
    bb.post_fact("camp1", "wf-b", "observation", {"note": "saw internal marker"}, backend=be)
    facts = bb.read_facts("camp1", backend=be)
    assert len(facts) == 2
    assert {f["author"] for f in facts} == {"wf-a", "wf-b"}


def test_concurrent_writers_never_collide(tmp_path):
    be = _be(tmp_path)
    # Same author posting many facts -> each is a distinct key, none lost.
    for i in range(50):
        bb.post_fact("camp2", "wf-a", "attempt", {"signature": f"s{i}"}, backend=be)
    assert len(bb.read_facts("camp2", kind="attempt", backend=be)) == 50


def test_read_facts_filters_by_kind(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "w", "finding", {"confirmed": True}, backend=be)
    bb.post_fact("c", "w", "claim", {"area": "ssrf"}, backend=be)
    assert len(bb.read_facts("c", kind="finding", backend=be)) == 1
    assert len(bb.read_facts("c", kind="claim", backend=be)) == 1


def test_artifact_put_get_roundtrip_returns_reference(tmp_path):
    be = _be(tmp_path)
    ref = bb.put_artifact("c", "wf-a", b"big evidence blob", ext="txt", backend=be)
    assert ref["blackboard_ref"] and ref["size"] == len(b"big evidence blob")
    assert bb.get_artifact(ref, backend=be) == b"big evidence blob"


def test_budget_is_lock_free_and_sums_across_authors(tmp_path):
    be = _be(tmp_path)
    bb.bump_budget("c", "wf-a", requests=3, bytes=100, backend=be)
    bb.bump_budget("c", "wf-b", requests=2, bytes=50, backend=be)
    total = bb.bump_budget("c", "wf-a", requests=1, bytes=10, backend=be)
    assert total == {"requests": 6, "bytes": 160}
    assert bb.read_budget("c", backend=be) == {"requests": 6, "bytes": 160}


def test_assemble_state_builds_read_model(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "wf-a", "verified_primitive", {"kind": "internal_reach"}, backend=be)
    bb.post_fact("c", "wf-a", "finding", {"confirmed": True, "severity": "High",
                                          "signature": "ssrf|/x"}, backend=be)
    bb.post_fact("c", "wf-b", "hypothesis", {"signature": "sqli|/y|anon", "title": "SQLi"}, backend=be)
    bb.post_fact("c", "wf-b", "coverage", {"area": "INFRA-SSRF", "status": "tested"}, backend=be)
    st = bb.assemble_state("c", backend=be)
    assert len(st["verified_primitives"]) == 1
    assert len(st["findings"]) == 1
    # A never-attempted hypothesis is open work.
    assert [h["signature"] for h in st["open_hypotheses"]] == ["sqli|/y|anon"]
    assert "INFRA-SSRF" in st["coverage"]


def test_hypothesis_retries_across_rounds_then_retires(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "w", "hypothesis", {"signature": "sqli|/y|anon", "title": "SQLi"}, backend=be)
    assert bb.assemble_state("c", backend=be)["open_hypotheses"][0]["attempt_count"] == 0
    # One failed attempt -> STILL open as a retry (anti-zero-shot), carrying no-confirm status.
    bb.post_fact("c", "w", "attempt", {"signature": "sqli|/y|anon"}, backend=be)
    st = bb.assemble_state("c", backend=be)
    assert len(st["open_hypotheses"]) == 1 and st["open_hypotheses"][0]["attempt_count"] == 1
    # Exhaust the retry budget (RETRY_MAX dispatches) -> retired.
    for _ in range(bb.RETRY_MAX):
        bb.post_fact("c", "w", "attempt", {"signature": "sqli|/y|anon"}, backend=be)
    assert bb.assemble_state("c", backend=be)["open_hypotheses"] == []


def test_confirmed_hypothesis_is_not_retried(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "w", "hypothesis", {"signature": "sqli|/y|anon", "title": "SQLi"}, backend=be)
    bb.post_fact("c", "w", "attempt", {"signature": "sqli|/y|anon"}, backend=be)
    # A confirmed finding for that signature stops further retries.
    bb.post_fact("c", "w", "finding", {"confirmed": True, "title": "SQLi", "signature": "sqli|/y|anon"}, backend=be)
    assert bb.assemble_state("c", backend=be)["open_hypotheses"] == []


def test_failed_attempt_lessons_carry_to_retry(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "w", "hypothesis", {"signature": "sqli|/y|anon", "title": "SQLi"}, backend=be)
    bb.post_fact("c", "w", "attempt", {"signature": "sqli|/y|anon"}, backend=be)
    bb.post_fact("c", "w", "finding", {"confirmed": False, "signature": "sqli|/y|anon",
                                       "lesson": "WAF blocked single quotes; try encoding"}, backend=be)
    oh = bb.assemble_state("c", backend=be)["open_hypotheses"][0]
    assert "WAF blocked" in oh["lessons"][0]


def test_confirmed_finding_is_a_review_candidate_until_reviewed(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "w", "finding", {"confirmed": True, "title": "SSRF"}, backend=be)
    assert [f["title"] for f in bb.assemble_state("c", backend=be)["review_candidates"]] == ["SSRF"]
    bb.post_fact("c", "r", "critique", {"survived": True, "candidate_title": "SSRF"}, backend=be)
    assert bb.assemble_state("c", backend=be)["review_candidates"] == []  # reviewed -> off the queue


def test_campaign_rollup_counts_confirmed_and_spend(tmp_path):
    be = _be(tmp_path)
    bb.post_fact("c", "wf-a", "finding", {"confirmed": True, "severity": "Critical"}, backend=be)
    bb.post_fact("c", "wf-b", "finding", {"confirmed": False, "severity": "Low"}, backend=be)
    bb.bump_budget("c", "wf-a", requests=10, bytes=2000, backend=be)
    roll = bb.campaign_rollup("c", backend=be)
    assert roll["confirmed_count"] == 1
    assert roll["confirmed_by_severity"]["Critical"] == 1
    assert roll["spend"]["requests"] == 10


def test_replay_ref_extracts_reproduction_handle():
    ref = bb.replay_ref({"author": "wf-42", "artifact_refs": [{"blackboard_ref": "k"}],
                         "signature": "ssrf|/x"})
    assert ref["workflow_id"] == "wf-42" and ref["signature"] == "ssrf|/x"
    assert ref["artifact_refs"] == [{"blackboard_ref": "k"}]


def test_refuter_excludes_unconfirmed_but_not_oracle_confirmed(tmp_path):
    # An UNCONFIRMED candidate the refuter breaks is excluded; an ORACLE-CONFIRMED finding (a
    # deterministic verifier receipt fired) SURVIVES an LLM refuter's dissent (the receipt outranks an
    # opinion), with the dissent recorded for human review. (Regression: the orkescloud SSRF was
    # oracle-confirmed yet the LLM refuter wrongly dismissed it as "the provider's own auth error".)
    be = _be(tmp_path)
    bb.post_fact("c", "wf-a", "finding", {"confirmed": True, "severity": "High", "title": "SSRF X"}, backend=be)   # oracle-confirmed
    bb.post_fact("c", "wf-b", "finding", {"confirmed": False, "severity": "High", "title": "Weak Z"}, backend=be)  # unconfirmed candidate
    bb.post_fact("c", "wf-r", "critique", {"survived": False, "candidate_title": "SSRF X", "reason": "open redirect, not SSRF"}, backend=be)
    bb.post_fact("c", "wf-r", "critique", {"survived": False, "candidate_title": "Weak Z", "reason": "benign"}, backend=be)
    st = bb.assemble_state("c", backend=be)
    byt = {f["title"]: f for f in st["findings"]}
    assert byt["SSRF X"].get("refuted") is not True and byt["SSRF X"].get("refuter_dissent") is True  # survives + flagged
    assert byt["Weak Z"].get("refuted") is True                                                      # unconfirmed -> refuted
    roll = bb.campaign_rollup("c", backend=be)
    assert roll["confirmed_count"] == 1  # the oracle-confirmed SSRF survives


def test_direct_message_reaches_addressed_recipient(tmp_path):
    be = _be(tmp_path)
    mid = bb.send_message("c", "wf-a", to="wf-b", body="what identity did you use on /x?",
                          sender_role="scout", intent="ask", backend=be)
    # wf-b has it in its inbox; wf-c (not addressed) does not.
    box_b = bb.read_inbox("c", "wf-b", "exploiter", backend=be)
    box_c = bb.read_inbox("c", "wf-c", "chainer", backend=be)
    assert box_b["available"] and "what identity" in box_b["digest"]
    assert box_c["available"] is False
    assert box_b["inbox"][0]["from"] == "wf-a" and box_b["inbox"][0]["thread"] == mid


def test_role_addressed_and_broadcast_messages(tmp_path):
    be = _be(tmp_path)
    bb.send_message("c", "wf-a", to="role:exploiter", body="who can test /admin?", intent="ask", backend=be)
    bb.send_message("c", "wf-a", to="all", body="found creds in JS bundle", intent="alert", backend=be)
    # any exploiter sees the role message + the broadcast; a scout sees only the broadcast.
    exp = bb.read_inbox("c", "wf-z", "exploiter", backend=be)
    scout = bb.read_inbox("c", "wf-y", "scout", backend=be)
    assert "who can test /admin" in exp["digest"] and "found creds" in exp["digest"]
    assert "found creds" in scout["digest"] and "who can test /admin" not in scout["digest"]


def test_reply_threads_a_conversation(tmp_path):
    be = _be(tmp_path)
    mid = bb.send_message("c", "wf-a", to="wf-b", body="is /x injectable?", intent="ask", backend=be)
    bb.send_message("c", "wf-b", to="wf-a", body="yes, blind SQLi via id param", intent="answer",
                    thread=mid, reply_to=mid, backend=be)
    box_a = bb.read_inbox("c", "wf-a", "scout", backend=be)
    ans = box_a["inbox"][0]
    assert ans["intent"] == "answer" and ans["thread"] == mid and ans["reply_to"] == mid
    assert "blind SQLi" in ans["body"]


def test_sender_does_not_see_own_message(tmp_path):
    be = _be(tmp_path)
    bb.send_message("c", "wf-a", to="all", body="hi", backend=be)
    assert bb.read_inbox("c", "wf-a", "scout", backend=be)["available"] is False


def test_attempt_lesson_folds_into_lessons_without_bumping_retry_counter(tmp_path):
    # Gap 3: per-turn lessons are shared on the board so peers + retries learn from each probe,
    # but they must NOT count as dispatches (that would exhaust the RETRY_MAX budget prematurely).
    be = _be(tmp_path)
    sig = "sqli|/login|anon"
    bb.post_fact("g3", "lead", "hypothesis", {"signature": sig, "title": "login SQLi"}, backend=be)
    bb.post_fact("g3", "lead", "attempt", {"signature": sig, "attempt": 1}, backend=be)  # 1 dispatch
    # many per-turn lessons from the SAME dispatch
    for i in range(5):
        bb.post_fact("g3", "agent-x", "attempt_lesson",
                     {"signature": sig, "lesson": f"[union] WAF blocks UNION variant {i}"}, backend=be)
    st = bb.assemble_state("g3", backend=be)
    oh = [h for h in st["open_hypotheses"] if h.get("signature") == sig]
    assert oh, "hypothesis should still be OPEN — one dispatch, not six"
    assert oh[0]["attempt_count"] == 1, "attempt_lesson facts must not inflate the dispatch counter"
    assert oh[0]["lessons"], "per-turn lessons should be folded in for the retry to learn from"


def test_attempt_lesson_dedupes_identical_text(tmp_path):
    be = _be(tmp_path)
    sig = "ssrf|/fetch|anon"
    bb.post_fact("g3b", "lead", "hypothesis", {"signature": sig}, backend=be)
    for _ in range(3):
        bb.post_fact("g3b", "a", "attempt_lesson", {"signature": sig, "lesson": "same note"}, backend=be)
    st = bb.assemble_state("g3b", backend=be)
    oh = [h for h in st["open_hypotheses"] if h.get("signature") == sig][0]
    assert oh["lessons"].count("same note") == 1
