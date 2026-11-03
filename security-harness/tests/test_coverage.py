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


def test_docs_search_lexical_retrieval_no_vectordb(tmp_path, monkeypatch):
    # Live per-agent search_docs must work WITHOUT a vector DB (BM25 over the per-run chunk store).
    import types
    from rag import tasks as rt
    monkeypatch.setattr(rt, "_docs_store_path", lambda rid: str(tmp_path / f"{rid}.json"))
    rt._save_docs_store("r1", [
        {"docId":"h-0","text":"Human-in-the-loop HUMAN task pauses the workflow until an assigned user claims and completes it; approval gates progression.","metadata":{"source":"docs/human","section":"Human Tasks"}},
        {"docId":"w-0","text":"Webhook id is a time-UUID; a verified callback starts the workflow.","metadata":{"source":"docs/webhook","section":"Webhooks"}},
    ])
    out = rt.docs_search(types.SimpleNamespace(input_data={"run_id":"r1","query":"who can complete a human approval task","maxResults":1}))
    assert out["available"] is True and out["hits"] == 1
    assert "human" in out["result"][0]["text"].lower()
    # missing store / empty query -> graceful, not raising
    assert rt.docs_search(types.SimpleNamespace(input_data={"run_id":"nope","query":"x"}))["available"] is False
    assert rt.docs_search(types.SimpleNamespace(input_data={"run_id":"r1","query":""}))["available"] is False
