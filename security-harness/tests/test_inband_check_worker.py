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
