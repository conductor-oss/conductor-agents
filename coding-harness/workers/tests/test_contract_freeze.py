"""Phase 1 group 6: frozen-contract discipline — a diff respecting the contract is ordinary and
integration-eligible; a diff editing the contract is a controlled event."""
from common import contract_freeze as cf


def test_diff_respecting_contract_is_ordinary():
    out = cf.classify_change(["src/handler.py", "tests/test_handler.py"],
                             ["src/contract.py", "tests/test_contract.py"])
    assert out["event"] == "ordinary" and out["touches_contract"] is False
    assert cf.is_integration_eligible(["src/handler.py"], ["src/contract.py"]) is True


def test_diff_editing_contract_is_controlled_event():
    out = cf.classify_change(["src/handler.py", "src/contract.py"], ["src/contract.py"])
    assert out["event"] == "controlled" and out["touches_contract"] is True
    assert out["contract_paths_touched"] == ["src/contract.py"]
    assert cf.is_integration_eligible(["src/contract.py"], ["src/contract.py"]) is False


def test_contract_entry_may_name_a_directory():
    out = cf.classify_change(["api/v1/schema.py"], ["api/v1"])
    assert out["touches_contract"] is True and out["contract_paths_touched"] == ["api/v1/schema.py"]


def test_path_normalization_matches_equivalent_spellings():
    out = cf.classify_change(["./src/contract.py"], ["src/contract.py"])
    assert out["touches_contract"] is True


def test_directory_prefix_is_not_fooled_by_name_overlap():
    # "src/contract" must not match "src/contract_helpers.py" (a sibling, not inside it).
    out = cf.classify_change(["src/contract_helpers.py"], ["src/contract"])
    assert out["touches_contract"] is False


def test_empty_inputs_are_ordinary():
    assert cf.classify_change([], [])["event"] == "ordinary"
    assert cf.classify_change(["a.py"], [])["event"] == "ordinary"
    assert cf.classify_change([], ["c.py"])["event"] == "ordinary"
