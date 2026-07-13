import json
import os

from conftest import REPO
from common import feature_exercise as fx
from common import profiles
from codeexec import sandbox_sc
from codeexec import tasks as code_tasks
from httptool import tasks as http_tasks
from rag import tasks as rag_tasks


def _playbook():
    profile = profiles.load("vuln-app")
    objectives = list(dict.fromkeys(
        finding["objective"] for finding in profile["expected_findings"]
    ))
    return {
        "must_exercise": objectives,
        "primitives": [
            {
                "objective": objective,
                "task_type": "INLINE" if objective == "INFRA-RCE-INJECTION" else "HTTP",
                "abuse": objective,
                "how": "Exercise the corresponding vulnerable application endpoint.",
            }
            for objective in objectives
        ],
    }


def _ops():
    return [
        {
            "type": "workflow_registered",
            "workflow_name": "wf-http",
            "task_types": ["HTTP"],
            "objective_id": "AUTHZ-FUNCTION-LEVEL",
        },
        {
            "type": "workflow_started",
            "workflow_name": "wf-http",
            "execution_id": "e-http",
            "objective_id": "AUTHZ-FUNCTION-LEVEL",
        },
        {
            "type": "workflow_registered",
            "workflow_name": "wf-inline",
            "task_types": ["INLINE"],
            "objective_id": "INFRA-RCE-INJECTION",
        },
        {
            "type": "workflow_started",
            "workflow_name": "wf-inline",
            "execution_id": "e-inline",
            "objective_id": "INFRA-RCE-INJECTION",
        },
        {
            "type": "workflow_registered",
            "workflow_name": "wf-secret",
            "task_types": ["SIMPLE"],
            "objective_id": "INFRA-SECRET-SURFACE",
        },
        {
            "type": "workflow_started",
            "workflow_name": "wf-secret",
            "execution_id": "e-secret",
            "objective_id": "INFRA-SECRET-SURFACE",
        },
        {
            "type": "cve_attempt",
            "cve_id": "CVE-TEST",
            "dependency": "pkg@1",
            "objective_id": "INFRA-SUPPLY-CHAIN",
        },
    ]


def test_feature_gate_requires_real_started_workflows_and_cve_attempt():
    status = fx.evaluate(
        _playbook(),
        _ops(),
        [{"dependency": "pkg@1", "top_cves": [{"id": "CVE-TEST"}]}],
        {"cross_tenant": True},
        2,
    )
    assert status["complete"] is True
    assert status["workflows_run"] == 3
    assert {"HTTP", "INLINE", "SIMPLE"} <= set(status["task_types_exercised"])
    assert status["cves_attempted"] == [{"cve_id": "CVE-TEST", "dependency": "pkg@1"}]


def test_definition_without_execution_does_not_satisfy_gate():
    status = fx.evaluate(
        _playbook(),
        [{"type": "workflow_registered", "workflow_name": "x", "task_types": ["INLINE"],
          "objective_id": "INFRA-RCE-INJECTION"}],
        [],
        {"cross_tenant": True},
        2,
    )
    assert status["complete"] is False
    assert "INFRA-RCE-INJECTION" in status["pending"]


def test_capability_and_identity_gaps_are_blocked_not_silently_complete():
    status = fx.evaluate(_playbook(), [], [], {"cross_tenant": False}, 1)
    assert status["complete"] is True
    by_id = {x["id"]: x["reason"] for x in status["blocked"]}
    assert "INFRA-RCE-INJECTION" in by_id
    assert "AUTHZ-FUNCTION-LEVEL" in by_id


def test_mandatory_hypotheses_include_top_cve_and_product_primitives():
    hypotheses = fx.mandatory_hypotheses(
        _playbook(),
        [],
        [
            {"dependency": "low@1", "priority_score": 0.1, "top_cves": [{"id": "CVE-LOW"}]},
            {"dependency": "top@2", "priority_score": 2.0, "top_cves": [{"id": "CVE-TOP"}]},
        ],
        {"cross_tenant": True},
        2,
        {"admin": {"value": "x"}, "tenantB": {"value": "y"}},
    )
    assert {h["objective_id"] for h in hypotheses} >= {
        "INFRA-RCE-INJECTION", "AUTHZ-FUNCTION-LEVEL", "INFRA-SECRET-SURFACE",
        "INFRA-SUPPLY-CHAIN",
    }
    cve = next(h for h in hypotheses if h["objective_id"] == "INFRA-SUPPLY-CHAIN")
    assert cve["cve_id"] == "CVE-TOP" and cve["mandatory"] is True


def test_cve_focus_does_not_duplicate_the_version_matched_requirement():
    leads = [{"dependency": "pkg@1", "top_cves": [{"id": "CVE-TEST"}]}]
    status = fx.evaluate(
        {}, [], leads, {}, 2, ["INFRA-SUPPLY-CHAIN"], []
    )
    assert status["required"] == ["INFRA-SUPPLY-CHAIN"]
    assert status["pending"] == ["INFRA-SUPPLY-CHAIN"]


def test_cve_focus_without_a_version_matched_lead_is_explicitly_blocked():
    status = fx.evaluate(
        {}, [], [], {}, 2, ["INFRA-SUPPLY-CHAIN"], []
    )
    assert status["complete"] is True
    assert status["pending"] == []
    assert status["blocked"] == [{
        "id": "INFRA-SUPPLY-CHAIN",
        "reason": "no reachable version-matched CVE lead was discovered",
    }]


def test_focused_objective_is_machine_required_until_exploit_agent_is_scheduled():
    catalog = [{
        "id": "CONF-BOLA-CROSS-USER",
        "class": "authz",
        "objective": "Read another user's object",
        "required_capability": 1,
        "required_identities": ">=2 users",
        "how_to_test": "flip object ids across users",
        "impact_evidence": "cross-user response contrast",
    }]
    pending = fx.evaluate(
        {}, [], [], {"cross_user": True}, 1, ["CONF-BOLA-CROSS-USER"], catalog
    )
    assert pending["complete"] is False
    assert pending["pending"] == ["CONF-BOLA-CROSS-USER"]

    hypotheses = fx.mandatory_hypotheses(
        {}, [], [], {"cross_user": True}, 1,
        {"userA": {"value": "a"}, "userB": {"value": "b"}},
        ["CONF-BOLA-CROSS-USER"], catalog,
    )
    assert hypotheses[0]["mandatory_kind"] == "focused_objective"
    assert hypotheses[0]["test_plan"][0] == "flip object ids across users"

    complete = fx.evaluate(
        {},
        [{"type": "objective_attempt", "objective_id": "CONF-BOLA-CROSS-USER"}],
        [],
        {"cross_user": True},
        1,
        ["CONF-BOLA-CROSS-USER"],
        catalog,
    )
    assert complete["complete"] is True
    assert complete["completed"] == ["CONF-BOLA-CROSS-USER"]


def _vuln_app_feature_ops():
    return json.load(open(os.path.join(REPO, "profiles", "vuln-app.json"))).get(
        "feature_operations", []
    )


def test_sandbox_uses_generic_writes_without_profile_operation_rules(monkeypatch):
    monkeypatch.setattr(
        sandbox_sc,
        "_state",
        {"created": [], "evidence": [], "findings": [], "oob": [], "operations": []},
    )
    monkeypatch.setattr(sandbox_sc, "_FEATURE_OPS", _vuln_app_feature_ops())
    definition = {
        "name": "sc-pentest-wf",
        "tasks": [{"name": "fetch", "taskReferenceName": "fetch", "type": "HTTP"}],
    }
    sandbox_sc._record_api_operation(
        "POST", "https://target/api/metadata/workflow", definition, 200, {}, ""
    )
    sandbox_sc._record_api_operation(
        "POST", "https://target/api/workflow/sc-pentest-wf", {}, 200, "execution-123", "execution-123"
    )
    assert [op["type"] for op in sandbox_sc._state["operations"]] == [
        "product_write", "product_write"
    ]
    assert [op["path"] for op in sandbox_sc._state["operations"]] == [
        "/api/metadata/workflow", "/api/workflow/sc-pentest-wf"
    ]


def test_http_action_emits_secret_free_operation_record():
    class T:
        input_data = {
            "method": "POST",
            "url": "https://outside.example/api/items?token=secret",
            "scope": {"in_scope_hosts": ["allowed.example"]},
            "identities": {},
            "identity": "anon",
            "capability_max": 2,
        }

    out = http_tasks.http_request(T())
    assert out["operation"] == {
        "type": "http_request",
        "method": "POST",
        "path": "/api/items",
        "identity": "anon",
        "blocked_reason": "refused: out of scope",
    }
    assert "secret" not in json.dumps(out["operation"])


def test_code_exec_fails_closed_when_egress_jail_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(code_tasks, "_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(code_tasks, "EGRESS_MODE", "jail")
    monkeypatch.setattr(code_tasks.egress_mod, "ensure_jail", lambda target, oob: None)
    monkeypatch.setattr(code_tasks, "WORK_ROOT", str(tmp_path))

    class T:
        input_data = {
            "code": "print('x')",
            "target": "https://target",
            "scope": {"in_scope_hosts": ["target"]},
            "identities": {},
            "capability_max": 2,
            "run_id": "r",
            "agent": "a",
        }

    out = code_tasks.code_exec(T())
    assert out["ok"] is False
    assert "fails closed" in out["refused_reason"]


def test_js_docs_render_output_is_ingested(monkeypatch):
    monkeypatch.setattr(
        rag_tasks,
        "_fetch_url",
        lambda url, auth, scope: (b"<html><body><div id='root'></div></body></html>", "text/html"),
    )
    monkeypatch.setattr(rag_tasks, "_doc_site_urls", lambda entry, auth, scope: ["https://docs/x"])
    monkeypatch.setattr(
        rag_tasks,
        "_render_urls",
        lambda urls, auth, scope: (
            {"https://docs/x": "Create a workflow, register it, start it, and poll workflowId."},
            [],
        ),
    )

    class T:
        input_data = {"docs": ["https://docs"], "render_js": True}

    out = rag_tasks.ingest_docs(T())
    assert out["docs_available"] is True
    assert out["meta"]["rendered_sources"] == 1
    assert any("start it" in c["text"] for c in out["chunks"])


def test_e11_wiring_is_present_in_workflows_and_taskdefs():
    with open(os.path.join(REPO, "conductor", "workflows", "deep_assess.json")) as fh:
        deep = json.load(fh)
    refs = {
        t.get("taskReferenceName")
        for t in deep["tasks"]
    }
    loop_refs = {
        t.get("taskReferenceName")
        for t in next(t for t in deep["tasks"] if t["taskReferenceName"] == "pass_loop")["loopOver"]
    }
    assert "target_profile" in deep["inputParameters"]
    assert {"campaign_progress", "set_campaign_progress", "merge_focus"} <= loop_refs
    assert "operation_ledger" in deep["outputParameters"]
    assert "product_feature_exercise" in deep["outputParameters"]
    with open(os.path.join(REPO, "assess")) as fh:
        assess_cli = fh.read()
    assert ".documentation // []" in assess_cli
    assert "profile documentation enrolled" in assess_cli
    progress = next(t for t in next(
        t for t in deep["tasks"] if t["taskReferenceName"] == "pass_loop"
    )["loopOver"] if t["taskReferenceName"] == "campaign_progress")
    assert {"objective_focus", "catalog_objectives"} <= set(progress["inputParameters"])

    with open(os.path.join(REPO, "conductor", "workflows", "assess_pass.json")) as fh:
        assess_pass = json.load(fh)
    collect_ops = next(
        t for t in assess_pass["tasks"] if t["taskReferenceName"] == "collect_operations"
    )
    assert "objective_attempt" in collect_ops["inputParameters"]["queryExpression"]

    with open(os.path.join(REPO, "conductor", "workflows", "docs_ingest.json")) as fh:
        docs = json.load(fh)
    assert "render_js" in docs["inputParameters"]
    assert "index_ready" in docs["outputParameters"]

    for name in ("build_mandatory_hypotheses", "evaluate_campaign_progress"):
        with open(os.path.join(REPO, "conductor", "taskdefs", f"{name}.json")) as fh:
            assert json.load(fh)["pollTimeoutSeconds"] > 0


def test_every_simple_taskdef_has_a_poll_timeout():
    taskdefs = os.path.join(REPO, "conductor", "taskdefs")
    for filename in os.listdir(taskdefs):
        if filename.endswith(".json"):
            with open(os.path.join(taskdefs, filename)) as fh:
                taskdef = json.load(fh)
            assert taskdef.get("pollTimeoutSeconds", 0) > 0, filename


# ── Task 2: SAST-flagged injection sinks force an ACTIVE, OOB-confirmed exploit ──

def _sast_injection_sink():
    return [{"title": "Potential code injection via ScriptEngine.eval in workflow script evaluator",
             "evidence": "semgrep java.lang.security.audit.script-engine-injection at ScriptEvaluator.java:53",
             "source_tool": "sast"}]


def test_injection_sink_detection_is_generic():
    assert len(fx.injection_sinks(_sast_injection_sink())) == 1
    assert fx.injection_sinks([{"title": "SSTI in template renderer"}])       # SSTI
    assert fx.injection_sinks([{"title": "OS command injection in /ping"}])    # command
    assert fx.injection_sinks([{"title": "Insecure deserialization of user input"}])
    assert fx.injection_sinks([{"title": "Missing HSTS header"}]) == []        # not an injection sink


def test_sast_injection_forces_active_rce_exercise():
    sink = _sast_injection_sink()
    # cap>=2 + a flagged sink -> INFRA-RCE-INJECTION becomes a forced (pending) exercise.
    pending = fx.evaluate({}, [], [], {}, 2, [], [], sink)
    assert "INFRA-RCE-INJECTION" in pending["required"]
    assert "INFRA-RCE-INJECTION" in pending["pending"] and pending["complete"] is False
    # cap<2 -> honestly blocked, never silently complete.
    blocked = fx.evaluate({}, [], [], {}, 1, [], [], sink)
    assert any(b["id"] == "INFRA-RCE-INJECTION" for b in blocked["blocked"])
    # a recorded injection_attempt (sc.injection_attempt, any sink) satisfies the gate.
    done = fx.evaluate({}, [{"type": "injection_attempt", "note": "payload issued"}], [], {}, 2, [], [], sink)
    assert "INFRA-RCE-INJECTION" in done["completed"] and done["complete"] is True
    # no flagged sink -> not forced (no false pressure).
    assert "INFRA-RCE-INJECTION" not in fx.evaluate({}, [], [], {}, 2, [], [], [{"title": "Missing HSTS"}])["required"]


def test_mandatory_hypotheses_emits_active_injection_with_oob_canary():
    hyps = fx.mandatory_hypotheses({}, [], [], {}, 2, {"anon": {"value": "x"}}, [], [], _sast_injection_sink())
    inj = next(h for h in hyps if h.get("mandatory_kind") == "sast_injection")
    assert inj["objective_id"] == "INFRA-RCE-INJECTION"
    plan = " ".join(inj["test_plan"]).lower()
    assert "sc.oob" in plan and "sc.injection_attempt" in plan


def _sast_sql_sink():
    return [{"title": "Potential SQL injection via formatted SQL strings in archive/audit DAOs",
             "evidence": "semgrep java.lang.security.audit.formatted-sql-string at PostgresArchiveDAO.java:221",
             "source_tool": "sast"}]


def test_injection_sink_class_routes_each_sink_to_its_technique():
    assert fx.injection_sink_class(_sast_sql_sink()[0]) == "sqli"            # SQL not lumped as RCE
    assert fx.injection_sink_class(_sast_injection_sink()[0]) == "code-eval"
    assert fx.injection_sink_class({"title": "Path traversal in document download"}) == "traversal"
    assert fx.injection_sink_class({"title": "Missing HSTS header"}) == ""


def test_sql_sink_seeds_dedicated_sqli_hypothesis_even_after_rce_objective_completes():
    """The bug from run 10e885ea: a SAST SQLi sink was reported but never exploited, because it was
    lumped under INFRA-RCE-INJECTION which a JS/eval injection_attempt marked complete. The data-layer
    SQLi hypothesis must fire INDEPENDENTLY of the RCE objective's status, with a SQL-specific oracle."""
    ops = [{"type": "injection_attempt", "note": "JS eval tried"}]   # RCE objective already satisfied
    hyps = fx.mandatory_hypotheses({}, ops, [], {}, 2, {"anon": {"value": "x"}}, [], [], _sast_sql_sink())
    sqli = next((h for h in hyps if h.get("id") == "MAND-SQLI"), None)
    assert sqli is not None, "SQL sink must still be driven even after the RCE objective completes"
    assert sqli["category"] == "sqli"                          # -> exploit_deepen's sqli ladder
    blob = (sqli["rationale"] + " ".join(sqli["test_plan"])).lower()
    assert "time-based" in blob and "boolean" in blob and "error" in blob   # SQL oracle, not OOB-exec
    assert "downgrad" in blob                                  # no runtime PoC => downgrade discipline
    # the SQL sink alone must NOT also produce an exec-family RCE-flavored lump
    assert all(h.get("id") != "MAND-INJECTION" for h in hyps)


def test_sqli_hypothesis_routes_to_the_sql_deepen_ladder():
    from common import deepen
    fam, _ = deepen.ladder_for({"category": "sqli", "title": "SQL injection via archive search"})
    assert fam == "sqli"


# ── fix #2: operations are recorded at the SESSION layer (raw sc.session, not just sc.api) ──

def test_raw_session_records_generic_write_operations(monkeypatch):
    import requests
    monkeypatch.setattr(sandbox_sc, "_state",
                        {"created": [], "evidence": [], "findings": [], "oob": [], "operations": []})
    monkeypatch.setattr(sandbox_sc, "SCOPE", {"in_scope_hosts": ["app.test"]})
    monkeypatch.setattr(sandbox_sc, "IDENTITIES", {"anon": {}})
    monkeypatch.setattr(sandbox_sc, "flush", lambda: None)
    monkeypatch.setattr(sandbox_sc, "_FEATURE_OPS", _vuln_app_feature_ops())

    class _R:
        def __init__(self, payload, text=""):
            self.status_code = 200; self._p = payload; self.text = text; self.url = "https://app.test/x"
        def json(self):
            if self._p is None:
                raise ValueError("no json")
            return self._p

    def _fake(self, method, url, **kw):
        if url.rstrip("/").endswith("/api/metadata/workflow"):
            return _R({})
        return _R("exec-1", '"exec-1"')

    monkeypatch.setattr(requests.Session, "request", _fake)

    s = sandbox_sc.session("anon")                       # RAW session — NOT sc.api()
    s.post("https://app.test/api/metadata/workflow", json={"name": "sc-pentest-w", "tasks": [{"type": "INLINE"}]})
    s.post("https://app.test/api/workflow/sc-pentest-w", json={})
    assert [o["type"] for o in sandbox_sc._state["operations"]] == [
        "product_write", "product_write"
    ]
    assert [o["path"] for o in sandbox_sc._state["operations"]] == [
        "/api/metadata/workflow", "/api/workflow/sc-pentest-w"
    ]


def test_coverage_and_cve_completion_survive_a_truncated_ledger():
    """Phase 2a: even when the operation ledger is EMPTY (tail-sliced away), technique_coverage,
    cves_attempted, and INFRA-SUPPLY-CHAIN completion are recovered from the deepen_states ledgers
    — the deep work stays visible despite the 220-op truncation."""
    deepen_states = [
        {"objective_id": "INFRA-SUPPLY-CHAIN", "cve_id": "CVE-2026-44249",
         "dependency": "io.netty:netty-handler@4.1.133.Final",
         "ledger": {"ssrf-filter-bypass": {"tries": 3, "outcome": "blocked"},
                    "dos-amplification": {"tries": 1, "outcome": "blocked"}}},
        {"objective_id": "INFRA-RCE-INJECTION",
         "ledger": {"error-based": {"tries": 2}, "time-blind": {"tries": 1}, "union-based": {"tries": 0}}},
    ]
    # operations is empty (simulating truncation) — coverage must still come from deepen_states
    f = fx.evaluate({}, [], [], {}, 2, [], [], None, deepen_states=deepen_states)
    tc = f["technique_coverage"]
    assert set(tc["INFRA-SUPPLY-CHAIN"]["tried_families"]) == {"ssrf-filter-bypass", "dos-amplification"}
    assert tc["INFRA-RCE-INJECTION"]["n_tried"] == 2          # union-based had 0 tries -> excluded
    assert {"cve_id": "CVE-2026-44249", "dependency": "io.netty:netty-handler@4.1.133.Final"} in f["cves_attempted"]
    # cve_attempt recovered from deepen_states -> INFRA-SUPPLY-CHAIN completes, not pending
    assert "INFRA-SUPPLY-CHAIN" not in f["pending"]


def test_collected_cve_deepen_state_completes_supply_chain():
    """Regression for the residual bug: collect_deepen now carries cve_id, so a CVE deepen state
    (shape it emits) flows to _states_coverage -> cves_attempted populates AND INFRA-SUPPLY-CHAIN
    completes, even when the cve_attempt ops were tail-sliced from the 220-cap operation ledger."""
    states = [{"objective_id": "INFRA-SUPPLY-CHAIN", "sink_class": "cve",
               "cve_id": "CVE-2026-44249", "dependency": "io.netty:netty-handler@4.1.133.Final",
               "ledger": {"published-poc": {"tries": 1}, "payload-variant": {"tries": 1}},
               "confirmed": False}]
    leads = [{"dependency": "io.netty:netty-handler@4.1.133.Final", "version_known": True,
              "top_cves": [{"id": "CVE-2026-44249"}]}]
    f = fx.evaluate({}, [], leads, {}, 2, [], [], None, deepen_states=states)
    assert {"cve_id": "CVE-2026-44249", "dependency": "io.netty:netty-handler@4.1.133.Final"} in f["cves_attempted"]
    assert "INFRA-SUPPLY-CHAIN" in f["completed"] and "INFRA-SUPPLY-CHAIN" not in f["pending"]
