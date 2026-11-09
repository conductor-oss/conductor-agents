"""Phase 0 groups 4-5: aggregate per-run spend gate — accumulation, the ok/warn/halt decision, the
truthful report, the not-startable rule, and the no-wall-clock-deadline guarantee."""
import common  # noqa: F401  -- triggers the sys.path shim for swarm_substrate

from common import budget_gate as bg
from swarm_substrate import blackboard as bb


def _be(tmp_path):
    return bb._LocalBackend(str(tmp_path))


def test_gate_states_ok_warn_halt():
    assert bg.gate(0.0, 100.0)["state"] == "ok"
    assert bg.gate(50.0, 100.0)["state"] == "ok"
    assert bg.gate(80.0, 100.0)["state"] == "warn"
    assert bg.gate(95.0, 100.0)["state"] == "warn"
    assert bg.gate(100.0, 100.0)["state"] == "halt"
    assert bg.gate(120.0, 100.0)["state"] == "halt"


def test_gate_reports_percent_and_warn_point():
    d = bg.gate(90.0, 100.0, warn_fraction=0.8)
    assert d["warn_at_usd"] == 80.0 and d["percent"] == 90.0 and d["enforceable"] is True


def test_gate_unbounded_when_no_ceiling():
    for c in (None, 0, -5):
        d = bg.gate(10.0, c)
        assert d["state"] == "unbounded" and d["enforceable"] is False


def test_not_startable_without_ceiling_in_swarm_mode():
    assert bg.is_startable(None, swarm_mode=True) is False
    assert bg.is_startable(0, swarm_mode=True) is False
    assert bg.is_startable(50.0, swarm_mode=True) is True
    # Single-agent mode is unaffected — it keeps per-agent caps.
    assert bg.is_startable(None, swarm_mode=False) is True


def test_record_spend_accumulates_across_authors(tmp_path):
    be = _be(tmp_path)
    usage_a = {"input_tokens": 1_000_000, "output_tokens": 0}   # claude-sonnet-5 -> $3.00
    usage_b = {"input_tokens": 0, "output_tokens": 1_000_000}   # -> $15.00
    bg.record_spend("run1", "wf-a", usage=usage_a, model="claude-sonnet-5", backend=be)
    total = bg.record_spend("run1", "wf-b", usage=usage_b, model="claude-sonnet-5", backend=be)
    assert total["usd"] == 18.0
    assert total["by_author"]["wf-a"]["usd"] == 3.0
    assert total["by_author"]["wf-b"]["usd"] == 15.0


def test_evaluate_folds_board_spend_and_gates(tmp_path):
    be = _be(tmp_path)
    bg.record_spend("run2", "wf-a", usd=85.0, tokens=10, model="x", backend=be)
    out = bg.evaluate("run2", ceiling_usd=100.0, backend=be)
    assert out["state"] == "warn" and out["report"]["usd"] == 85.0
    assert out["report"]["ceiling_usd"] == 100.0
    bg.record_spend("run2", "wf-b", usd=20.0, tokens=10, model="x", backend=be)
    out2 = bg.evaluate("run2", ceiling_usd=100.0, backend=be)
    assert out2["state"] == "halt" and out2["report"]["usd"] == 105.0


def test_report_is_append_only_audit_trail(tmp_path):
    be = _be(tmp_path)
    for i in range(5):
        bg.record_spend("run3", "wf-a", usd=1.0, tokens=1, model="m", backend=be)
    spend = bg.read_spend("run3", backend=be)
    assert spend["fact_count"] == 5 and spend["usd"] == 5.0


def test_no_wall_clock_deadline_mechanism_in_module():
    # AGENTS.md timeout policy: the gate must introduce no execution-deadline MECHANISM. We check for
    # concrete deadline APIs, not the prose word "timeout" (the docstring legitimately cites the
    # policy). The module does no time accounting at all, so none of these should appear in its code.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(bg))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"time", "asyncio", "signal"}), \
        f"budget gate must not import time/asyncio/signal; got {imported}"
    src = inspect.getsource(bg)
    for mech in ("wait_for(", ".alarm(", "--max-time", "--connect-timeout", "setitimer"):
        assert mech not in src, f"budget gate must not use deadline mechanism {mech!r}"
