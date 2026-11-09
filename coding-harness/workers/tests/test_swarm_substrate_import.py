"""Phase 0 group 3: the coding-harness workers can import the shared ``swarm_substrate`` library and
round-trip a fact. The library is otherwise unused by any coding-harness workflow yet (it is consumed
by the Phase 1 convergence primitives and later phases)."""
import common  # noqa: F401  -- triggers the common/__init__ sys.path shim that locates swarm_substrate


def test_coding_harness_can_import_substrate():
    from swarm_substrate import blackboard as bb
    assert hasattr(bb, "post_fact") and hasattr(bb, "read_facts")


def test_fact_roundtrip_against_local_backend(tmp_path):
    from swarm_substrate import blackboard as bb
    be = bb._LocalBackend(str(tmp_path))
    bb.post_fact("run1", "wf-impl", "progress", {"item": "7", "state": "in-progress"}, backend=be)
    bb.post_fact("run1", "wf-impl", "progress", {"item": "7", "state": "done"}, backend=be)
    facts = bb.read_facts("run1", kind="progress", backend=be)
    assert [f["data"]["state"] for f in facts] == ["in-progress", "done"]


def test_message_bus_roundtrip(tmp_path):
    from swarm_substrate import blackboard as bb
    be = bb._LocalBackend(str(tmp_path))
    bb.send_message("run1", "wf-a", to="wf-b", body="schema.py is yours until #3 merges",
                    sender_role="lead", intent="assign", backend=be)
    box = bb.read_inbox("run1", "wf-b", "impl", backend=be)
    assert box["available"] and "schema.py is yours" in box["digest"]
