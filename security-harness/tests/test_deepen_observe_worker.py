"""deepen_observe worker (recon/tasks.py): the completion-gate fix must hold at the actual worker
boundary, not just in the pure deepen.observe()/attempt_op() simulation -- this is where
`run_code.output` (carrying code_exec's failure_class) actually reaches the ladder."""
import types

from recon import tasks as rt


def _task(**inp):
    return types.SimpleNamespace(input_data=inp)


def _hypothesis():
    return {"objective_id": "INFRA-RCE-INJECTION", "category": "sqli", "title": "SQLi in /search"}


def test_infra_failed_run_code_result_emits_no_completion_op():
    out = rt.deepen_observe(_task(
        hypothesis=_hypothesis(), family="error-based", lesson="docker down",
        result={"ok": False, "error": "docker not available on the worker host",
                "failure_class": "infra"},
    ))
    assert out["operation"] == {}
    assert out["confirmed"] is False
    assert out["state"]["consecutive_infra_failures"] == 1


def test_policy_refused_run_code_result_emits_no_completion_op():
    out = rt.deepen_observe(_task(
        hypothesis=_hypothesis(), family="error-based", lesson="no permission",
        result={"ok": False, "error": "refused: capability", "failure_class": "policy",
                "refused_reason": "code_exec needs capability level 2 but the campaign is authorized to level 1"},
    ))
    assert out["operation"] == {}
    assert out["state"]["policy_refused"] is True


def test_genuine_run_code_result_still_emits_completion_op():
    out = rt.deepen_observe(_task(
        hypothesis=_hypothesis(), family="error-based", lesson="parameterized, no error leaked",
        result={"ok": False, "exit_code": 0, "timed_out": False, "stdout": "200 generic response"},
    ))
    assert out["operation"].get("type") == "injection_attempt"
    assert out["operation"].get("family") == "error-based"
    assert out["accepted_family"] is True
