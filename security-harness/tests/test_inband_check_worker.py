"""In-band verifier runtime worker (item #8): inband_check RE-ISSUES the oracle itself and emits a
typed inband_check/v1 receipt -- confirmation never trusts sandbox stdout. The HTTP re-issue is
monkeypatched here; end-to-end fetch correctness is a live-acceptance concern."""
import types

from common import evidence
from oob import tasks as oob_tasks


def _task(inp):
    return types.SimpleNamespace(input_data=inp)


def test_ssrf_oracle_confirms_via_reissued_differential(monkeypatch):
    calls = []

    def fake_issue(req, identities, base):
        calls.append(req.get("path"))
        if str(req.get("path", "")).endswith("control"):
            return {"status": 403, "body": "blocked"}
        return {"status": 200, "body": "{\"internal-admin\": true, \"secret\": \"x\"}"}

    monkeypatch.setattr(oob_tasks, "_issue", fake_issue)
    probe = {"oracle": "ssrf_differential",
             "control": {"path": "/api/fetch?u=control"},
             "test": {"path": "/api/fetch?u=bypass"},
             "markers": ["internal-admin"]}
    out = oob_tasks.inband_check(_task({"probe": probe, "base_url": "http://t"}))
    assert out["available"] is True and out["confirmed"] is True
    assert evidence.inband_confirmation(out)[0] is True
    assert len(calls) == 2  # re-issued BOTH control and test itself


def test_ssrf_bare_2xx_without_marker_not_confirmed(monkeypatch):
    monkeypatch.setattr(oob_tasks, "_issue", lambda req, i, b:
                        {"status": 403, "body": "no"} if str(req.get("path", "")).endswith("control")
                        else {"status": 200, "body": "generic ok page"})
    probe = {"oracle": "ssrf_differential", "control": {"path": "/control"},
             "test": {"path": "/test"}, "markers": ["internal-admin"]}
    out = oob_tasks.inband_check(_task({"probe": probe, "base_url": "http://t"}))
    assert out["confirmed"] is False
    assert evidence.inband_confirmation(out)[0] is False


def test_rce_oracle_confirms_via_reissued_echo(monkeypatch):
    product = 1337 * 7919
    monkeypatch.setattr(oob_tasks, "_issue", lambda req, i, b: {"status": 200, "body": f"=> {product}"})
    probe = {"oracle": "rce_arithmetic", "request": {"path": "/run"}, "operand_a": 1337, "operand_b": 7919}
    out = oob_tasks.inband_check(_task({"probe": probe, "base_url": "http://t"}))
    assert out["available"] is True and out["confirmed"] is True


def test_no_probe_is_unavailable():
    out = oob_tasks.inband_check(_task({"probe": None}))
    assert out["available"] is False and out["confirmed"] is False


def test_unknown_oracle_is_unavailable():
    out = oob_tasks.inband_check(_task({"probe": {"oracle": "nope"}}))
    assert out["available"] is False


# --- new oracles produced by the verifier worker (item: oracle breadth end-to-end) ---

def test_time_blind_oracle_confirms_via_reissued_timing(monkeypatch):
    def fake_issue(req, identities, base):
        slow = "sleep" in str(req.get("path", "")).lower()
        return {"status": 200, "body": "ok", "elapsed_ms": 5200 if slow else 110}
    monkeypatch.setattr(oob_tasks, "_issue", fake_issue)
    probe = {"oracle": "time_blind_sql", "test": {"path": "/x?q=1 AND SLEEP(5)"},
             "control": {"path": "/x?q=1"}, "injected_delay_ms": 5000}
    out = oob_tasks.inband_check(_task({"probe": probe, "base_url": "http://t"}))
    assert out["confirmed"] is True and evidence.inband_confirmation(out)[0] is True


def test_path_traversal_oracle_confirms_on_canary(monkeypatch):
    monkeypatch.setattr(oob_tasks, "_issue", lambda req, i, b:
                        {"status": 200, "body": "root:x:0:0:root", "elapsed_ms": 10}
                        if "etc" in str(req.get("path", "")) else {"status": 200, "body": "normal", "elapsed_ms": 10})
    probe = {"oracle": "path_traversal_canary", "test": {"path": "/f?p=../../etc/passwd"},
             "control": {"path": "/f?p=ok.txt"}, "canary": "root:x:0:0"}
    out = oob_tasks.inband_check(_task({"probe": probe, "base_url": "http://t"}))
    assert out["confirmed"] is True and evidence.inband_confirmation(out)[0] is True


def test_race_oracle_confirms_when_burst_breaks_enforced_rule(monkeypatch):
    seq = iter([200, 409])  # sequential: first ok, retry rejected -> rule enforced

    def fake_issue(req, i, b):
        try:
            return {"status": next(seq), "body": "", "elapsed_ms": 5}
        except StopIteration:
            return {"status": 200, "body": "", "elapsed_ms": 5}  # shouldn't be hit
    monkeypatch.setattr(oob_tasks, "_issue", fake_issue)
    monkeypatch.setattr(oob_tasks, "_issue_burst", lambda req, i, b, n: [200, 200, 200, 409])
    probe = {"oracle": "race_invariant", "boundary_request": {"path": "/redeem"},
             "allowed_successes": 1, "burst": 4}
    out = oob_tasks.inband_check(_task({"probe": probe, "base_url": "http://t"}))
    assert out["confirmed"] is True and evidence.inband_confirmation(out)[0] is True


def test_auth_state_oracle_confirms_when_illegal_persists(monkeypatch):
    def fake_issue(req, i, b):
        p = str(req.get("path", ""))
        if "control" in p:
            return {"status": 403, "body": "forbidden", "elapsed_ms": 5}   # legal guard enforced
        if "readback" in p:
            return {"status": 200, "body": '{"state":"refunded"}', "elapsed_ms": 5}
        return {"status": 200, "body": "ok", "elapsed_ms": 5}
    monkeypatch.setattr(oob_tasks, "_issue", fake_issue)
    probe = {"oracle": "auth_state_illegal_transition",
             "control": {"path": "/order/1/refund?as=control"},
             "test": {"path": "/order/1/refund"}, "readback": {"path": "/order/1?v=readback"},
             "persisted_marker": "refunded", "from_state": "shipped", "to_state": "refunded"}
    out = oob_tasks.inband_check(_task({"probe": probe, "base_url": "http://t"}))
    assert out["confirmed"] is True and evidence.inband_confirmation(out)[0] is True


def test_stored_xss_oracle_needs_execution_signal(monkeypatch):
    monkeypatch.setattr(oob_tasks, "_issue", lambda req, i, b:
                        {"status": 200, "body": "page has CNRY123 marker", "elapsed_ms": 5})
    base = {"oracle": "stored_xss_victim_context", "store": {"path": "/profile"},
            "view": {"path": "/u/userA"}, "marker": "CNRY123",
            "author_identity": "userA", "victim_identity": "admin"}
    # delivered cross-identity but NO execution signal -> not confirmed
    out = oob_tasks.inband_check(_task({"probe": base, "base_url": "http://t"}))
    assert out["confirmed"] is False
    # with the execution signal (OOB/browser) -> confirmed
    out2 = oob_tasks.inband_check(_task({"probe": {**base, "executed_in_victim_context": True},
                                         "base_url": "http://t"}))
    assert out2["confirmed"] is True and evidence.inband_confirmation(out2)[0] is True
