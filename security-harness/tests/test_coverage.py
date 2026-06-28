"""Multi-dimensional coverage ledger (spec 22)."""
from common import coverage


APP_MODEL = {
    "sensitive_operations": ["delete user account", "read stored secret"],
    "object_id_patterns": ["userId", "invoiceId"],
    "trust_boundaries": ["outbound HTTP fetch"],
}
DOCS = {"documented_invariants": [
    {"invariant": "a coupon is single-use"},
    {"invariant": "only the owner can delete an invoice"},
]}
PERSONAS = [{"label": "anon", "persona": "anonymous internet attacker"},
            {"label": "userA", "persona": "ordinary authenticated user"}]


def test_build_cells_spans_dimensions():
    cells = coverage.build_cells(APP_MODEL, PERSONAS, DOCS)
    dims = {c["dimension"] for c in cells}
    assert {"persona", "invariant", "sensitive_operation", "object_id", "interface"} <= dims


def test_untested_when_nothing_references():
    res = coverage.build(APP_MODEL, PERSONAS, DOCS, confirmed=[], tried=[], rejected=[])
    statuses = {c["status"] for c in res["ledger"]}
    assert statuses == {"untested"}  # nothing tested -> every cell untested
    assert res["summary"]["by_status"].get("untested", 0) > 0


def test_confirmed_finding_marks_cell_tested():
    confirmed = [{"title": "Invoice owner check bypass lets a user delete an invoice",
                  "category": "bola", "owasp": "A01"}]
    res = coverage.build(APP_MODEL, PERSONAS, DOCS, confirmed=confirmed, tried=[], rejected=[])
    inv_cells = [c for c in res["ledger"] if c["dimension"] == "invariant"
                 and "owner" in c["key"]]
    assert inv_cells and inv_cells[0]["status"] == "tested"


def test_tried_only_marks_partial():
    tried = ["bola|delete invoice owner|userA"]
    res = coverage.build(APP_MODEL, PERSONAS, DOCS, confirmed=[], tried=tried, rejected=[])
    partials = [c for c in res["ledger"] if c["status"] == "partial"]
    assert partials  # the owner-delete invariant cell is partially covered


def test_summary_reports_untested_keys():
    res = coverage.build(APP_MODEL, PERSONAS, DOCS, confirmed=[], tried=[], rejected=[])
    assert res["summary"]["untested_keys"]
    assert res["summary"]["total_cells"] == len(res["ledger"])
