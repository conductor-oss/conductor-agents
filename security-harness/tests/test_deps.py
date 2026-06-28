"""Dependency extraction + OSV CVE lookup (ROADMAP E4 supply chain)."""
from common import deps


def test_parse_gradle_with_property_versions(tmp_path):
    (tmp_path / "gradle.properties").write_text("jacksonVersion=2.9.8\n")
    (tmp_path / "build.gradle").write_text(
        'dependencies {\n'
        '  implementation "com.fasterxml.jackson.core:jackson-databind:$jacksonVersion"\n'
        '  implementation "org.apache.commons:commons-text:1.9"\n'
        '  testImplementation "junit:junit:4.13"\n'
        '}\n')
    got = {f"{d['name']}@{d['version']}": d for d in deps.parse_source(str(tmp_path))}
    assert "com.fasterxml.jackson.core:jackson-databind@2.9.8" in got   # $var resolved
    assert "org.apache.commons:commons-text@1.9" in got
    assert all(d["ecosystem"] == "Maven" for d in got.values())


def test_parse_gradle_groovy_versions_map(tmp_path):
    # Conductor-style: versions declared in a `versions = [ key : 'v' ]` map + ${versions.key} refs
    (tmp_path / "build.gradle").write_text(
        "ext {\n  versions = [\n"
        "    postgres        : '42.7.11',\n"
        "    revNettyDeps    : '4.1.133.Final',\n"
        "  ]\n}\n"
        'dependencies {\n'
        '  implementation "org.postgresql:postgresql:${versions.postgres}"\n'
        '  implementation "io.netty:netty-handler:${versions.revNettyDeps}"\n'
        '}\n')
    got = {d["name"]: d["version"] for d in deps.parse_source(str(tmp_path))}
    assert got["org.postgresql:postgresql"] == "42.7.11"      # map-resolved, not '?'
    assert got["io.netty:netty-handler"] == "4.1.133.Final"


def test_query_osv_ranks_version_matched_first(monkeypatch):
    def fetch(eco, name, ver):
        return [{"id": "CVE-1", "severity": "critical" if not ver else "low", "summary": "x"}]
    depslist = [
        {"ecosystem": "Maven", "name": "x:unknown", "version": "", "scope": "compile"},    # version-unknown 'critical'
        {"ecosystem": "Maven", "name": "x:matched", "version": "1.0", "scope": "compile"},  # version-matched 'low'
    ]
    out = deps.query_osv(depslist, fetch)
    assert out[0]["dependency"].startswith("x:matched")   # matched ranks above version-unknown 'critical'
    assert out[0]["version_known"] is True


def test_parse_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies":{"express":"^4.17.1"},"devDependencies":{"mocha":"9.0.0"}}')
    got = {d["name"]: d for d in deps.parse_source(str(tmp_path))}
    assert got["express"]["version"] == "4.17.1" and got["express"]["ecosystem"] == "npm"
    assert got["mocha"]["scope"] == "test"


def test_parse_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("Django==3.2.0\nrequests==2.20.0\n")
    got = {d["name"]: d["version"] for d in deps.parse_source(str(tmp_path))}
    assert got["Django"] == "3.2.0" and got["requests"] == "2.20.0"


def test_infer_stack_from_fingerprint():
    stack = deps.infer_stack({"tech": ["Spring Boot", "nginx/1.18.0"]}, {})
    names = {s["name"]: s for s in stack}
    assert any("spring" in n for n in names)
    nginx = next((s for s in stack if s["name"] == "nginx"), None)
    assert nginx and nginx["version"] == "1.18.0"


def test_query_osv_shapes_and_sorts(monkeypatch):
    def fake_fetch(eco, name, ver):
        if "jackson" in name:
            return [{"id": "CVE-2020-1", "severity": "high", "summary": "rce"},
                    {"id": "CVE-2020-2", "severity": "medium", "summary": "dos"}]
        if "safe" in name:
            return []
        return [{"id": "CVE-X", "severity": "low", "summary": "minor"}]
    depslist = [
        {"ecosystem": "Maven", "name": "x:jackson", "version": "2.9.8", "scope": "compile"},
        {"ecosystem": "Maven", "name": "x:safe", "version": "1.0", "scope": "compile"},
        {"ecosystem": "Maven", "name": "x:minor", "version": "1.0", "scope": "test"},
    ]
    out = deps.query_osv(depslist, fake_fetch)
    assert len(out) == 2                       # 'safe' (no vulns) dropped
    assert out[0]["dependency"].startswith("x:jackson")   # high sorted first
    assert out[0]["cve_count"] == 2
