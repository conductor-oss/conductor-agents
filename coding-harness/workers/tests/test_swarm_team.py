"""The design team's guards: one lead owns the seams, designers only where they pay, nobody else edits
the lead's files (swarm/team.py)."""
from __future__ import annotations

import json
import os

import pytest

from common import git
from swarm import team
from swarm.tasks import swarm_dispatch, swarm_formal, swarm_item_prep, swarm_item_release, swarm_round


def _it(iid, *deps, kind="api", owns=None):
    out = {"id": iid, "title": iid, "brief": iid, "kind": kind, "rung": 0, "dependsOn": list(deps)}
    if owns is not None:
        out["owns"] = owns
    return out


def _plan(items):
    return {"items": items, "checks": [], "retireChecks": [], "spec": {"kind": "none", "why": "small"},
            "design": {"docs": [], "why": "none"}, "verification": {"focuses": [], "why": "small"}}


GOOD = [_it("storage-design", kind="design", owns=["design/storage.md"]), _it("heap", "storage-design"),
        _it("index", "storage-design"), _it("api")]


def test_architects_setting():
    assert team.architects_mode("") == "auto" and team.architects_mode("auto") == "auto"
    assert team.architects_mode("1") == 1 and team.architects_mode(0) == 1  # the lead is always there
    assert team.designer_cap("auto") == team.MAX_DESIGNERS and team.designer_cap(1) == 0 and team.designer_cap(3) == 2
    assert team.designer_cap(50) == team.MAX_DESIGNERS
    with pytest.raises(ValueError):
        team.architects_mode("many")


def test_a_valid_design_team_passes():
    assert team.design_problems(_plan(GOOD), "auto") == []
    assert team.design_problems(_plan([_it("api")]), 1) == []  # no design items: nothing to check


def test_the_design_team_rules_go_back_to_the_lead():
    assert "one architect" in team.design_problems(_plan(GOOD), 1)[0]
    two = GOOD + [_it("sql-design", kind="design", owns=["design/sql.md"]), _it("p", "sql-design"), _it("q", "sql-design")]
    assert any("at most 1" in p for p in team.design_problems(_plan(two), 2))
    lonely = [_it("ui-design", kind="design", owns=["design/ui.md"]), _it("page", "ui-design")]
    assert any("1 build item(s) depending" in p for p in team.design_problems(_plan(lonely), "auto"))
    greedy = [_it("d", kind="design", owns=["design/d.md", ".cc/design.md"]), _it("a", "d"), _it("b", "d")]
    assert any("your own files" in p for p in team.design_problems(_plan(greedy), "auto"))
    nodoc = [_it("d", kind="design", owns=["src/stub.ts"]), _it("a", "d"), _it("b", "d")]
    assert any("design/<part>.md" in p for p in team.design_problems(_plan(nodoc), "auto"))
    chained = [_it("d1", kind="design", owns=["design/a.md"]), _it("d2", "d1", kind="design", owns=["design/b.md"]),
               _it("x", "d1"), _it("y", "d2"), _it("z", "d2")]
    assert any("never on each other" in p for p in team.design_problems(_plan(chained), "auto"))


def test_the_plan_gate_sends_a_bad_design_team_back_like_a_bad_spec(tmp_path, fake_task_input):
    work = str(tmp_path / "w")
    git.ensure_ready(work)
    out = swarm_formal(fake_task_input(workdir=work, plan={"structured": _plan(GOOD)}, specs="none", design="auto",
                                       existingSpec="", architects="1")).output_data
    assert out["ok"] is False and "one architect" in out["feedback"] and "THE HARNESS REJECTED" in out["feedback"]
    fine = swarm_formal(fake_task_input(workdir=work, plan={"structured": _plan(GOOD)}, specs="none", design="auto",
                                        existingSpec="", architects="auto")).output_data
    assert fine["ok"] is True and fine["feedback"] == ""


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def test_the_lead_files_are_put_back_unless_handed_over(tmp_path):
    work = str(tmp_path / "w")
    for rel in (".cc/design.md", ".cc/checks/a.mjs", "design/architecture.md", ".cc/checks.json", "src/app.js"):
        _write(work, rel, rel)
    team.snapshot(work, 1)
    _write(work, ".cc/design.md", "an item rewrote the seams")
    _write(work, ".cc/checks/new.mjs", "an item added a check")
    os.remove(os.path.join(work, ".cc/checks/a.mjs"))
    _write(work, "design/architecture.md", "handed to an item")
    _write(work, "src/app.js", "item code")
    out = {x["path"]: x["action"] for x in team.restore(work, 1, ["design/architecture.md"])}
    assert out == {".cc/design.md": "restored", ".cc/checks/new.mjs": "removed", ".cc/checks/a.mjs": "restored"}
    assert open(os.path.join(work, ".cc/design.md")).read() == ".cc/design.md"
    assert open(os.path.join(work, "design/architecture.md")).read() == "handed to an item"  # granted
    assert open(os.path.join(work, "src/app.js")).read() == "item code"  # not the lead's
    assert team.restore(work, 7, []) == []  # no snapshot of that round: nothing touched


def test_dispatch_puts_design_items_first_and_keeps_the_lead_files(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    _write(work, ".cc/design.md", "seams")
    many = [_it(f"a{n}") for n in range(4)] + GOOD
    out = swarm_dispatch(fake_task_input(items=many, round=1, runId="run-team", workdir=work, maxWidth=3)).output_data
    started = [t["taskReferenceName"] for t in out["dynamicTasks"]]
    assert "item_storage-design_r1" in started  # the width cut never drops a design item
    assert os.path.isfile(os.path.join(work, team.SNAPSHOTS, "r1", "manifest.json"))


def test_designers_run_on_the_design_role_inside_what_they_own(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-roles"
    swarm_dispatch(fake_task_input(items=GOOD, round=1, runId=run, workdir=work, maxWidth=10))
    d = swarm_item_prep(fake_task_input(item=GOOD[0], round=1, runId=run, workdir=work)).output_data
    assert d["modelRole"] == "code" and d["writeRoots"] == ["design/storage.md"]
    assert d["templateKey"] == "swarm_design" and "heap" in d["promptContext"]["itemGuide"]
    assert "THIS IS A DESIGN ITEM" in d["reviewContext"]["reviewFocus"]
    assert "WHEN SOURCES DISAGREE" in d["promptContext"]["precedence"]
    b = swarm_item_prep(fake_task_input(item=GOOD[1], round=1, runId=run, workdir=work)).output_data
    assert b["modelRole"] == "code" and b["writeRoots"] == []
    assert "`design/storage.md`" in b["promptContext"]["itemGuide"] and b["reviewContext"]["reviewFocus"] == ""


def test_a_designer_reporting_files_it_does_not_own_is_not_accepted(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-strays"
    swarm_dispatch(fake_task_input(items=GOOD, round=1, runId=run, workdir=work, maxWidth=10))
    out = swarm_item_release(fake_task_input(
        item=GOOD[0], round=1, runId=run, workdir=work, rung=0, reviewRung=0,
        work={"status": "success", "structured": {"summary": "designed", "done": True,
                                                  "files": ["design/storage.md", ".cc/design.md"]}},
        review={"status": "success", "structured": {"accepted": True, "notes": "fine", "fixed": []}})).output_data
    assert out["accepted"] is False and out["dynamicTasks"] == [] and sorted(out["held"]) == ["heap", "index"]
    assert team.outside_owns(GOOD[0], ["design/storage.md", ".cc/design.md"]) == [".cc/design.md"]
    ok = swarm_item_release(fake_task_input(
        item={**GOOD[0], "id": "storage-design"}, round=1, runId=run, workdir=work, rung=0, reviewRung=0,
        work={"status": "success", "structured": {"summary": "designed", "done": True, "files": ["./design/storage.md"]}},
        review={"status": "success", "structured": {"accepted": True, "notes": "fine", "fixed": []}})).output_data
    assert ok["accepted"] is True


def test_fold_puts_back_what_items_changed_and_tells_the_team(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import team_board
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    _write(work, ".cc/checks.json", "[]")
    run = "run-fold"
    swarm_dispatch(fake_task_input(items=[_it("api")], round=1, runId=run, workdir=work, maxWidth=10))
    _write(work, ".cc/checks.json", '[{"id": "weakened"}]')
    out = swarm_round(fake_task_input(runId=run, round=1, workdir=work, repoPath=work)).output_data
    assert json.load(open(os.path.join(work, ".cc/checks.json"))) == []
    assert any(r.get("restoredLeadFiles") for r in out["reports"])
    lessons = team_board.fold_board(run).get("lessons") or []
    assert any("Put back the lead architect's files" in str(x) for x in lessons)


def test_a_rejected_design_goes_back_to_its_architect_at_most_twice(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import team_board
    from swarm.tasks import swarm_design_dispatch, swarm_design_fold
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-rework"
    kick = {"structured": {"team": True, "why": "parts", "parts": [{"id": "core", "title": "Core", "brief": "b"},
                                                                    {"id": "data", "title": "Data", "brief": "b"}]}}
    out = swarm_design_dispatch(fake_task_input(kickoff=kick, round=0, runId=run, workdir=work, maxWidth=10,
                                                architects="auto")).output_data
    assert out["designers"] == ["design-core", "design-data"]
    item = {"id": "design-data", "title": "Design: Data", "brief": "b", "kind": "design", "rung": 0, "dependsOn": [],
            "owns": ["design/data.md"]}

    def release(it):
        return swarm_item_release(fake_task_input(
            item=it, round=0, runId=run, workdir=work, rung=0, reviewRung=0,
            work={"status": "success", "structured": {"summary": "designed", "done": True, "files": ["design/data.md"]}},
            review={"status": "success", "structured": {"accepted": False, "notes": "uses Copy(); keyspace has Clone()",
                                                        "fixed": []}})).output_data

    first = release(item)
    again = first["dynamicInputs"] if "dynamicInputs" in first else first["dynamicTasksInput"]
    (ref, inp), = again.items()
    assert ref.endswith("_rework1") and inp["item"]["rung"] == 1 and "Clone()" in inp["item"]["brief"]
    assert team_board.fold_board(run)["items"]["design-data"]["state"] == team_board.IN_PROGRESS
    second = release(inp["item"])
    (ref2, inp2), = second["dynamicTasksInput"].items()
    assert ref2.endswith("_rework2") and inp2["item"]["brief"].count("YOUR REVIEW DID NOT ACCEPT") == 1
    third = release(inp2["item"])
    assert third["dynamicTasks"] == []  # its reworks are used up: the lead settles it
    fold = swarm_design_fold(fake_task_input(runId=run)).output_data
    assert "STILL NOT ACCEPTED" in fold["text"] and "after 3 attempt(s)" in fold["text"]


def test_each_architect_is_told_who_designs_beside_it(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from swarm.tasks import swarm_design_dispatch
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    run = "run-roster"
    kick = {"structured": {"team": True, "why": "x", "parts": [{"id": "core", "title": "Core", "brief": "protocol"},
                                                               {"id": "data", "title": "Data", "brief": "types"}]}}
    out = swarm_design_dispatch(fake_task_input(kickoff=kick, round=0, runId=run, workdir=work, maxWidth=10,
                                                architects="auto")).output_data
    me = out["dynamicTasksInput"]["item_design-data_r0"]["item"]
    prep = swarm_item_prep(fake_task_input(item=me, round=0, runId=run, workdir=work)).output_data
    roster = prep["promptContext"]["team"]
    assert "design-core" in roster and "NOT YOURS" in roster and "only subsystem" not in roster
    assert me["rung"] == 1  # architects start on the designer rung (Sonnet high), not the cheapest


def test_the_plan_is_assembled_from_the_teams_proposals_and_the_leads_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    from common import blackboard as bb
    from swarm.tasks import REPORT_KIND, _team_reports, assemble_plan
    run = "run-assemble"
    chk = lambda cid, f: {"id": cid, "file": f".cc/checks/{f}.mjs", "command": f"node .cc/checks/{f}.mjs", "scenario": "s", "covers": []}
    bb.post_fact(run, "harness", REPORT_KIND, {"round": 0, "item": "design-core", "accepted": True,
                 "proposedItems": [{"id": "resp", "title": "RESP", "brief": "b", "kind": "api", "rung": 0, "dependsOn": []},
                                   {"id": "server", "title": "Server", "brief": "b", "kind": "api", "rung": 0, "dependsOn": ["resp"]}],
                 "proposedChecks": [chk("ping", "ping")]})
    bb.post_fact(run, "harness", REPORT_KIND, {"round": 0, "item": "design-data", "accepted": True,
                 "proposedItems": [{"id": "server", "title": "Data server", "brief": "b", "kind": "api", "rung": 0, "dependsOn": []}],
                 "proposedChecks": [chk("ping", "ping"), chk("strings", "strings")]})
    reports = _team_reports(run)
    ids = sorted(x["id"] for r in reports for x in r["proposedItems"])
    assert ids == ["core-resp", "core-server", "data-server"]  # two parts' `server` no longer collide
    plan = assemble_plan(reports, {"dropItems": ["data-server"], "dropChecks": [], "decisions": "d",
                                   "items": [{"id": "core-server", "title": "Server", "brief": "bigger", "kind": "api",
                                              "rung": 1, "dependsOn": ["core-resp"]}],
                                   "checks": [], "retireChecks": [], "spec": {"kind": "none", "why": "x"},
                                   "design": {"docs": [], "why": "x"}, "verification": {"focuses": [], "why": "x"}})
    by = {x["id"]: x for x in plan["items"]}
    assert set(by) == {"core-resp", "core-server"} and by["core-server"]["brief"] == "bigger"
    assert by["core-server"]["dependsOn"] == ["core-resp"]
    assert [c["file"] for c in plan["checks"]] == [".cc/checks/ping.mjs", ".cc/checks/strings.mjs"]  # one script, one check


def _cap(work, part, scenarios):
    body = "## Purpose\nP.\n\n## Requirements\n### Requirement: R\nThe system SHALL work.\n\n" + "".join(
        f"#### Scenario: {s}\n- **WHEN** x\n- **THEN** y\n\n" for s in scenarios)
    _write(work, f".cc/openspec/specs/{part}/spec.md", body)


def test_an_architects_spec_is_checked_when_it_finishes_and_sent_back_to_it(tmp_path, monkeypatch, fake_task_input):
    monkeypatch.setenv("SWARM_BLACKBOARD_DIR", str(tmp_path / "board"))
    work = str(tmp_path / "clone")
    git.ensure_ready(work)
    item = team.design_items({"team": True, "parts": [{"id": "keyspace", "title": "K", "brief": "b", "owns": "keys"}]},
                             "auto", spec_kind="openspec")[0]
    good = {"proposedChecks": [{"id": "keyspace-dump", "covers": ["keyspace: DUMP and RESTORE round trip"]}]}
    _cap(work, "keyspace", ["keyspace: DUMP and RESTORE round trip"])
    assert team.lint_part(work, item, good) == []
    _cap(work, "keyspace", ["DUMP and RESTORE round trip", "keyspace: TTL", "keyspace: TTL"])
    problems = " | ".join(team.lint_part(work, item, good))
    assert "does not start with `keyspace: `" in problems and "appears more than once" in problems
    assert "no check of yours proves scenario 'keyspace: TTL'" in problems
    assert "covers 'keyspace: DUMP and RESTORE round trip', which is not one of your scenarios" in problems
    run = "run-lint"
    swarm_dispatch(fake_task_input(items=[item], round=0, runId=run, workdir=work, maxWidth=10))
    out = swarm_item_release(fake_task_input(
        item=item, round=0, runId=run, workdir=work, rung=1, reviewRung=0,
        work={"status": "success", "structured": {"summary": "s", "done": True, "files": ["design/keyspace.md"], **good}},
        review={"status": "success", "structured": {"accepted": True, "notes": "fine", "fixed": []}})).output_data
    assert out["accepted"] is False  # the review accepted it; the harness did not
    (ref, inp), = out["dynamicTasksInput"].items()
    assert ref.endswith("_rework1") and "your spec is not ready" in inp["item"]["brief"]


def test_a_tla_team_gets_its_own_model_architect_and_no_split_part_writes_it():
    items = team.design_items({"team": True, "parts": [{"id": "core", "title": "C", "brief": "b", "owns": "x"},
                                                       {"id": "spec", "title": "S", "brief": "b", "owns": "y"}]},
                              "auto", spec_kind="tla")
    ids = [x["id"] for x in items]
    assert ids == ["design-core", "design-spec"] and team.is_spec_item(items[-1])
    assert items[-1]["owns"] == [".cc/spec"] and items[-1]["rung"] == team.DESIGNER_RUNG + 1
    assert "IS YOUR SUBSYSTEM" in team.spec_task(items[-1]) and "own architect" in team.spec_task(items[0])


def test_a_design_item_reporting_its_own_file_by_full_path_is_not_a_stray(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    assert team.outside_owns(GOOD[0], [str(work / "design" / "storage.md")], str(work)) == []
    assert team.outside_owns(GOOD[0], [str(work / ".cc" / "design.md")], str(work)) == [".cc/design.md"]


def test_a_new_compiled_project_needs_exactly_one_core_item(tmp_path):
    from common import stacks
    work = str(tmp_path / "clone")
    os.makedirs(work)
    assert stacks.needs_core(work, ["go", "web"]) == ["go"]
    items = [_it("server"), _it("lists")]
    assert "no item is marked `core: true`" in team.core_problems(items, ["go"])[0]
    assert team.core_problems([{**items[0], "core": True}, items[1]], ["go"]) == []
    assert "mark exactly one" in team.core_problems([{**x, "core": True} for x in items], ["go"])[0]
    _write(work, "go.mod", "module x\n")  # a continuation: the module is there already
    assert stacks.needs_core(work, ["go"]) == [] and team.core_problems(items, []) == []
    assert stacks.needs_core(work, ["python"]) == []
