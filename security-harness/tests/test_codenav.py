"""The codenav tools are read-only and path-jailed to source_path — test the jail."""
import os

from codenav import tasks


def test_safe_allows_paths_inside_root(tmp_path):
    root = os.path.realpath(str(tmp_path))
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.py").write_text("x = 1\n")
    assert tasks._safe(root, "a/f.py") == os.path.join(root, "a", "f.py")
    assert tasks._safe(root, "") == root
    assert tasks._safe(root, "/a/f.py") == os.path.join(root, "a", "f.py")  # leading slash stripped


def test_safe_rejects_parent_escape(tmp_path):
    root = os.path.realpath(str(tmp_path / "proj"))
    (tmp_path / "proj").mkdir()
    (tmp_path / "secret.txt").write_text("nope\n")
    assert tasks._safe(root, "../secret.txt") is None
    assert tasks._safe(root, "../../etc/passwd") is None
    assert tasks._safe(root, "a/../../secret.txt") is None


def test_safe_rejects_absolute_outside(tmp_path):
    root = os.path.realpath(str(tmp_path / "proj"))
    (tmp_path / "proj").mkdir()
    # an absolute-looking rel is joined under root (leading slash stripped), so an
    # absolute escape must resolve back outside -> rejected
    assert tasks._safe(root, "../../../../../../etc/passwd") is None


def test_safe_no_root_returns_none():
    assert tasks._safe("", "anything") is None


def test_read_refuses_escape(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "secret.txt").write_text("TOP SECRET\n")

    class T:
        input_data = {"source_path": str(tmp_path / "proj"), "path": "../secret.txt"}

    out = tasks.codenav_read(T())
    assert out["ok"] is False
    assert "not allowed" in out["error"] or "outside" in out["error"]


def test_read_returns_file_inside(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "app.py").write_text("line1\nline2\nline3\n")

    class T:
        input_data = {"source_path": str(tmp_path / "proj"), "path": "app.py"}

    out = tasks.codenav_read(T())
    assert out["ok"] is True
    assert out["total_lines"] == 3
    assert "1: line1" in out["content"]


def test_build_hunt_leads_shapes():
    class T:
        input_data = {
            "routes": [
                {"path": "/a", "method": "GET", "file": "api/x.py"},
                {"path": "/b", "method": "POST", "file": "api/x.py"},
            ],
            "cve_leads": [{
                "dependency": "log4j-core", "version_known": True, "kev": True,
                "priority_score": 9,
                "top_cves": [{"id": "CVE-2021-44228", "severity": "CRITICAL", "summary": "JNDI lookup RCE"}],
            }],
            "catalog_objectives": [
                {"id": "INFRA-RCE-INJECTION", "class": "infra", "objective": "exec", "how_to_test": "..."},
                {"id": "INFRA-SSRF", "class": "infra", "objective": "ssrf", "how_to_test": "..."},
            ],
            "max_leads": 12,
        }

    out = tasks.build_hunt_leads(T())
    kinds = {ld["kind"] for ld in out["leads"]}
    assert {"entrypoint_cluster", "dependency_cve", "objective_sweep"} <= kinds
    dep = [ld for ld in out["leads"] if ld["kind"] == "dependency_cve"][0]
    assert dep["dependency"] == "log4j-core"
    assert out["counts"]["total"] == len(out["leads"])
    # empty inputs must not throw and yield no leads
    class E:
        input_data = {}
    assert tasks.build_hunt_leads(E())["leads"] == []
