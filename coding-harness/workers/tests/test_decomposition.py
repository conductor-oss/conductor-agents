"""Closure plan WP3 (H12/H14): the decomposition gate and mode routing read off the plan's shape."""
import common  # noqa: F401
from common import decomposition as dc, intake


def _it(i, files, deps=()):
    return {"id": i, "description": i, "files": list(files), "dependsOn": list(deps)}


def test_mode_is_read_off_the_shape():
    assert dc.assess([_it("a", ["a.py"])])["mode"] == "single"
    disjoint = dc.assess([_it("a", ["a.py"]), _it("b", ["b.py"]), _it("c", ["c.py"])])
    assert disjoint["mode"] == "parallel" and disjoint["verdict"] == "ok"
    shared = dc.assess([_it("a", ["reg.py", "a.py"]), _it("b", ["reg.py", "b.py"]), _it("c", ["reg.py", "c.py"])])
    assert shared["mode"] == "team" and shared["verdict"] == "ok"
    assert shared["evidence"]["sharedFiles"] == {"reg.py": ["a", "b", "c"]}
    ordered = dc.assess([_it("a", ["a.py"]), _it("b", ["b.py"], ["a"])])
    assert ordered["mode"] == "team" and ordered["evidence"]["edges"] == [["a", "b"]]


def test_contract_files_do_not_count_as_shared_scope():
    out = dc.assess([_it("a", ["api.py", "a.py"]), _it("b", ["api.py", "b.py"])], contract_paths=["api.py"])
    assert out["mode"] == "parallel" and out["evidence"]["sharedFiles"] == {}


def test_bad_cuts_are_rejected_with_precise_reasons():
    cases = {
        "cycle": [_it("a", ["a.py"], ["b"]), _it("b", ["b.py"], ["a"])],
        "unknown item": [_it("a", ["a.py"], ["ghost"]), _it("b", ["b.py"])],
        "names no files": [_it("a", []), _it("b", ["b.py"])],
        "duplicate item id": [_it("a", ["a.py"]), _it("a", ["b.py"])],
        "duplicate scope": [_it("a", ["x.py", "y.py"]), _it("b", ["x.py", "y.py"])],
        "overlap heavily": [_it("a", ["x.py", "y.py", "a.py"]), _it("b", ["x.py", "y.py", "b.py"])],
    }
    for needle, plan in cases.items():
        out = dc.assess(plan)
        assert out["verdict"] == "replan", needle
        assert any(needle in r for r in out["reasons"]), (needle, out["reasons"])
    # The same heavy overlap is fine once the items are ordered (B builds on A's merged result).
    ok = dc.assess([_it("a", ["x.py", "y.py", "a.py"]), _it("b", ["x.py", "y.py", "b.py"], ["a"])])
    assert ok["verdict"] == "ok" and ok["mode"] == "team"


def test_intake_routes_parallel_replan_and_team():
    disjoint = {"kind": "feature", "confidence": 0.9,
                "subtasks": [_it("a", ["a.py"]), _it("b", ["b.py"])]}
    plan = intake.plan_route(disjoint)
    assert plan["route"] == "parallel"
    assert plan["precomputedPlan"]["subtasks"][1] == {"id": "b", "description": "b", "files": ["b.py"]}
    bad = {"kind": "feature", "confidence": 0.9,
           "subtasks": [_it("a", ["x.py", "y.py"]), _it("b", ["x.py", "y.py"])]}
    plan = intake.plan_route(bad)
    assert plan["route"] == "replan" and "REJECTED" in plan["replanPrompt"]
    coupled = {"kind": "feature", "confidence": 0.9,
               "subtasks": [_it("a", ["reg.py", "a.py"]), _it("b", ["reg.py", "b.py"])]}
    assert intake.plan_route(coupled)["route"] == "team"
    # Forcing single overrides a bad cut (the planner's split is ignored entirely).
    assert intake.plan_route(bad, force_single=True)["route"] == "single"


def test_loop_conditions_read_the_loops_own_iteration_output():
    # Live-found (runs f6015e75, de79d076; reproduced with probe workflows): with a SIMPLE task in a
    # DO_WHILE body, the condition reads neither a bare in-loop task ref (``route__1`` -> null) nor a
    # SET_VARIABLE written in the same iteration (stale). The loop's own output map
    # ``{iteration, "<n>": {<ref>: output}}`` is always current, so conditions read that.
    import json
    import os
    here = os.path.join(os.path.dirname(__file__), "..", "workflows")
    for name, loop_ref, task_ref, field in (("sdlc_intake", "plan_loop", "route", "route"),
                                            ("cc_program", "plan_loop", "program_parse", "route"),
                                            ("cc_program", "rank_loop", "apply_rank", "continue")):
        d = json.load(open(os.path.join(here, f"{name}.json")))

        def find(ts):
            for t in ts:
                if t["taskReferenceName"] == loop_ref:
                    return t
                for sub in [t.get("loopOver", [])] + list(t.get("decisionCases", {}).values()):
                    r = find(sub)
                    if r:
                        return r
        loop = find(d["tasks"])
        cond = loop["loopCondition"]
        assert f"$.{loop_ref}[String($.{loop_ref}['iteration'])]" in cond and f"['{task_ref}']" in cond and f"['{field}']" in cond, name
        assert "workflow.variables" not in json.dumps(loop["inputParameters"]), name


def test_placeholder_and_docs_only_program_plans_are_rejected():
    """The planner sent a one-item plan — description "test", criterion "ok", a documents deliverable — to
    get past the output validator; the gate accepted it (one item routes single, never replan), the
    program built that document and reported itself delivered (run b8dc259b)."""
    ph = [{"id": "design", "description": "test", "deliverable": "docs", "files": ["docs/design/architecture.md"],
           "acceptanceCriteria": ["ok"]}]
    out = dc.assess(ph)
    assert out["verdict"] == "replan" and out["fatal"] is True
    assert any("placeholder description" in r for r in out["reasons"])
    assert any("placeholder acceptance criteria" in r for r in out["reasons"])
    docs_only = [{"id": "design", "description": "write the architecture document", "deliverable": "docs",
                  "files": ["docs/design/architecture.md"], "acceptanceCriteria": ["the layout is fixed"]}]
    assert dc.assess(docs_only)["verdict"] == "ok"  # a documents program is fine on its own...
    strict = dc.assess(docs_only, require_code=True)  # ...but not as a program that builds software
    assert strict["verdict"] == "replan" and strict["fatal"] is True and "deliverable 'code'" in strict["reasons"][0]
    inferred = [{"id": "d", "description": "design notes", "files": ["docs/notes.md"], "acceptanceCriteria": ["notes exist"]}]
    assert dc.assess(inferred, require_code=True)["fatal"] is True  # docs inferred from the files
    with_code = docs_only + [{"id": "resp", "description": "RESP reader", "deliverable": "code", "files": ["resp/reader.go"],
                              "dependsOn": ["design"], "acceptanceCriteria": ["reads a multibulk frame"]}]
    assert dc.assess(with_code, require_code=True)["verdict"] == "ok"
    # A fatal reason rejects a one-item plan too; the program's parse step asks for code.
    plan = intake.plan_route({"kind": "feature", "confidence": 1.0, "subtasks": ph}, force_team=True, require_code=True)
    assert plan["route"] == "replan" and plan["decompositionVerdict"] == "replan" and "REJECTED" in plan["replanPrompt"]
    import json, os
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "workflows", "cc_program.json")))
    def walk(tasks):
        for t in tasks:
            yield t
            for k in ("loopOver", "defaultCase"):
                yield from walk(t.get(k) or [])
            for case in (t.get("decisionCases") or {}).values():
                yield from walk(case)
    parse = [t for t in walk(d["tasks"]) if t.get("taskReferenceName") == "program_parse"][0]
    assert parse["inputParameters"]["requireCode"] is True
