"""Run the REAL jq expressions from security_scan.json so the workflow's
intrusive-filtering and PDF-sanitization logic is covered by tests."""
import json
import os
import shutil
import subprocess

import pytest
from conftest import REPO

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")

WF = os.path.join(REPO, "conductor", "workflows", "security_scan.json")


def _query_expr(ref):
    with open(WF) as fh:
        wf = json.load(fh)
    task = next(t for t in wf["tasks"] if t.get("taskReferenceName") == ref)
    return task["inputParameters"]["queryExpression"]


def _run_jq(expr, data):
    p = subprocess.run(["jq", "-c", expr], input=json.dumps(data),
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


CHECKS = [
    {"id": "P-1", "check_type": "open_redirect", "target": "http://t/r", "param": "to", "intrusive": False},
    {"id": "P-2", "check_type": "sqli", "target": "http://t/s", "param": "q", "intrusive": True},
    {"id": "P-3", "check_type": "xss", "target": "http://t/x", "param": "q", "intrusive": True},
    {"id": "P-4", "check_type": "auth", "target": "http://t/a", "param": "", "intrusive": False},
]
BASE = {"base_url": "http://t", "scope": {"in_scope_hosts": ["t"]}}


def test_build_jobs_non_intrusive_filters_and_prepends_nuclei():
    expr = _query_expr("build_jobs")
    out = _run_jq(expr, {**BASE, "planned_checks": CHECKS, "allow_intrusive": "false"})
    # 2 non-intrusive planned + 1 prepended nuclei job
    assert out["count"] == 3
    types = [v["check"]["check_type"] for v in out["forkTasksInputs"].values()]
    assert "nuclei" in types
    assert "sqli" not in types and "xss" not in types  # intrusive excluded
    assert all(t["name"] == "active_check" for t in out["forkTasks"])
    # fork task refs line up with the input map keys
    assert {t["taskReferenceName"] for t in out["forkTasks"]} == set(out["forkTasksInputs"].keys())


def test_build_jobs_intrusive_includes_all():
    expr = _query_expr("build_jobs")
    out = _run_jq(expr, {**BASE, "planned_checks": CHECKS, "allow_intrusive": "true"})
    assert out["count"] == 5  # 4 planned + nuclei
    types = [v["check"]["check_type"] for v in out["forkTasksInputs"].values()]
    assert "sqli" in types and "xss" in types


def test_build_jobs_empty_still_has_nuclei():
    expr = _query_expr("build_jobs")
    out = _run_jq(expr, {**BASE, "planned_checks": [], "allow_intrusive": "false"})
    assert out["count"] == 1
    assert list(out["forkTasksInputs"].values())[0]["check"]["check_type"] == "nuclei"


def test_merge_surface_seeds_source_routes_and_api():
    expr = _query_expr("merge_surface")
    data = {
        "recon": {"findings": [{"title": "r1"}], "meta": {"server": "nginx"}},
        "crawl": {"findings": [{"title": "c1"}], "surface": {"urls": ["http://t/a"], "forms": [], "endpoints": [], "params": []}, "meta": {}},
        "agent": {"discovered_urls": ["http://t/agent"], "steps": 2},
        "sast": {"findings": [{"title": "s1"}], "routes": [{"path": "/admin", "method": "GET"}]},
        "api": {"findings": [{"title": "a1"}], "endpoints": [{"url": "http://t/api/users", "method": "GET"}], "meta": {"specs": ["/swagger.json"]}},
        "base_url": "http://t",
    }
    out = _run_jq(expr, data)
    titles = [f["title"] for f in out["findings"]]
    assert {"r1", "c1", "s1", "a1"} <= set(titles)          # all sources merged
    assert "http://t/admin" in out["surface"]["urls"]       # source route seeded as URL
    assert "http://t/api/users" in out["surface"]["urls"]   # api endpoint seeded
    assert "http://t/agent" in out["surface"]["urls"]       # agent discovery seeded
    assert out["source_routes"] == 1 and out["api_specs"] == 1


def test_sanitize_md_maps_and_strips_unicode():
    expr = _query_expr("sanitize_md")
    out = _run_jq(expr, {"md": "go A → B, em — dash, ellipsis …, emoji \U0001F600 end"})
    assert isinstance(out, str)
    assert "->" in out          # arrow mapped
    assert "→" not in out  # no raw arrow
    assert "—" not in out  # em dash mapped/stripped
    assert "…" not in out  # ellipsis mapped
    assert "\U0001F600" not in out  # emoji stripped
    assert "go A" in out and "end" in out  # content preserved
