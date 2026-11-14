"""Phase 1 group 7: shared-file ownership on the board — exclusive live ownership resolved lock-free
from the append-only log, release-and-promote, and the dispatch guard."""

from common import file_ownership as fo
from common import blackboard as bb


def _be(tmp_path):
    return bb._LocalBackend(str(tmp_path))


def test_first_claimant_owns_the_file(tmp_path):
    be = _be(tmp_path)
    r = fo.claim("run", "wf-a", "schema.py", backend=be)
    assert r["granted"] is True and r["owner"] == "wf-a"


def test_second_claimant_waits_exactly_one_live_owner(tmp_path):
    be = _be(tmp_path)
    fo.claim("run", "wf-a", "schema.py", backend=be)
    r = fo.claim("run", "wf-b", "schema.py", backend=be)
    assert r["granted"] is False and r["owner"] == "wf-a"
    # Exactly one live owner in the folded map.
    assert fo.owner_map("run", backend=be) == {"schema.py": "wf-a"}


def test_release_promotes_the_waiting_claimant(tmp_path):
    be = _be(tmp_path)
    fo.claim("run", "wf-a", "schema.py", backend=be)
    fo.claim("run", "wf-b", "schema.py", backend=be)   # queued
    fo.release("run", "wf-a", "schema.py", backend=be)
    assert fo.owner_map("run", backend=be) == {"schema.py": "wf-b"}


def test_release_frees_file_when_no_waiters(tmp_path):
    be = _be(tmp_path)
    fo.claim("run", "wf-a", "a.py", backend=be)
    fo.release("run", "wf-a", "a.py", backend=be)
    assert fo.owner_map("run", backend=be) == {}
    # now grantable again
    assert fo.claim("run", "wf-c", "a.py", backend=be)["granted"] is True


def test_dispatch_guard_blocks_on_other_owner(tmp_path):
    be = _be(tmp_path)
    fo.claim("run", "wf-a", "schema.py", backend=be)
    out = fo.can_dispatch("run", "wf-b", ["schema.py", "free.py"], backend=be)
    assert out["dispatchable"] is False and out["blocked_by"] == {"schema.py": "wf-a"}
    # the owner itself is never blocked by its own file
    assert fo.can_dispatch("run", "wf-a", ["schema.py"], backend=be)["dispatchable"] is True


def test_fold_is_deterministic_and_pure():
    events = [
        {"ts": 1, "author": "wf-a", "data": {"op": "claim", "path": "f", "author": "wf-a"}},
        {"ts": 2, "author": "wf-b", "data": {"op": "claim", "path": "f", "author": "wf-b"}},
        {"ts": 3, "author": "wf-a", "data": {"op": "release", "path": "f", "author": "wf-a"}},
    ]
    assert fo.fold_owner_map(events) == {"f": "wf-b"}
    # same input -> same output, no IO
    assert fo.fold_owner_map(events) == {"f": "wf-b"}
