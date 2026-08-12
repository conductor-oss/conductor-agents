from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "scripts" / "audit_no_hidden_timeouts.py"


def test_every_production_function_and_worker_has_no_hidden_execution_timeout():
    spec = importlib.util.spec_from_file_location("audit_no_hidden_timeouts", AUDIT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = module.audit()

    assert result["pythonFilesScanned"] > 0
    assert result["functionsScanned"] > 0
    assert result["workersScanned"]
    assert result["conductorTimeoutFieldsChecked"] > 0
    assert result["violations"] == []
    assert result["passed"] is True
