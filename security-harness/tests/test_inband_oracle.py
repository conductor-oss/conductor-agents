"""In-band verifier trust core (PLAN_V3 5.4, item #3): oracle math + receipt gate + deepen
promotion, incl. the false-positive guards and the no-self-confirmation invariant."""
from common import deepen, evidence
from common import inband_oracle as io


# --- SSRF differential oracle ---

def test_ssrf_confirms_on_blocked_control_and_marked_bypass():
    r = io.ssrf_differential(
        control={"status": 403, "body": "blocked"},
        test={"status": 200, "body": "{\"openapi\":\"3.0\",\"title\":\"internal-admin\"}"},
        markers=["internal-admin"])
    assert r["confirmed"] is True and r["marker"] == "internal-admin"


def test_ssrf_bare_2xx_without_marker_is_not_confirmed():
    r = io.ssrf_differential(control={"status": 403, "body": "no"},
                             test={"status": 200, "body": "generic ok page"}, markers=["internal-admin"])
    assert r["confirmed"] is False and r["reached_backend"] is False


def test_ssrf_marker_present_in_control_too_is_not_confirmed():
    # Front-end echoes the marker for everyone -> not a differential.
    r = io.ssrf_differential(control={"status": 403, "body": "internal-admin banner"},
                             test={"status": 200, "body": "internal-admin banner"},
                             markers=["internal-admin"])
    assert r["confirmed"] is False


def test_ssrf_control_not_blocked_is_not_confirmed():
    r = io.ssrf_differential(control={"status": 200, "body": "x"},
                             test={"status": 200, "body": "internal-admin"}, markers=["internal-admin"])
    assert r["confirmed"] is False and r["control_blocked"] is False


# --- RCE arithmetic oracle ---

def test_rce_confirms_when_product_echoed_and_operands_absent():
    r = io.rce_arithmetic(body="result is 1787569 done", operand_a=1337, operand_b=1337)
    assert r["confirmed"] is True and r["product"] == 1787569


def test_rce_reflected_operand_is_not_confirmed():
    # The payload "1337*1337" reflected verbatim: operand present -> reflection, not evaluation.
    r = io.rce_arithmetic(body="you sent 1337*1337 and 1787569", operand_a=1337, operand_b=1337)
    assert r["confirmed"] is False and r["operands_reflected"] is True


# --- receipt gate (evidence.inband_confirmation) ---

def _ssrf_receipt(**over):
    r = {"verification": evidence.INBAND_VERIFIER, "available": True, "oracle": "ssrf_differential",
         "confirmed": True, "control_blocked": True, "reached_backend": True, "marker": "internal-admin"}
    r.update(over)
    return r


def test_gate_accepts_wellformed_receipt():
    ok, ev = evidence.inband_confirmation(_ssrf_receipt())
    assert ok is True and "SSRF differential" in ev


def test_gate_rejects_forged_confirmed_without_proof():
    # confirmed:true but the proof fields are missing -> rejected.
    ok, _ = evidence.inband_confirmation(_ssrf_receipt(marker="", reached_backend=False))
    assert ok is False


def test_gate_rejects_wrong_verification_string():
    ok, _ = evidence.inband_confirmation(_ssrf_receipt(verification="sandbox_says_so"))
    assert ok is False


def test_gate_rejects_non_dict():
    assert evidence.inband_confirmation("confirmed!") == (False, "")


# --- deepen promotion + the no-self-confirmation invariant ---

_STATE = {"sink_class": "ssrf", "ladder": ["oob-confirm"], "ladder_detail": [{"family": "oob-confirm"}]}


def test_detect_confirmation_promotes_on_inband_receipt():
    ok, ev = deepen.detect_confirmation(inband_verification=_ssrf_receipt())
    assert ok is True and "marker" in ev


def test_observe_promotes_on_inband_receipt():
    new = deepen.observe(dict(_STATE), "oob-confirm", "tried [::1] bypass",
                         inband_verification=_ssrf_receipt())
    assert new["confirmed"] is True
    assert new["ledger"]["oob-confirm"]["outcome"] == "confirmed"


def test_observe_never_confirms_from_sandbox_stdout():
    # A rich but UNTRUSTED sandbox result must never confirm -- only verifier receipts do.
    new = deepen.observe(dict(_STATE), "oob-confirm", "x",
                         result={"evidence": ["looks confirmed!"], "findings": [{"x": 1}],
                                 "oob": [{"token": "t"}]})
    assert new.get("confirmed") is not True
