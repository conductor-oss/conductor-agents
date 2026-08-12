from __future__ import annotations

import json
from pathlib import Path

import pytest

from common import check_execution, git, polling, verification
from verification import tasks as verification_tasks


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_verification_worker_has_no_timeouts():
    taskdefs = Path(__file__).resolve().parents[1] / "workflows" / "taskdefs"
    for name in ("verification_discover", "test_discover", "test_run", "verification_health"):
        definition = json.loads((taskdefs / f"{name}.json").read_text())

        assert definition["pollTimeoutSeconds"] == 0
        assert definition["responseTimeoutSeconds"] == 0
        assert definition["timeoutSeconds"] == 0
        assert definition["timeoutPolicy"] == "ALERT_ONLY"


def test_workers_enable_lease_extension_for_server_default_response_windows():
    runner = Path(__file__).resolve().parents[1] / "run_workers.sh"
    assert "CONDUCTOR_WORKER_ALL_LEASE_EXTEND_ENABLED" in runner.read_text()
    assert "terminate_descendants" in runner.read_text()
    assert "terminate_generation" in runner.read_text()
    assert 'kill -TERM -- "-$leader_pid"' in runner.read_text()
    assert "pgrep -P" in runner.read_text()
    assert "WORKER_MODULES_WAS_SET" in runner.read_text()
    assert "REQUESTED_WORKER_MODULES" in runner.read_text()


def test_worker_main_owns_a_generation_process_group():
    worker_main = Path(__file__).resolve().parents[1] / "main.py"
    text = worker_main.read_text()

    assert "os.getpgrp() != os.getpid()" in text
    assert "os.setpgrp()" in text


def test_verification_health_reports_all_runtime_families(monkeypatch):
    class Task:
        input_data = {}

    report = {
        "healthy": True,
        "families": {
            name: {"available": True}
            for name in ("java", "go", "python", "ruby", "typescript")
        },
        "inheritedCapabilities": {},
        "isolatedCapabilities": {},
        "idleSleepInhibitionActive": True,
    }
    monkeypatch.setattr(verification_tasks.check_execution,
                        "runtime_health_report", lambda: report)
    monkeypatch.setattr(verification_tasks.polling, "registered_worker_guard_report", lambda: {
        "healthy": True, "registeredWorkers": 3, "guardedWorkers": 3, "workers": []
    })
    monkeypatch.setattr(verification_tasks, "ok", lambda _task, output, _logs: output)
    result = verification_tasks.verification_health(Task())

    assert result["healthy"] is True
    assert set(result["families"]) == {"java", "go", "python", "ruby", "typescript"}


def _fake_jdk(root: Path, *, exit_code: int = 0, stderr: str = "") -> Path:
    home = root / "jdk"
    java = home / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text(f"#!/bin/sh\necho {stderr!r} >&2\nexit {exit_code}\n")
    java.chmod(java.stat().st_mode | 0o111)
    return home


def test_unpinned_gradle_check_preserves_valid_worker_java_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = _fake_jdk(tmp_path)
    monkeypatch.setenv("JAVA_HOME", str(home))
    wrapper = tmp_path / "gradlew"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)

    env = verification._isolated_environment(str(tmp_path), verification._java_home(None))

    assert env["JAVA_HOME"] == str(home)
    assert env["PATH"].startswith(f"{home}/bin:")
    assert verification._runtime_block_reason(["./gradlew", "test"], env, str(tmp_path)) is None


def test_unpinned_java_discovery_works_without_an_interactive_java_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = _fake_jdk(tmp_path)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(verification, "_candidate_jdk_homes", lambda _version: [str(home)])

    assert verification._java_home(None) == str(home)


def test_gradle_preflight_rejects_a_java_launcher_that_cannot_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = _fake_jdk(tmp_path, exit_code=1, stderr="runtime unavailable")
    monkeypatch.setenv("JAVA_HOME", str(home))
    wrapper = tmp_path / "gradlew"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)

    env = verification._isolated_environment(str(tmp_path), verification._java_home(None))
    reason = verification._runtime_block_reason(["./gradlew", "test"], env, str(tmp_path))

    assert reason is not None
    assert "could not start" in reason


def test_worker_launcher_preflights_github_auth_for_github_backed_modules():
    runner = Path(__file__).resolve().parents[1] / "run_workers.sh"
    text = runner.read_text()

    assert "gh auth status" in text
    assert "GitHub authentication unavailable" in text


def test_harness_definitions_do_not_add_deadlines():
    workflows = Path(__file__).resolve().parents[1] / "workflows"
    for definition_path in [*sorted((workflows / "taskdefs").glob("*.json")), *sorted(workflows.glob("*.json"))]:
        definition = json.loads(definition_path.read_text())
        assert definition.get("timeoutSeconds") == 0, definition_path.name
        assert definition.get("timeoutPolicy") == "ALERT_ONLY", definition_path.name
        if definition_path.parent.name == "taskdefs":
            assert definition.get("responseTimeoutSeconds") == 0, definition_path.name
            assert definition.get("pollTimeoutSeconds") == 0, definition_path.name


def test_remediation_rediscovers_commands_for_every_candidate_iteration():
    workflow_path = Path(__file__).resolve().parents[1] / "workflows" / "test_cycle.json"
    workflow = json.loads(workflow_path.read_text())
    loop = next(
        task for task in _walk(workflow)
        if task.get("taskReferenceName") == "test_fix_loop"
    )
    discovery = next(task for task in loop["loopOver"] if task["taskReferenceName"] == "discover")

    # Rediscovery per iteration is what keeps the plan bound to the commit that
    # is actually being tested, not the one the loop started with.
    assert discovery["name"] == "test_discover"
    assert discovery["inputParameters"]["candidateCommit"] == \
        "${repair_result.output.candidate}"
    verify = next(task for task in loop["loopOver"] if task["taskReferenceName"] == "verify")
    assert verify["inputParameters"]["commands"] == "${verification_plan.output.commands}"
    assert verify["inputParameters"]["discoveryOutcome"] == \
        "${verification_plan.output.discoveryOutcome}"
    plan = next(task for task in loop["loopOver"]
                if task["taskReferenceName"] == "verification_plan")
    assert plan["name"] == "test_plan_resolve"
    assert plan["inputParameters"]["discovered"] == "${discover.output.candidates}"
    # Carried obligations reach the resolver directly now; the separate jq that
    # pre-normalized them is gone.
    assert plan["inputParameters"]["priorVerification"] == "${workflow.input.priorVerification}"


def test_every_workflow_uses_current_signalable_checkpoints():
    workflows = Path(__file__).resolve().parents[1] / "workflows"
    for workflow_path in sorted(workflows.glob("*.json")):
        workflow = json.loads(workflow_path.read_text())
        def walk(task):
            if not isinstance(task, dict):
                return
            assert task.get("type") != "HUMAN", workflow_path.name
            for child in task.values():
                if isinstance(child, dict):
                    walk(child)
                elif isinstance(child, list):
                    for item in child:
                        walk(item)

        for task in workflow.get("tasks", []):
            walk(task)


def test_every_workflow_has_unique_task_references_and_bounded_explicit_loops():
    """Prevent a definition edit from silently creating an invalid/stuck path."""
    workflows = Path(__file__).resolve().parents[1] / "workflows"
    for workflow_path in sorted(workflows.glob("*.json")):
        workflow = json.loads(workflow_path.read_text())
        references: list[str] = []

        def walk(value):
            if isinstance(value, dict):
                reference = value.get("taskReferenceName")
                if reference:
                    references.append(reference)
                if value.get("type") == "DO_WHILE":
                    assert value.get("evaluatorType") == "graaljs", workflow_path.name
                    assert "iteration" in str(value.get("loopCondition") or ""), workflow_path.name
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(workflow.get("tasks", []))
        assert len(references) == len(set(references)), workflow_path.name


def _commit(repo: Path, message: str) -> str:
    git.git(str(repo), "add", "--", ".")
    git.git(str(repo), "commit", "-m", message)
    return git.head(str(repo))


def _focused_gradle_command() -> list[dict]:
    return [{"argv": ["./gradlew", "test", "--tests", "ExampleTest"],
             "source": "build-metadata:gradle:root-exact-tests", "kind": "changed-scope",
             "scope": "focused", "affectedUnit": ":", "coveredPaths": ["ExampleTest.java"]}]


def test_discovery_prefers_documented_lightweight_gradle_test(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Run `./gradlew test` before submitting.\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    git.ensure_ready(str(tmp_path))

    found = verification.discover_commands(str(tmp_path))

    assert found["selection"] == "documented-guide"
    assert found["candidates"] == [{"argv": ["./gradlew", "test"],
                                     "source": "AGENTS.md", "kind": "documented"}]


def test_discovery_uses_changed_gradle_module_not_root_build(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Run `./gradlew build` before submitting.\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "settings.gradle").write_text("include ':core'\n")
    (tmp_path / "core" / "src").mkdir(parents=True)
    (tmp_path / "core" / "build.gradle").write_text("")
    git.ensure_ready(str(tmp_path))
    (tmp_path / "core" / "src" / "Feature.java").write_text("class Feature {}\n")
    candidate = _commit(tmp_path, "change core")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["selection"] == "changed-scope"
    assert found["changedPaths"] == ["core/src/Feature.java"]
    assert found["candidates"][0]["argv"] == ["./gradlew", ":core:test"]
    assert found["candidates"][0]["scope"] == "affected"


def test_gradle_include_without_colon_maps_exact_changed_test(tmp_path: Path):
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "settings.gradle").write_text("include 'agentspan'\n")
    test_file = tmp_path / "agentspan/src/test/java/io/orkes/AgentServiceTest.java"
    test_file.parent.mkdir(parents=True)
    (tmp_path / "agentspan/build.gradle").write_text("")
    test_file.write_text("class AgentServiceTest {}\n")
    git.ensure_ready(str(tmp_path))
    test_file.write_text("class AgentServiceTest { int changed; }\n")
    candidate = _commit(tmp_path, "change exact test")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["coverage"] == "focused"
    assert found["candidates"][0]["argv"] == [
        "./gradlew", ":agentspan:test", "--tests", "io.orkes.AgentServiceTest",
    ]


def test_gradle_adapter_applies_root_child_project_renames(tmp_path: Path):
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "settings.gradle").write_text(
        "include 'agentspan'\nrootProject.children.each {it.name=\"conductor-${it.name}\"}\n"
    )
    source = tmp_path / "agentspan/src/main/java/io/orkes/AgentService.java"
    source.parent.mkdir(parents=True)
    (tmp_path / "agentspan/build.gradle").write_text("")
    source.write_text("class AgentService {}\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("class AgentService { int changed; }\n")
    candidate = _commit(tmp_path, "change renamed project")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["candidates"][0]["argv"] == ["./gradlew", ":conductor-agentspan:test"]


def test_maven_maps_changed_module_and_exact_test(tmp_path: Path):
    (tmp_path / "pom.xml").write_text("<project><modules><module>service</module></modules></project>")
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    test_file = tmp_path / "service/src/test/java/io/acme/ServiceTest.java"
    test_file.parent.mkdir(parents=True)
    (tmp_path / "service/pom.xml").write_text("<project/>")
    test_file.write_text("class ServiceTest {}\n")
    git.ensure_ready(str(tmp_path))
    test_file.write_text("class ServiceTest { int changed; }\n")
    candidate = _commit(tmp_path, "change maven test")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["candidates"][0]["argv"] == [
        "./mvnw", "-pl", "service", "-am", "-Dtest=ServiceTest", "test",
    ]


def test_javascript_maps_changed_workspace_without_root_test(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"workspaces":["packages/*"]}')
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    source = tmp_path / "packages/api/src/index.ts"
    source.parent.mkdir(parents=True)
    (tmp_path / "packages/api/package.json").write_text('{"name":"@acme/api","scripts":{"test":"vitest"}}')
    source.write_text("export const value = 1;\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("export const value = 2;\n")
    candidate = _commit(tmp_path, "change workspace")

    found = verification.discover_commands(str(tmp_path), candidate)

    # The install must come first. verify_candidate runs in a fresh no-local
    # clone that has no node_modules, so this plan without its prepare step
    # would have died with "vitest: command not found" rather than producing a
    # test result at all.
    assert [command["argv"] for command in found["candidates"]] == [
        ["pnpm", "install", "--frozen-lockfile"],
        ["pnpm", "--filter", "@acme/api", "test"],
    ]
    assert found["candidates"][0]["phase"] == "prepare"


def test_python_maps_source_to_matching_test_file(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    source = tmp_path / "src/widget.py"
    test_file = tmp_path / "tests/test_widget.py"
    source.parent.mkdir()
    test_file.parent.mkdir()
    source.write_text("value = 1\n")
    test_file.write_text("def test_widget(): pass\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("value = 2\n")
    candidate = _commit(tmp_path, "change python source")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["candidates"][0]["argv"] == ["pytest", "tests/test_widget.py"]


def test_python_needs_no_packaging_metadata_when_a_test_file_already_proves_it(tmp_path: Path):
    # A bare stdlib module plus a test_*.py file is a fully valid, idiomatic
    # Python project -- unlike a Gemfile or package.json, pyproject.toml is
    # optional for pytest to run. A freshly bootstrapped repo with neither
    # pyproject.toml, pytest.ini, tox.ini, nor setup.cfg must still be
    # verifiable once it has delivered its first module and test.
    assert not any((tmp_path / name).is_file()
                   for name in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"))
    source = tmp_path / "calculator.py"
    test_file = tmp_path / "test_calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n")
    test_file.write_text("from calculator import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n")
    candidate = _commit(tmp_path, "add subtract")

    targeted = verification.discover_commands(str(tmp_path), candidate, mode="targeted")
    assert targeted["candidates"][0]["argv"] == ["pytest", "test_calculator.py"]

    full = verification.discover_commands(str(tmp_path), candidate, mode="full")
    assert full["candidates"][0]["argv"] == ["pytest"]
    assert full["candidates"][0]["source"] == "build-system:pytest"


def test_python_with_no_metadata_and_no_test_file_still_yields_nothing(tmp_path: Path):
    # The broadened detection must not turn every repository into a false
    # positive: a bare .py file with no test-shaped file anywhere is still no
    # evidence of a Python test suite.
    (tmp_path / "script.py").write_text("VALUE = 1\n")
    git.ensure_ready(str(tmp_path))
    (tmp_path / "script.py").write_text("VALUE = 2\n")
    candidate = _commit(tmp_path, "change script")

    with pytest.raises(verification.VerificationBlocked, match="repository-wide fallback is prohibited") as excinfo:
        verification.discover_commands(str(tmp_path), candidate, mode="targeted")
    # allowAgentAuthoredTests' shape check needs the real changed paths even on
    # this exact block -- it is the one outcome that gates authoring a test.
    assert excinfo.value.changed_paths == ["script.py"]
    with pytest.raises(verification.VerificationBlocked, match="no supported repository-wide"):
        verification.discover_commands(str(tmp_path), mode="full")


def test_discover_worker_reports_changed_paths_on_a_configuration_block(fake_task_input, tmp_path: Path):
    (tmp_path / "script.py").write_text("VALUE = 1\n")
    git.ensure_ready(str(tmp_path))
    (tmp_path / "script.py").write_text("VALUE = 2\n")
    candidate = _commit(tmp_path, "change script")

    result = verification_tasks.test_discover(fake_task_input(
        repoPath=str(tmp_path), candidateCommit=candidate, testMode="targeted"))

    assert result.output_data["verificationState"] == "blocked"
    assert result.output_data["executionOutcome"] == "configuration_blocked"
    # This is the field allowAgentAuthoredTests' shape check reads to confirm
    # an authored test references the real change -- losing it here would
    # silently reject every authored test regardless of its content.
    assert result.output_data["changedPaths"] == ["script.py"]


def test_go_and_cargo_map_changed_packages(tmp_path: Path):
    go_root = tmp_path / "go-repo"
    go_root.mkdir()
    (go_root / "go.mod").write_text("module example.com/app\n\ngo 1.23\n")
    go_file = go_root / "internal/api/api.go"
    go_file.parent.mkdir(parents=True)
    go_file.write_text("package api\n")
    git.ensure_ready(str(go_root))
    go_file.write_text("package api\n// changed\n")
    go_candidate = _commit(go_root, "change go package")
    assert verification.discover_commands(str(go_root), go_candidate)["candidates"][0]["argv"] == [
        "go", "test", "./internal/api",
    ]

    cargo_root = tmp_path / "cargo-repo"
    cargo_root.mkdir()
    (cargo_root / "Cargo.toml").write_text('[workspace]\nmembers=["crates/core"]\n')
    rust_file = cargo_root / "crates/core/src/lib.rs"
    rust_file.parent.mkdir(parents=True)
    (cargo_root / "crates/core/Cargo.toml").write_text('[package]\nname="core-lib"\nversion="0.1.0"\n')
    rust_file.write_text("pub fn value() {}\n")
    git.ensure_ready(str(cargo_root))
    rust_file.write_text("pub fn value() { /* changed */ }\n")
    cargo_candidate = _commit(cargo_root, "change cargo package")
    assert verification.discover_commands(str(cargo_root), cargo_candidate)["candidates"][0]["argv"] == [
        "cargo", "test", "-p", "core-lib",
    ]


def test_cmake_maps_changed_library_to_linked_ctest_target(tmp_path: Path):
    (tmp_path / "CMakeLists.txt").write_text(
        "add_library(core src/core.cpp)\n"
        "add_executable(core_test tests/core_test.cpp)\n"
        "target_link_libraries(core_test PRIVATE core)\n"
        "add_test(NAME core-unit COMMAND core_test)\n"
    )
    source = tmp_path / "src/core.cpp"
    test_source = tmp_path / "tests/core_test.cpp"
    source.parent.mkdir()
    test_source.parent.mkdir()
    source.write_text("int value() { return 1; }\n")
    test_source.write_text("int main() { return 0; }\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("int value() { return 2; }\n")
    candidate = _commit(tmp_path, "change cpp library")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert [item["argv"] for item in found["candidates"]] == [
        ["cmake", "-S", ".", "-B", ".conductor-verify-build"],
        ["cmake", "--build", ".conductor-verify-build", "--target", "core_test"],
        ["ctest", "--test-dir", ".conductor-verify-build", "-R", "^core\\-unit$", "--output-on-failure"],
    ]


def test_swiftpm_maps_changed_source_target_to_dependent_test_target(tmp_path: Path):
    (tmp_path / "Package.swift").write_text(
        '.target(name: "Core"),\n.testTarget(name: "CoreTests", dependencies: ["Core"]),\n'
    )
    source = tmp_path / "Sources/Core/Value.swift"
    source.parent.mkdir(parents=True)
    source.write_text("public let value = 1\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("public let value = 2\n")
    candidate = _commit(tmp_path, "change swift target")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["candidates"][0]["argv"] == ["swift", "test", "--filter", "CoreTests"]


def test_language_neutral_scope_config_selects_only_matching_changed_paths(tmp_path: Path):
    config = tmp_path / ".conductor-code/verification.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "version": 1,
        "changedScopeRules": [{
            "paths": ["native/core/**"], "affectedUnit": "native-core", "scope": "focused",
            "scopeToken": "native-core-test", "commands": [["ninja", "native-core-test"]],
        }],
    }))
    source = tmp_path / "native/core/value.custom"
    source.parent.mkdir(parents=True)
    source.write_text("one\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("two\n")
    candidate = _commit(tmp_path, "change custom language")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["candidates"][0]["affectedUnit"] == "native-core"
    assert found["candidates"][0]["coveredPaths"] == ["native/core/value.custom"]
    assert found["candidates"][0]["argv"] == ["ninja", "native-core-test"]


@pytest.mark.parametrize("argv", [["bash", "-lc", "test"], ["python3", "-c", "print(1)"],
                                  ["ninja", "test", "&&", "echo", "oops"]])
def test_language_neutral_scope_config_still_forbids_interpreter_and_shell_escape(argv):
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_configured_argv(argv)


def test_discovery_uses_changed_gradle_module_for_a_merge_candidate(tmp_path: Path):
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "settings.gradle").write_text("include ':core'\n")
    (tmp_path / "core" / "src").mkdir(parents=True)
    (tmp_path / "core" / "build.gradle").write_text("")
    git.ensure_ready(str(tmp_path))
    base_branch = git.git(str(tmp_path), "branch", "--show-current").stdout.strip()
    git.git(str(tmp_path), "checkout", "-b", "parallel-slice")
    (tmp_path / "core" / "src" / "Feature.java").write_text("class Feature {}\n")
    _commit(tmp_path, "parallel change")
    git.git(str(tmp_path), "checkout", base_branch)
    git.git(str(tmp_path), "merge", "--no-ff", "parallel-slice", "-m", "merge parallel slice")
    candidate = git.head(str(tmp_path))

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["changedPaths"] == ["core/src/Feature.java"]
    assert found["candidates"][0]["argv"] == ["./gradlew", ":core:test"]


def test_discovery_never_uses_documented_root_gradle_build_for_a_candidate(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Run `./gradlew build` before submitting.\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    git.ensure_ready(str(tmp_path))
    (tmp_path / "Root.java").write_text("class Root {}\n")
    candidate = _commit(tmp_path, "change root")

    with pytest.raises(verification.VerificationBlocked, match="repository-wide fallback is prohibited"):
        verification.discover_commands(str(tmp_path), candidate)


def test_candidate_discovery_uses_build_metadata_not_prose_guidance(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Run `./gradlew test`.\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "module" / "src").mkdir(parents=True)
    (tmp_path / "module" / "AGENTS.md").write_text("Module-specific guidance.\n")
    (tmp_path / "settings.gradle").write_text("include 'module'\n")
    (tmp_path / "module" / "build.gradle").write_text("")
    git.ensure_ready(str(tmp_path))
    (tmp_path / "module" / "src" / "Feature.java").write_text("class Feature {}\n")
    candidate = _commit(tmp_path, "change module")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["sourcePaths"] == ["build-metadata:gradle:module"]


def test_discovery_does_not_trust_guidance_changed_by_the_candidate(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Run `make deploy`.\n")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"true"}}\n')
    git.ensure_ready(str(tmp_path))
    (tmp_path / "AGENTS.md").write_text("Run `make test`.\n")
    candidate = _commit(tmp_path, "candidate changes its own guidance")

    found = verification.discover_commands(str(tmp_path), candidate)

    assert found["selection"] == "no-tests-required"
    assert found["sourcePaths"] == []
    assert found["candidates"] == []


def test_discovery_does_not_read_github_workflow_commands(tmp_path: Path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "test.yml").write_text("steps:\n  - run: ./gradlew test\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    git.ensure_ready(str(tmp_path))

    found = verification.discover_commands(str(tmp_path))

    assert found["selection"] == "build-system-inference"
    assert found["candidates"][0]["argv"] == ["./gradlew", "test"]


def test_verification_discover_only_validates_the_parallel_plan(fake_task_input, tmp_path):
    result = verification_tasks.verification_discover(fake_task_input(
        repoPath=str(tmp_path),
        subtasks=[
            {"id": "api", "description": "Implement API", "files": ["src/api.py"],
             "testCmd": "this field must be ignored"},
            {"id": "docs", "description": "Document API", "files": ["docs/api.md"]},
        ],
    ))

    assert result.output_data == {
        "valid": True,
        "planningState": "valid",
        "issues": [],
        "subtasks": [
            {"id": "api", "description": "Implement API", "files": ["src/api.py"]},
            {"id": "docs", "description": "Document API", "files": ["docs/api.md"]},
        ],
    }


def test_verification_discover_returns_replan_feedback_without_running_commands(
        monkeypatch, fake_task_input, tmp_path):
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("plan validation must not discover or run tests")

    monkeypatch.setattr(verification, "discover_commands", must_not_run)
    monkeypatch.setattr(verification, "verify_candidate", must_not_run)
    result = verification_tasks.verification_discover(fake_task_input(
        repoPath=str(tmp_path),
        subtasks=[
            {"id": "one", "description": "One", "files": ["same.py"]},
            {"id": "two", "description": "Two", "files": ["same.py"]},
        ],
    ))

    assert result.output_data["valid"] is False
    assert result.output_data["planningState"] == "needs_replan"
    assert result.output_data["issues"][0]["code"] == "overlapping_file"


def test_verification_worker_reports_unexpected_execution_errors_as_blocked(monkeypatch, fake_task_input):
    def boom(*_args, **_kwargs):
        raise RuntimeError("temporary worktree I/O failure")

    monkeypatch.setattr(verification, "verify_candidate", boom)
    result = verification_tasks.test_run(
        fake_task_input(repoPath="/tmp/repo", candidateCommit="abc123", commands=[["pytest"]])
    )

    assert result.output_data["verificationState"] == "blocked"
    assert result.output_data["candidateCommit"] == "abc123"


class _FakeProgressReporter:
    """Records lifecycle calls without any real thread/HTTP work."""
    instances: list["_FakeProgressReporter"] = []

    def __init__(self, task, heartbeat_s=10.0):
        self.task = task
        self.heartbeat_s = heartbeat_s
        self.started = False
        self.stopped = False
        self.updates: list[dict] = []
        _FakeProgressReporter.instances.append(self)

    def start(self):
        self.started = True
        return self

    def update(self, output):
        self.updates.append(output)

    def stop(self):
        self.stopped = True


def test_test_run_worker_wires_a_heartbeat_reporter_through_verify_candidate(monkeypatch, fake_task_input):
    # A bare full-suite pytest run has taken over an hour as a single blocking
    # call on this exact task type; the worker must push periodic IN_PROGRESS
    # updates the same way coding_agent does, not look frozen for the duration.
    _FakeProgressReporter.instances = []
    captured = {}

    def fake_verify_candidate(*_args, **kwargs):
        captured["on_progress"] = kwargs.get("on_progress")
        return {"verificationState": "passed", "executionOutcome": "passed",
                "candidateCommit": "abc123", "commands": []}

    monkeypatch.setattr(verification, "verify_candidate", fake_verify_candidate)
    monkeypatch.setattr(verification_tasks, "ProgressReporter", _FakeProgressReporter)

    result = verification_tasks.test_run(fake_task_input(
        repoPath="/tmp/repo", candidateCommit="abc123", commands=[["pytest"]]))

    assert result.output_data["verificationState"] == "passed"
    assert len(_FakeProgressReporter.instances) == 1
    reporter = _FakeProgressReporter.instances[0]
    assert reporter.heartbeat_s == 10.0
    assert reporter.started is True
    assert reporter.stopped is True
    assert captured["on_progress"] == reporter.update


def test_test_run_worker_stops_the_reporter_even_when_verify_candidate_raises(monkeypatch, fake_task_input):
    _FakeProgressReporter.instances = []

    def boom(*_args, **_kwargs):
        raise RuntimeError("temporary worktree I/O failure")

    monkeypatch.setattr(verification, "verify_candidate", boom)
    monkeypatch.setattr(verification_tasks, "ProgressReporter", _FakeProgressReporter)

    result = verification_tasks.test_run(fake_task_input(
        repoPath="/tmp/repo", candidateCommit="abc123", commands=[["pytest"]]))

    assert result.output_data["verificationState"] == "blocked"
    assert len(_FakeProgressReporter.instances) == 1
    assert _FakeProgressReporter.instances[0].stopped is True


def test_agent_plan_resolve_worker_accepts_a_valid_proposal(fake_task_input, tmp_path):
    _python_repo(tmp_path)
    result = verification_tasks.test_agent_plan_resolve(fake_task_input(
        repoPath=str(tmp_path), testMode="targeted",
        proposal=[{"argv": ["pytest", "test_app.py"], "coveredPaths": ["test_app.py"]}],
        changedPaths=["test_app.py"],
    ))

    assert result.output_data["executionOutcome"] == "discovered"
    assert result.output_data["candidates"][0]["argv"] == ["pytest", "test_app.py"]


def test_agent_plan_resolve_worker_reports_a_rejected_proposal_as_configuration_blocked(
        fake_task_input, tmp_path):
    _python_repo(tmp_path)
    result = verification_tasks.test_agent_plan_resolve(fake_task_input(
        repoPath=str(tmp_path), testMode="targeted", proposal=[], changedPaths=["test_app.py"],
    ))

    assert result.output_data["verificationState"] == "blocked"
    assert result.output_data["executionOutcome"] == "configuration_blocked"
    assert result.output_data["candidates"] == []


def test_agent_plan_resolve_worker_reports_unexpected_errors_as_infra_blocked(
        monkeypatch, fake_task_input, tmp_path):
    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(verification, "validate_agent_argv", boom)
    result = verification_tasks.test_agent_plan_resolve(fake_task_input(
        repoPath=str(tmp_path), testMode="targeted",
        proposal=[{"argv": ["pytest", "test_app.py"]}], changedPaths=["test_app.py"],
    ))

    assert result.output_data["verificationState"] == "blocked"
    assert result.output_data["executionOutcome"] == "infra_blocked"


def test_java_home_blocks_when_ci_jdk_is_not_installed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JAVA99_HOME", raising=False)
    monkeypatch.delenv("JDK99_HOME", raising=False)
    with pytest.raises(verification.VerificationBlocked, match="requires Java 99"):
        verification._java_home("99")


def test_discovery_rejects_documented_heavyweight_targets(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Use `./gradlew integrationTest`.\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    git.ensure_ready(str(tmp_path))

    found = verification.discover_commands(str(tmp_path))

    assert found["selection"] == "build-system-inference"
    assert found["candidates"][0]["argv"] == ["./gradlew", "test"]


@pytest.mark.parametrize("argv", [
    "./gradlew build",
    ["sh", "-c", "pytest"],
    ["pytest", "&&", "echo", "oops"],
    ["./gradlew", "clean"],
    ["npm", "exec", "pytest"],
    ["npm", "ci"],
    ["make", "deploy"],
])
def test_command_validator_rejects_shell_and_generic_interpreters(argv):
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_argv(argv)


@pytest.mark.parametrize("argv", [
    ["./gradlew", "test"], ["./mvnw", "test"], ["npm", "test"],
    ["pnpm", "test"], ["yarn", "test"], ["pytest"],
    ["go", "test", "./..."], ["cargo", "test"], ["make", "test"],
])
def test_remediation_validator_prohibits_every_repository_wide_fallback(argv):
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_remediation_argv(argv)


def test_vetted_staging_rejects_cache_only_and_stages_source(tmp_git_repo: Path):
    (tmp_git_repo / "src.py").write_text("value = 1\n")
    (tmp_git_repo / ".gradle-local").mkdir()
    (tmp_git_repo / ".gradle-local" / "state").write_text("cache\n")

    staged = git.stage_vetted_changes(str(tmp_git_repo))

    assert staged["staged"] == ["src.py"]
    assert staged["rejected"] == [".gradle-local/"]


def test_cache_only_change_becomes_a_noop_commit(tmp_git_repo: Path):
    (tmp_git_repo / "build").mkdir()
    (tmp_git_repo / "build" / "output").write_text("generated\n")

    result = git.commit(str(tmp_git_repo), "cache is not a repair")

    assert result["noOp"] is True
    assert result["commit"] == git.head(str(tmp_git_repo))
    assert result["stagedPaths"] == []
    assert result["rejectedPaths"] == ["build/"]


def test_generated_change_prevents_ambiguous_source_deletion_from_being_committed(tmp_git_repo: Path):
    (tmp_git_repo / "source.py").write_text("value = 1\n")
    _commit(tmp_git_repo, "tracked source")
    (tmp_git_repo / "source.py").unlink()
    (tmp_git_repo / "build").mkdir()
    (tmp_git_repo / "build" / "source.py").write_text("value = 1\n")

    result = git.commit(str(tmp_git_repo), "do not turn a cache move into a deletion")

    assert result["noOp"] is True
    assert "source.py" not in result["stagedPaths"]
    assert "build/" in result["rejectedPaths"]
    assert "protected deletion: source.py" in result["rejectedPaths"]
    assert git.git(str(tmp_git_repo), "ls-files", "--error-unmatch", "source.py", check=False).code == 0


def test_clean_worktree_becomes_a_noop_commit(tmp_git_repo: Path):
    result = git.commit(str(tmp_git_repo), "nothing to commit")

    assert result["noOp"] is True
    assert result["commit"] == git.head(str(tmp_git_repo))
    assert result["stagedPaths"] == []


def test_verifier_uses_disposable_exact_commit(tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\ntest \"$(cat value.txt)\" = good\n")
    (tmp_git_repo / "value.txt").write_text("good\n")
    good = _commit(tmp_git_repo, "good candidate")
    (tmp_git_repo / "value.txt").write_text("bad\n")
    _commit(tmp_git_repo, "later change")

    result = verification.verify_candidate(str(tmp_git_repo), good, _focused_gradle_command())

    assert result["verificationState"] == "passed"
    assert result["candidateCommit"] == good
    assert Path(result["commands"][0]["logPath"]).is_file()
    assert not str(result["artifactDir"]).startswith(str(tmp_git_repo))
    assert not list(tmp_git_repo.glob("conductor-verify-*"))


def test_verify_candidate_reports_progress_before_each_command(tmp_git_repo: Path):
    # A single command can block for a very long time (a bare `pytest` full-suite
    # run has taken over an hour on this exact code path); on_progress is the
    # only signal test_run's ProgressReporter has to push periodic IN_PROGRESS
    # updates instead of looking frozen for the whole duration.
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\ntest \"$(cat value.txt)\" = good\n")
    (tmp_git_repo / "value.txt").write_text("good\n")
    good = _commit(tmp_git_repo, "good candidate")
    snapshots: list[dict] = []

    result = verification.verify_candidate(str(tmp_git_repo), good, _focused_gradle_command(),
                                           on_progress=snapshots.append)

    assert result["verificationState"] == "passed"
    assert len(snapshots) == 1
    assert snapshots[0]["commandIndex"] == 1
    assert snapshots[0]["totalCommands"] == 1
    assert snapshots[0]["currentCommand"] == _focused_gradle_command()[0]["argv"]
    assert snapshots[0]["completedCommands"] == []


def test_verify_candidate_survives_a_raising_on_progress_callback(tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\ntest \"$(cat value.txt)\" = good\n")
    (tmp_git_repo / "value.txt").write_text("good\n")
    good = _commit(tmp_git_repo, "good candidate")

    def _boom(_snapshot):
        raise RuntimeError("progress reporting must never break the run")

    result = verification.verify_candidate(str(tmp_git_repo), good, _focused_gradle_command(),
                                           on_progress=_boom)

    assert result["verificationState"] == "passed"


def test_verifier_supports_a_project_directory_nested_in_a_git_repository(tmp_git_repo: Path):
    project = tmp_git_repo / "project"
    project.mkdir()
    (project / "gradlew").write_text("#!/bin/sh\ntest -f marker.txt\n")
    (project / "marker.txt").write_text("ok\n")
    candidate = _commit(tmp_git_repo, "nested project candidate")

    result = verification.verify_candidate(str(project), candidate, _focused_gradle_command())

    assert result["verificationState"] == "passed"
    assert result["projectRelativePath"] == "project"


def test_verifier_candidate_git_mutation_cannot_touch_source_repository(tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\ngit update-ref refs/heads/verifier-escape HEAD\n")
    candidate = _commit(tmp_git_repo, "candidate with build git command")

    result = verification.verify_candidate(str(tmp_git_repo), candidate, _focused_gradle_command())

    assert result["verificationState"] == "passed"
    assert result["gitMetadataIsIsolated"] is True
    assert git.git(str(tmp_git_repo), "rev-parse", "refs/heads/verifier-escape", check=False).code != 0


def test_verifier_detaches_the_clone_remote_before_candidate_code_runs(tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\ntest -z \"$(git remote)\"\n")
    candidate = _commit(tmp_git_repo, "candidate checks verifier remotes")

    result = verification.verify_candidate(str(tmp_git_repo), candidate, _focused_gradle_command())

    assert result["verificationState"] == "passed"
    assert result["sourceRemoteDetached"] is True


def test_gradle_verification_uses_an_isolated_cache_home_and_environment(monkeypatch: pytest.MonkeyPatch, tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\n")
    candidate = _commit(tmp_git_repo, "gradle candidate")
    seen = {}

    monkeypatch.setenv("GH_TOKEN", "must-not-reach-build")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-build")

    def fake_run(argv, *, cwd, check, env=None, clean_env=False):
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["env"] = env or {}
        seen["clean_env"] = clean_env
        seen["git_dir_is_isolated"] = git.common_gitdir(cwd) != git.common_gitdir(str(tmp_git_repo))
        return type("Result", (), {"stdout": "ok", "stderr": "", "code": 0})()

    monkeypatch.setattr(verification, "run", fake_run)
    result = verification.verify_candidate(str(tmp_git_repo), candidate, _focused_gradle_command())

    assert result["verificationState"] == "passed"
    assert result["commands"][0]["scope"] == "focused"
    assert "conductor-dependency-cache" in seen["env"]["GRADLE_USER_HOME"]
    assert not str(tmp_git_repo) in seen["env"]["GRADLE_USER_HOME"]
    assert seen["clean_env"] is True
    assert "GH_TOKEN" not in seen["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in seen["env"]
    assert seen["env"]["HOME"].endswith("/home")
    assert seen["git_dir_is_isolated"] is True


def test_gradle_verification_blocks_missing_java_without_spending_repair_budget(monkeypatch: pytest.MonkeyPatch, tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\n")
    candidate = _commit(tmp_git_repo, "gradle candidate")
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(verification, "_candidate_jdk_homes", lambda _version: [])
    real_which = verification.shutil.which
    monkeypatch.setattr(verification.shutil, "which",
                        lambda command, path=None: None if command == "java" else real_which(command, path=path))

    result = verification.verify_candidate(str(tmp_git_repo), candidate, _focused_gradle_command())

    assert result["verificationState"] == "blocked"
    assert result["commands"][0]["blocked"] is True
    assert "no Java runtime" in result["commands"][0]["output"]


def test_verifier_strips_terminal_control_characters_from_candidate_output(monkeypatch, tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\n")
    candidate = _commit(tmp_git_repo, "control output candidate")

    def fake_run(*_args, **_kwargs):
        return type("Result", (), {"stdout": "ok\x1b[2J\x00", "stderr": "bad\x07", "code": 1})()

    monkeypatch.setattr(verification, "run", fake_run)
    result = verification.verify_candidate(str(tmp_git_repo), candidate, _focused_gradle_command())

    output = result["commands"][0]["output"]
    assert "\x1b" not in output and "\x00" not in output and "\x07" not in output
    assert "ok[2J" in output and "bad" in output


def test_exit_124_is_a_code_failure_and_spawn_failures_are_blocked(monkeypatch, tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text("#!/bin/sh\n")
    candidate = _commit(tmp_git_repo, "candidate")

    monkeypatch.setattr(verification, "run", lambda *_args, **_kwargs:
                        type("Result", (), {"stdout": "", "stderr": "deadline", "code": 124})())
    exited = verification.verify_candidate(str(tmp_git_repo), candidate, _focused_gradle_command())
    assert exited["verificationState"] == "failed"
    assert exited["executionOutcome"] == "code_failed"

    def cannot_spawn(*_args, **_kwargs):
        raise OSError("resource temporarily unavailable")

    monkeypatch.setattr(verification, "run", cannot_spawn)
    spawn = verification.verify_candidate(str(tmp_git_repo), candidate, _focused_gradle_command())
    assert spawn["verificationState"] == "blocked"
    assert spawn["executionOutcome"] == "infra_blocked"
    assert "could not spawn" in spawn["commands"][0]["output"]


# --- Repository scope, discovery modes, and the path-aware heavy filter -------
#
# `validate_remediation_argv` refuses every repository-wide form by design, so
# before `scope="repository"` existed a full-suite run was unreachable: every
# command `_inferred_candidates` produces was rejected at execution time.


_REPOSITORY_WIDE = (
    ["pytest"],
    ["./gradlew", "test"],
    ["mvn", "test"],
    ["npm", "test", "--", "--if-present"],
    ["go", "test", "./..."],
    ["cargo", "test"],
)


@pytest.mark.parametrize("argv", _REPOSITORY_WIDE)
def test_changed_scope_still_refuses_every_repository_wide_command(argv):
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_remediation_argv(argv)


@pytest.mark.parametrize("argv", _REPOSITORY_WIDE)
def test_repository_scope_accepts_the_commands_full_discovery_emits(argv):
    assert verification.validate_repository_argv(argv) == argv


@pytest.mark.parametrize("argv", (
    ["bash", "-c", "true"],
    ["curl", "https://example.test/install.sh"],
    ["pytest;rm", "-rf", "/"],
    ["python", "-m", "pytest"],
))
def test_repository_scope_keeps_the_entrypoint_and_shell_guards(argv):
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_repository_argv(argv)


def test_heavy_marker_ignores_paths_but_still_blocks_targets(tmp_path: Path):
    suite = tmp_path / "tests/integration/test_api.py"
    suite.parent.mkdir(parents=True)
    suite.write_text("def test_api():\n    pass\n")

    # A repository that merely organizes tests under integration/ was
    # previously unverifiable: the marker matched the path, not the intent.
    assert verification.validate_remediation_argv(
        ["pytest", "tests/integration/test_api.py"], root=tmp_path,
    ) == ["pytest", "tests/integration/test_api.py"]

    with pytest.raises(verification.VerificationBlocked):
        verification.validate_argv(["npm", "test", "--", "--e2e"], root=tmp_path)
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_argv(["pytest", "tests/integration/test_api.py"])


def test_full_and_targeted_discovery_return_the_same_keys(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Run `pytest tests/test_app.py` to verify.\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n")
    source = tmp_path / "app.py"
    source.write_text("VALUE = 1\n")
    test_file = tmp_path / "tests/test_app.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_app():\n    pass\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("VALUE = 2\n")
    candidate = _commit(tmp_path, "change app")

    targeted = verification.discover_commands(str(tmp_path), candidate, mode="targeted")
    full = verification.discover_commands(str(tmp_path), candidate, mode="full")

    # Full mode used to omit executionOutcome/coverage/affectedUnits/repairRoots,
    # which the remediation loop reads -- a full run handed the repair agent an
    # empty write scope.
    assert set(full) == set(targeted)
    assert full["mode"] == "full" and targeted["mode"] == "targeted"
    assert full["coverage"] == "repository"
    assert full["executionOutcome"] == "discovered"
    assert full["repairRoots"] == targeted["repairRoots"]


def test_full_mode_derives_a_repair_scope_from_the_candidate(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.23\n")
    (tmp_path / "AGENTS.md").write_text("Run `go test ./...` to verify.\n")
    source = tmp_path / "internal/api/api.go"
    source.parent.mkdir(parents=True)
    source.write_text("package api\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("package api\n// changed\n")
    candidate = _commit(tmp_path, "change go package")

    full = verification.discover_commands(str(tmp_path), candidate, mode="full")

    # The command set is repository-wide, but a repair still needs somewhere to
    # write. _changed_paths returns nothing without a commit, so full mode has
    # to derive the scope from the candidate rather than hand back an empty list.
    assert full["candidates"][0]["argv"] == ["go", "test", "./..."]
    assert full["repairRoots"] == ["internal/api"]
    assert full["repairScopeResolved"] is True


def test_discovery_mode_defaults_preserve_historical_behaviour(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n")
    source = tmp_path / "app.py"
    source.write_text("VALUE = 1\n")
    test_file = tmp_path / "tests/test_app.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_app():\n    pass\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("VALUE = 2\n")
    candidate = _commit(tmp_path, "change app")

    assert verification.discover_commands(str(tmp_path), candidate)["mode"] == "targeted"
    assert verification.discover_commands(str(tmp_path))["mode"] == "full"
    with pytest.raises(verification.VerificationBlocked):
        verification.discover_commands(str(tmp_path), mode="targeted")
    with pytest.raises(verification.VerificationBlocked):
        verification.discover_commands(str(tmp_path), candidate, mode="sideways")


def test_full_mode_excludes_a_guide_the_candidate_itself_edited(tmp_path: Path):
    guide = tmp_path / "AGENTS.md"
    guide.write_text("Run `pytest tests/test_app.py` to verify.\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n")
    test_file = tmp_path / "tests/test_app.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_app():\n    pass\n")
    git.ensure_ready(str(tmp_path))
    guide.write_text("Run `pytest tests/test_app.py --maxfail=99` to verify.\n")
    candidate = _commit(tmp_path, "edit the guide")

    # Until full mode was given a candidate this filter could never fire, because
    # discovery without a commit sees no changed paths at all.
    found = verification.discover_commands(str(tmp_path), candidate, mode="full")
    assert found["selection"] == "build-system-inference"
    assert found["sourcePaths"] == []


def test_one_unmappable_planner_output_drops_instead_of_aborting_discovery(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n")
    source = tmp_path / "app.py"
    source.write_text("VALUE = 1\n")
    test_file = tmp_path / "tests/test_app.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_app():\n    pass\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("VALUE = 2\n")
    candidate = _commit(tmp_path, "change app")

    rejected: list[dict] = []
    real_planner = verification._targeted_python_candidates

    def planner_with_one_bad_output(root, changed_paths):
        return [{"argv": ["make", "deploy"], "source": "test:bad", "kind": "changed-scope",
                 "scope": "affected", "affectedUnit": "bad", "coveredPaths": []},
                *real_planner(root, changed_paths)]

    original = verification._changed_scope_candidates.__globals__["_targeted_python_candidates"]
    verification._changed_scope_candidates.__globals__["_targeted_python_candidates"] = \
        planner_with_one_bad_output
    try:
        candidates = verification._changed_scope_candidates(
            tmp_path, verification._changed_paths(tmp_path, candidate), rejected)
    finally:
        verification._changed_scope_candidates.__globals__["_targeted_python_candidates"] = original

    assert [c["argv"] for c in candidates] == [["pytest", "tests/test_app.py"]]
    assert len(rejected) == 1
    assert rejected[0]["argv"] == ["make", "deploy"]
    assert "make" in rejected[0]["reason"]


def test_scan_skip_covers_vendored_trees_not_just_build_output():
    for part in ("node_modules", "vendor", "_build", "deps", ".venv"):
        assert part in verification._SCAN_SKIP_PARTS
    # is_generated_path classifies *changed* paths and must stay narrow: a
    # vendored file is not a generated build artifact.
    assert not verification.is_generated_path("vendor/lib/thing.php")
    assert verification.is_generated_path("build/classes/Thing.class")


def test_java_entrypoints_are_shared_with_the_command_runtime():
    assert verification._JAVA_ENTRYPOINTS is check_execution.JAVA_ENTRYPOINTS


# --- Worker concurrency -------------------------------------------------------
#
# A parallel fan-out asks for one verification per subtask. At the SDK default
# of thread_count=1 they queue behind each other, turning the fan-out into a
# serial test phase.


def _configured_records():
    import verification.tasks  # noqa: F401  (registers the @worker_task functions)
    from conductor.client.automator.task_handler import _decorated_functions

    polling.configure_registered_workers()
    return _decorated_functions


def test_verification_tasks_poll_concurrently():
    records = _configured_records()
    assert records[("test_run", None)]["thread_count"] >= 2
    assert records[("test_discover", None)]["thread_count"] >= 2


def test_thread_count_override_raises_but_never_lowers(monkeypatch):
    # A broad operator env var must not silently undo a per-task ceiling that
    # exists because that task spawns a real build toolchain.
    assert polling.worker_thread_count("test_run", 3) == 3
    monkeypatch.setenv("CONDUCTOR_WORKER_THREADS_TEST_RUN", "8")
    assert polling.worker_thread_count("test_run", 3) == 8
    monkeypatch.setenv("CONDUCTOR_WORKER_THREADS_TEST_RUN", "1")
    assert polling.worker_thread_count("test_run", 3) == 3
    monkeypatch.delenv("CONDUCTOR_WORKER_THREADS_TEST_RUN")
    monkeypatch.setenv("CONDUCTOR_WORKER_THREADS", "6")
    assert polling.worker_thread_count("test_discover", 4) == 6
    monkeypatch.setenv("CONDUCTOR_WORKER_THREADS", "not-a-number")
    assert polling.worker_thread_count("test_discover", 4) == 4


def test_guard_report_exposes_thread_count():
    _configured_records()
    report = polling.registered_worker_guard_report()
    counts = {worker["task"]: worker["threadCount"] for worker in report["workers"]}
    # Proving concurrency from a deployed fleet requires reading it back out of
    # verification_health, not trusting the source.
    assert counts["test_run"] >= 2
    assert report["healthy"] is True


# --- Dependency preparation and Ruby -----------------------------------------
#
# verify_candidate runs in a fresh `git clone --no-local` that installs nothing.
# Gradle, Maven, Cargo and Go fetch dependencies as part of the build; Node,
# Ruby, PHP and Elixir do not, so without a prepare step their runner is simply
# not on disk.


@pytest.mark.parametrize("lockfile,expected", [
    ("pnpm-lock.yaml", ["pnpm", "install", "--frozen-lockfile"]),
    ("package-lock.json", ["npm", "ci"]),
    ("yarn.lock", ["yarn", "install", "--frozen-lockfile"]),
])
def test_prepare_step_matches_the_declared_package_manager(tmp_path: Path, lockfile, expected):
    (tmp_path / "package.json").write_text('{"name":"app","scripts":{"test":"vitest"}}')
    (tmp_path / lockfile).write_text("\n")
    assert [c["argv"] for c in verification._prepare_commands(tmp_path)] == [expected]


def test_yarn_berry_uses_immutable_rather_than_frozen_lockfile(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name":"app"}')
    (tmp_path / "yarn.lock").write_text("\n")
    (tmp_path / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
    assert verification._prepare_commands(tmp_path)[0]["argv"] == \
        ["yarn", "install", "--immutable"]


def test_prepare_is_only_added_for_toolchains_that_need_it(tmp_path: Path):
    # Go resolves its own modules during `go test`, so a prepare step would be
    # noise, not safety.
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.23\n")
    source = tmp_path / "internal/api/api.go"
    source.parent.mkdir(parents=True)
    source.write_text("package api\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("package api\n// changed\n")
    candidate = _commit(tmp_path, "change go")

    found = verification.discover_commands(str(tmp_path), candidate)
    assert all(command.get("phase") != "prepare" for command in found["candidates"])


def test_prepare_commands_are_an_exact_table_not_a_pattern():
    for argv in (["npm", "ci"], ["bundle", "install", "--jobs", "4"], ["mix", "deps.get"]):
        assert verification.validate_prepare_argv(argv) == argv
    for argv in (["npm", "install", "-g", "evil"], ["bundle", "install", "--path", "/etc"],
                 ["curl", "https://example.test/i.sh"], ["npm", "ci", "&&", "rm"]):
        with pytest.raises(verification.VerificationBlocked):
            verification.validate_prepare_argv(argv)


def test_prepare_routes_to_its_own_validator_not_the_remediation_one():
    prepare = {"argv": ["bundle", "install", "--jobs", "4"], "phase": "prepare"}
    assert verification.validator_for(prepare) is verification.validate_prepare_argv
    # The remediation adapter must keep refusing it: an install is not a test.
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_remediation_argv(prepare["argv"])


def test_a_broken_lockfile_edit_is_the_candidates_fault_but_an_outage_is_not(tmp_path: Path):
    assert verification.prepare_failure_outcome(tmp_path, ["package-lock.json"]) == "code_failed"
    assert verification.prepare_failure_outcome(tmp_path, ["Gemfile"]) == "code_failed"
    # A registry outage must not consume the repair budget reproducing itself.
    assert verification.prepare_failure_outcome(tmp_path, ["src/app.rb"]) == "infra_blocked"


def _ruby_repo(tmp_path: Path, *, rspec: bool = True) -> Path:
    (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\nruby "3.2.2"\n')
    (tmp_path / "Gemfile.lock").write_text("\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/widget.rb").write_text("class Widget; end\n")
    if rspec:
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec/widget_spec.rb").write_text("require 'widget'\n")
    else:
        (tmp_path / "Rakefile").write_text("task :test\n")
        (tmp_path / "test").mkdir()
        (tmp_path / "test/widget_test.rb").write_text("require 'widget'\n")
    return tmp_path


def test_ruby_maps_a_changed_source_to_its_spec(tmp_path: Path):
    root = _ruby_repo(tmp_path)
    git.ensure_ready(str(root))
    (root / "lib/widget.rb").write_text("class Widget; def go; end; end\n")
    candidate = _commit(root, "change widget")

    found = verification.discover_commands(str(root), candidate)

    assert [command["argv"] for command in found["candidates"]] == [
        ["bundle", "install", "--jobs", "4"],
        ["bundle", "exec", "rspec", "spec/widget_spec.rb"],
    ]
    assert found["coverage"] == "focused"
    # check_execution already probed ruby/bundle/rake and already isolated the
    # gem caches; only the planner was missing.
    assert found["runtimeRequirements"]["ruby"] == "3.2.2"


def test_ruby_minitest_uses_an_exact_rake_test_target(tmp_path: Path):
    root = _ruby_repo(tmp_path, rspec=False)
    git.ensure_ready(str(root))
    (root / "lib/widget.rb").write_text("class Widget; def go; end; end\n")
    candidate = _commit(root, "change widget")

    argv = verification.discover_commands(str(root), candidate)["candidates"][-1]["argv"]
    assert argv == ["bundle", "exec", "rake", "test", "TEST=test/widget_test.rb"]


def test_ruby_planner_stays_quiet_without_a_gemfile(tmp_path: Path):
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec/widget_spec.rb").write_text("\n")
    assert verification._targeted_ruby_candidates(tmp_path, ["lib/widget.rb"]) == []


def test_every_entrypoint_has_a_runtime_probe():
    # An entrypoint without a probe fails at execution with a bare
    # "command not found" instead of a named runtime_unavailable.
    probed = {argv[0] for argv in check_execution._RUNTIME_PROBES.values()}
    # Host-resident or build-file-resident tools the probe table does not cover;
    # they were already in _ENTRYPOINTS before this change.
    preexisting = {"./gradlew", "./mvnw", "gradle", "mvn", "pytest", "cargo",
                   "make", "cmake", "ctest", "swift"}
    assert verification._ENTRYPOINTS <= probed | preexisting, \
        verification._ENTRYPOINTS - (probed | preexisting)
    # Ruby specifically: check_execution has probed ruby/bundle/rake all along,
    # so the planner added no unprobed runtime.
    assert {"ruby", "bundle", "rake"} <= set(check_execution._RUNTIME_PROBES)


# --- Multi-language planners --------------------------------------------------


def _language_repo(tmp_path: Path, files: dict[str, str], changed: str) -> tuple[Path, str]:
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    git.ensure_ready(str(tmp_path))
    (tmp_path / changed).write_text((tmp_path / changed).read_text() + "\n// changed\n")
    return tmp_path, _commit(tmp_path, "change")


LANGUAGE_CASES = {
    "dotnet": ({"App/App.csproj": "<Project/>",
                "App.Tests/App.Tests.csproj":
                    "<Project><PackageReference Include='Microsoft.NET.Test.Sdk'/></Project>",
                "App/Widget.cs": "class Widget {}"},
               "App/Widget.cs", ["dotnet", "test", "App.Tests/App.Tests.csproj"], "affected"),
    "php": ({"composer.json": '{"scripts":{"test":"phpunit"}}', "phpunit.xml": "<phpunit/>",
             "src/Widget.php": "<?php class Widget {}",
             "tests/WidgetTest.php": "<?php class WidgetTest {}"},
            "src/Widget.php", ["composer", "test", "--", "--filter", "WidgetTest"], "focused"),
    "elixir": ({"mix.exs": 'defmodule M do\n  def project, do: [elixir: "~> 1.15"]\nend',
                "lib/widget.ex": "defmodule Widget do\nend",
                "test/widget_test.exs": "defmodule WidgetTest do\nend"},
               "lib/widget.ex", ["mix", "test", "test/widget_test.exs"], "focused"),
    "sbt": ({"build.sbt": 'lazy val core = (project in file("core"))',
             "core/src/main/scala/W.scala": "object W"},
            "core/src/main/scala/W.scala", ["sbt", "core/test"], "affected"),
    "make": ({"Makefile": "api-test:\n\tgo test ./api\n", "api/a.go": "package api"},
             "api/a.go", ["make", "api-test"], "affected"),
}


@pytest.mark.parametrize("name", sorted(LANGUAGE_CASES))
def test_language_planners_map_a_change_to_a_scoped_command(tmp_path: Path, name):
    files, changed, expected, scope = LANGUAGE_CASES[name]
    root, candidate = _language_repo(tmp_path, files, changed)

    found = verification.discover_commands(str(root), candidate)
    tests = [c for c in found["candidates"] if c.get("phase") != "prepare"]

    assert [c["argv"] for c in tests] == [expected]
    assert tests[0]["scope"] == scope
    assert changed in tests[0]["coveredPaths"]


@pytest.mark.parametrize("name", sorted(LANGUAGE_CASES))
def test_language_planners_stay_quiet_without_their_manifest(tmp_path: Path, name):
    files, changed, _, _ = LANGUAGE_CASES[name]
    manifests = {"dotnet": "App.Tests/App.Tests.csproj", "php": "composer.json",
                 "elixir": "mix.exs", "sbt": "build.sbt", "make": "Makefile"}
    trimmed = {k: v for k, v in files.items() if k != manifests[name]}
    for relative, body in trimmed.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    planner = getattr(verification, f"_targeted_{name}_candidates")
    assert planner(tmp_path, [changed]) == []


def test_single_package_javascript_repo_targets_its_spec_file(tmp_path: Path):
    root, candidate = _language_repo(tmp_path, {
        "package.json": '{"name":"app","scripts":{"test":"vitest run"}}',
        "src/widget.ts": "export const a = 1;",
        "src/widget.test.ts": "test('a', () => {});",
    }, "src/widget.ts")

    found = verification.discover_commands(str(root), candidate)
    tests = [c for c in found["candidates"] if c.get("phase") != "prepare"]

    # A non-workspace repository produced no targeted candidate at all before.
    assert tests[0]["argv"] == ["npm", "test", "--", "src/widget.test.ts"]
    assert tests[0]["scope"] == "focused"


def test_watch_mode_runners_never_receive_a_file_filter(tmp_path: Path):
    # Bare `vitest` defaults to watch mode, which would hang the run forever.
    (tmp_path / "package.json").write_text('{"name":"app","scripts":{"test":"vitest"}}')
    assert verification._js_runner_accepts_paths(tmp_path, ".") is False
    (tmp_path / "package.json").write_text('{"name":"app","scripts":{"test":"vitest run"}}')
    assert verification._js_runner_accepts_paths(tmp_path, ".") is True
    # A browser runner is never targeted, whatever the script says.
    (tmp_path / "package.json").write_text('{"name":"app","scripts":{"test":"playwright test"}}')
    assert verification._js_runner_accepts_paths(tmp_path, ".") is False


def test_bazel_maps_a_changed_source_to_its_exact_test_label(tmp_path: Path):
    root, candidate = _language_repo(tmp_path, {
        "MODULE.bazel": "module(name='app')",
        "api/BUILD": "go_test(name = 'api_test', srcs = ['api_test.go', 'api.go'])",
        "api/api.go": "package api",
        "api/api_test.go": "package api",
    }, "api/api.go")

    found = verification.discover_commands(str(root), candidate)
    assert [c["argv"] for c in found["candidates"]] == [["bazel", "test", "//api:api_test"]]


@pytest.mark.parametrize("argv", [
    ["dotnet", "run"], ["dotnet", "tool", "install"],
    ["composer", "exec", "bash"], ["mix", "run", "-e", "System.halt()"],
    ["sbt", "consoleProject"], ["bazel", "run", "//:app"],
    ["npx", "playwright", "test"], ["bundle", "exec", "sh", "-c", "x"],
])
def test_new_entrypoints_refuse_non_test_and_interpreter_invocations(argv):
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_argv(argv)


@pytest.mark.parametrize("argv", [
    ["dotnet", "test"], ["composer", "test"], ["mix", "test"], ["sbt", "test"],
    ["bazel", "test", "//..."], ["make", "test"], ["make", "check"],
    ["npm", "test"], ["pnpm", "test"], ["yarn", "test"],
])
def test_new_tools_still_refuse_a_repository_wide_fallback_in_targeted_mode(argv):
    # Repository-wide is exactly what targeted mode exists to prevent; it stays
    # reachable only through an explicit full-mode request.
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_remediation_argv(argv)
    assert verification.validate_repository_argv(argv) == argv


def test_optional_runtime_families_do_not_make_a_host_unhealthy(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_WORKER_ALL_LEASE_EXTEND_ENABLED", "true")
    monkeypatch.setattr(check_execution, "runtime_capabilities", lambda *, isolated: {
        tool: {"available": tool not in {"dotnet", "php", "composer", "mix"}}
        for argv in check_execution._RUNTIME_PROBES.values() for tool in [argv[0]]
    })
    report = check_execution.runtime_health_report()

    # A machine that never builds .NET or PHP is not unhealthy for lacking them.
    assert report["healthy"] is True
    assert report["families"]["dotnet"]["available"] is False
    assert report["families"]["dotnet"]["required"] is False
    assert report["families"]["java"]["required"] is True


def test_browser_suites_require_provisioned_binaries(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("CYPRESS_CACHE_FOLDER", raising=False)
    assert "no browser test configuration" in verification.browser_suite_reason(tmp_path)

    (tmp_path / "playwright.config.ts").write_text("export default {};")
    reason = verification.browser_suite_reason(tmp_path)
    # Never download browsers inside a verification run.
    assert reason is not None and "provisioned browser binaries" in reason

    cache = tmp_path / "browsers"
    (cache / "chromium").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    assert verification.browser_suite_reason(tmp_path) is None


def _browser_repo(tmp_path: Path, script: str = "playwright test") -> tuple[Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "playwright.config.ts").write_text("export default {};")
    (tmp_path / "package.json").write_text(json.dumps(
        {"name": "app", "scripts": {"test:e2e": script}}))
    git.ensure_ready(str(tmp_path))
    (tmp_path / "package.json").write_text(json.dumps(
        {"name": "app", "scripts": {"test:e2e": script}, "version": "1.0.1"}))
    return tmp_path, _commit(tmp_path, "bump version")


def test_browser_suite_candidate_requires_the_scripts_own_first_token_to_be_a_real_runner(tmp_path: Path):
    root, _ = _browser_repo(tmp_path, script="npx playwright test")
    # npx resolves and fetches from the network at run time, so a script that
    # only reaches the runner through npx is not something this can validate.
    assert verification._browser_suite_candidate(root) is None

    root2, _ = _browser_repo(tmp_path.parent / "direct", script="playwright test")
    candidate = verification._browser_suite_candidate(root2)
    assert candidate is not None
    assert candidate["argv"] == ["npm", "run", "test:e2e"]
    assert candidate["adapter"] == "browser-suite"


def test_validate_browser_argv_accepts_only_the_package_manager_entrypoints():
    assert verification.validate_browser_argv(["npm", "run", "test:e2e"]) == ["npm", "run", "test:e2e"]
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_browser_argv(["npx", "playwright", "test"])
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_browser_argv(["npm", "run", "test:e2e", "&&", "rm", "-rf", "/"])
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_browser_argv([])


def test_full_mode_adds_a_browser_candidate_only_when_binaries_are_provisioned(tmp_path: Path, monkeypatch):
    root, candidate = _browser_repo(tmp_path)
    cache = tmp_path.parent / "browsers"
    (cache / "chromium").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))

    found = verification.discover_commands(str(root), candidate, mode="full", include_browser_tests=True)
    browser = [c for c in found["candidates"] if c.get("adapter") == "browser-suite"]
    assert browser and browser[0]["argv"] == ["npm", "run", "test:e2e"]
    assert found["rejectedCandidates"] == []


def test_full_mode_reports_a_diagnostic_instead_of_blocking_when_browsers_are_absent(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("CYPRESS_CACHE_FOLDER", raising=False)
    root, candidate = _browser_repo(tmp_path)

    found = verification.discover_commands(str(root), candidate, mode="full", include_browser_tests=True)
    assert found["executionOutcome"] == "discovered"
    assert not any(c.get("adapter") == "browser-suite" for c in found["candidates"])
    reasons = [r["reason"] for r in found["rejectedCandidates"] if r["source"] == "browser-suite"]
    assert reasons and "provisioned browser binaries" in reasons[0]


def test_include_browser_tests_is_a_noop_without_the_flag(tmp_path: Path, monkeypatch):
    root, candidate = _browser_repo(tmp_path)
    cache = tmp_path.parent / "browsers-off"
    (cache / "chromium").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))

    found = verification.discover_commands(str(root), candidate, mode="full")
    assert not any(c.get("adapter") == "browser-suite" for c in found["candidates"])
    assert found["rejectedCandidates"] == []


def test_targeted_mode_never_considers_browser_tests_even_with_the_flag(tmp_path: Path, monkeypatch):
    # A dedicated Python change, unrelated to the browser suite, so that
    # targeted discovery has an ordinary mappable candidate; the assertion is
    # that a browser-suite candidate never appears here regardless.
    (tmp_path / "playwright.config.ts").write_text("export default {};")
    (tmp_path / "package.json").write_text(json.dumps(
        {"name": "app", "scripts": {"test:e2e": "playwright test"}}))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n")
    source = tmp_path / "app.py"
    source.write_text("VALUE = 1\n")
    test_file = tmp_path / "test_app.py"
    test_file.write_text("def test_app():\n    pass\n")
    git.ensure_ready(str(tmp_path))
    source.write_text("VALUE = 2\n")
    candidate = _commit(tmp_path, "change app")
    cache = tmp_path.parent / "browsers-targeted"
    (cache / "chromium").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))

    found = verification.discover_commands(str(tmp_path), candidate, mode="targeted",
                                            include_browser_tests=True)
    assert not any(c.get("adapter") == "browser-suite" for c in found["candidates"])


def _python_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n")
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    (tmp_path / "test_app.py").write_text("def test_app():\n    pass\n")
    return tmp_path


def test_detected_build_systems_reports_only_real_evidence(tmp_path: Path):
    _python_repo(tmp_path)
    assert verification._detected_build_systems(tmp_path) == {"pytest"}
    assert "cargo" not in verification._detected_build_systems(tmp_path)


def test_detected_build_systems_finds_nested_manifests(tmp_path: Path):
    # A repository that keeps every project self-contained under its own
    # directory (no root-level manifest at all) is exactly as real as one
    # rooted at the top -- this used to be invisible to full-mode discovery.
    nested = tmp_path / "features" / "widget"
    nested.mkdir(parents=True)
    (nested / "go.mod").write_text("module widget\n\ngo 1.22\n")
    detected = verification._detected_build_systems(tmp_path)
    assert "go" in detected


def test_prepare_commands_scopes_a_nested_npm_project(tmp_path: Path):
    # A fresh disposable clone installs nothing; without a scoped prepare step
    # a nested npm project's test runner is never on disk (confirmed live:
    # "vitest: command not found", exit 127, even though testsPassed should
    # depend on real test results, not on this being silently skipped).
    nested = tmp_path / "features" / "widget"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text('{"name": "widget"}')
    (nested / "package-lock.json").write_text('{}')

    commands = verification._prepare_commands(tmp_path)

    assert commands == [{"argv": ["npm", "--prefix", "features/widget", "ci"],
                         "source": "prepare:package-lock.json:features/widget",
                         "kind": "prepare", "phase": "prepare", "scope": "affected",
                         "affectedUnit": "dependencies:features/widget", "coveredPaths": []}]


def test_prepare_commands_root_case_is_unchanged(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "app"}')

    commands = verification._prepare_commands(tmp_path)

    assert commands == [{"argv": ["npm", "install", "--no-audit", "--no-fund"],
                         "source": "prepare:package.json:.",
                         "kind": "prepare", "phase": "prepare", "scope": "affected",
                         "affectedUnit": "dependencies:.", "coveredPaths": []}]


def test_with_prepare_matches_each_candidate_to_its_own_directory(tmp_path: Path):
    widget_dir = tmp_path / "features" / "widget"
    widget_dir.mkdir(parents=True)
    (widget_dir / "package.json").write_text('{"name": "widget"}')
    gadget_dir = tmp_path / "features" / "gadget"
    gadget_dir.mkdir(parents=True)
    (gadget_dir / "package.json").write_text('{"name": "gadget"}')

    candidates = [
        {"argv": ["npm", "--prefix", "features/widget", "test", "--if-present"]},
        {"argv": ["npm", "--prefix", "features/gadget", "test", "--if-present"]},
    ]
    result = verification._with_prepare(tmp_path, candidates)

    assert [c["argv"] for c in result] == [
        ["npm", "--prefix", "features/widget", "install", "--no-audit", "--no-fund"],
        ["npm", "--prefix", "features/widget", "test", "--if-present"],
        ["npm", "--prefix", "features/gadget", "install", "--no-audit", "--no-fund"],
        ["npm", "--prefix", "features/gadget", "test", "--if-present"],
    ]


def test_validate_prepare_argv_accepts_a_scoped_npm_install():
    assert verification.validate_prepare_argv(
        ["npm", "--prefix", "features/widget", "install", "--no-audit", "--no-fund"]
    ) == ["npm", "--prefix", "features/widget", "install", "--no-audit", "--no-fund"]


def test_validate_prepare_argv_still_rejects_an_unknown_command():
    with pytest.raises(verification.VerificationBlocked):
        verification.validate_prepare_argv(["npm", "--prefix", "features/widget", "run", "postinstall"])


def test_inferred_candidates_scopes_a_nested_npm_project_with_prepare(tmp_path: Path):
    nested = tmp_path / "features" / "widget"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text('{"name": "widget", "scripts": {"test": "vitest run"}}')
    git.ensure_ready(str(tmp_path))

    found = verification.discover_commands(str(tmp_path))
    argvs = [c["argv"] for c in found["candidates"]]

    assert ["npm", "--prefix", "features/widget", "install", "--no-audit", "--no-fund"] in argvs
    assert ["npm", "--prefix", "features/widget", "test", "--if-present"] in argvs
    # The install must come before the test that needs it.
    install_index = argvs.index(["npm", "--prefix", "features/widget", "install", "--no-audit", "--no-fund"])
    test_index = argvs.index(["npm", "--prefix", "features/widget", "test", "--if-present"])
    assert install_index < test_index


def test_detected_build_systems_ignores_vendored_nested_manifests(tmp_path: Path):
    vendored = tmp_path / "node_modules" / "some-dep"
    vendored.mkdir(parents=True)
    (vendored / "Cargo.toml").write_text("[package]\nname = 'dep'\n")
    assert "cargo" not in verification._detected_build_systems(tmp_path)


def test_inferred_candidates_scopes_a_nested_go_module(tmp_path: Path):
    nested = tmp_path / "features" / "widget"
    nested.mkdir(parents=True)
    (nested / "go.mod").write_text("module widget\n\ngo 1.22\n")
    git.ensure_ready(str(tmp_path))

    found = verification.discover_commands(str(tmp_path))

    go_candidates = [c for c in found["candidates"] if c["argv"][0] == "go"]
    assert go_candidates == [{"argv": ["go", "-C", "features/widget", "test", "./..."],
                              "source": "build-system:go:features/widget", "kind": "inferred"}]


def test_inferred_candidates_scopes_a_nested_cargo_and_cmake_project(tmp_path: Path):
    rust_dir = tmp_path / "features" / "ledger"
    rust_dir.mkdir(parents=True)
    (rust_dir / "Cargo.toml").write_text("[package]\nname = 'ledger'\nedition = '2021'\n")
    cpp_dir = tmp_path / "features" / "device"
    cpp_dir.mkdir(parents=True)
    (cpp_dir / "CMakeLists.txt").write_text(
        "add_executable(device_tests tests/test_device.cpp)\n"
        "add_test(NAME device_tests COMMAND device_tests)\n"
    )
    git.ensure_ready(str(tmp_path))

    found = verification.discover_commands(str(tmp_path))
    argvs = [c["argv"] for c in found["candidates"]]

    assert ["cargo", "test", "--manifest-path", "features/ledger/Cargo.toml"] in argvs
    assert ["cmake", "-S", "features/device", "-B", "features/device/.conductor-verify-build"] in argvs
    assert ["ctest", "--test-dir", "features/device/.conductor-verify-build", "--output-on-failure"] in argvs


def test_inferred_candidates_covers_every_accumulated_language_together(tmp_path: Path):
    # This harness's own real-e2e test repository accumulates one independent,
    # self-contained project per scenario across many languages over time;
    # full mode must run every one of them, not just whichever build system a
    # first-match check happened to find first.
    _python_repo(tmp_path)
    go_dir = tmp_path / "features" / "widget"
    go_dir.mkdir(parents=True)
    (go_dir / "go.mod").write_text("module widget\n\ngo 1.22\n")
    git.ensure_ready(str(tmp_path))

    found = verification.discover_commands(str(tmp_path))
    entrypoints = {c["argv"][0] for c in found["candidates"]}

    assert {"pytest", "go"} <= entrypoints


def test_targeted_cmake_candidates_handles_a_nested_only_project(tmp_path: Path):
    cpp_dir = tmp_path / "features" / "device"
    cpp_dir.mkdir(parents=True)
    (cpp_dir / "CMakeLists.txt").write_text(
        "add_library(device_lib src/device.cpp)\n"
        "add_executable(device_tests tests/test_device.cpp)\n"
        "target_link_libraries(device_tests device_lib)\n"
        "add_test(NAME device_tests COMMAND device_tests)\n"
    )

    candidates = verification._targeted_cmake_candidates(tmp_path, ["features/device/src/device.cpp"])

    assert candidates[0]["argv"] == ["cmake", "-S", "features/device", "-B", "features/device/.conductor-verify-build"]
    build_targets = [c["argv"] for c in candidates if c["argv"][:2] == ["cmake", "--build"]]
    assert ["cmake", "--build", "features/device/.conductor-verify-build", "--target", "device_tests"] in build_targets


def test_targeted_go_candidates_handles_a_nested_module(tmp_path: Path):
    go_dir = tmp_path / "features" / "widget"
    go_dir.mkdir(parents=True)
    (go_dir / "go.mod").write_text("module widget\n\ngo 1.22\n")

    candidates = verification._targeted_go_candidates(tmp_path, ["features/widget/reconcile.go"])

    assert candidates == [{"argv": ["go", "-C", "features/widget", "test", "."],
                           "source": "build-metadata:go:features/widget",
                           "kind": "changed-scope", "scope": "affected",
                           "affectedUnit": "features/widget", "repairRoots": ["features/widget"],
                           "coveredPaths": ["features/widget/reconcile.go"]}]


def test_targeted_go_candidates_still_handles_a_root_module(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module app\n\ngo 1.22\n")
    (tmp_path / "internal" / "api").mkdir(parents=True)

    candidates = verification._targeted_go_candidates(tmp_path, ["internal/api/api.go"])

    assert candidates == [{"argv": ["go", "test", "./internal/api"],
                           "source": "build-metadata:go:internal/api",
                           "kind": "changed-scope", "scope": "affected",
                           "affectedUnit": "internal/api", "repairRoots": ["internal/api"],
                           "coveredPaths": ["internal/api/api.go"]}]


def test_targeted_python_candidates_scopes_repair_roots_to_covered_directories(tmp_path: Path):
    # Confirmed live: this was unconditionally `[]` regardless of nesting, which
    # `_worktree_guard` (workers/common/coding_agent.py) treats as "unrestricted,
    # whole worktree" -- harmless for a single flat repo, but wrong once a
    # repository accumulates independent nested projects, since a Python repair
    # agent then had no reason to ever be scoped to just its own scenario.
    nested = tmp_path / "features" / "widget"
    (nested / "tests").mkdir(parents=True)
    (nested / "reconcile.py").write_text("VALUE = 1\n")
    (nested / "tests" / "test_reconcile.py").write_text("def test_it():\n    pass\n")

    candidates = verification._targeted_python_candidates(
        tmp_path, ["features/widget/reconcile.py"])

    assert candidates == [{"argv": ["pytest", "features/widget/tests/test_reconcile.py"],
                           "source": "build-metadata:pytest:changed-files",
                           "kind": "changed-scope", "scope": "focused",
                           "affectedUnit": "pytest-files",
                           "repairRoots": ["features/widget", "features/widget/tests"],
                           "coveredPaths": ["features/widget/reconcile.py"]}]


def test_targeted_python_candidates_root_case_is_unchanged(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "test_app.py").write_text("def test_it():\n    pass\n")

    candidates = verification._targeted_python_candidates(tmp_path, ["app.py"])

    # "." (repo root) is dropped, matching the go/dotnet/cmake planners -- a
    # flat, non-nested project keeps the original unrestricted ([]) behavior.
    assert candidates[0]["repairRoots"] == ["tests"]


def test_looks_like_test_file_is_conservative():
    assert verification._looks_like_test_file("tests/test_app.py") is True
    assert verification._looks_like_test_file("spec/app_spec.rb") is True
    assert verification._looks_like_test_file("app.py") is False
    assert verification._looks_like_test_file("README.testing.md") is False


def test_validate_agent_argv_rejects_an_empty_or_malformed_proposal(tmp_path: Path):
    _python_repo(tmp_path)
    for junk in (None, [], "not a list", [None], ["a string"]):
        with pytest.raises(verification.VerificationBlocked):
            verification.validate_agent_argv(junk, root=tmp_path, changed_paths=["app.py"])


def test_validate_agent_argv_rejects_an_entrypoint_with_no_repository_evidence(tmp_path: Path):
    _python_repo(tmp_path)
    with pytest.raises(verification.VerificationBlocked, match="no evidence"):
        verification.validate_agent_argv(
            [{"argv": ["cargo", "test", "-p", "pkg"], "coveredPaths": ["app.py"]}],
            root=tmp_path, changed_paths=["app.py", "test_app.py"])


def test_validate_agent_argv_rejects_a_nonexistent_path_token(tmp_path: Path):
    _python_repo(tmp_path)
    with pytest.raises(verification.VerificationBlocked, match="nonexistent path"):
        verification.validate_agent_argv(
            [{"argv": ["pytest", "test_missing.py"], "coveredPaths": ["test_app.py"]}],
            root=tmp_path, changed_paths=["test_app.py"])


def test_validate_agent_argv_rejects_claimed_coverage_outside_the_diff(tmp_path: Path):
    _python_repo(tmp_path)
    (tmp_path / "other_test.py").write_text("def test_other():\n    pass\n")
    with pytest.raises(verification.VerificationBlocked, match="outside the candidate diff"):
        verification.validate_agent_argv(
            [{"argv": ["pytest", "test_app.py", "other_test.py"],
              "coveredPaths": ["test_app.py", "other_test.py"]}],
            root=tmp_path, changed_paths=["test_app.py"])


def test_validate_agent_argv_requires_every_changed_test_file_covered_when_targeted(tmp_path: Path):
    _python_repo(tmp_path)
    (tmp_path / "test_other.py").write_text("def test_other():\n    pass\n")
    with pytest.raises(verification.VerificationBlocked, match="omits changed test files"):
        verification.validate_agent_argv(
            [{"argv": ["pytest", "test_app.py"], "coveredPaths": ["test_app.py"]}],
            root=tmp_path, changed_paths=["test_app.py", "test_other.py"])


def test_validate_agent_argv_accepts_a_well_formed_targeted_proposal(tmp_path: Path):
    _python_repo(tmp_path)
    accepted = verification.validate_agent_argv(
        [{"argv": ["pytest", "test_app.py"], "coveredPaths": ["test_app.py"],
          "affectedUnit": "pytest-files", "repairRoots": []}],
        root=tmp_path, changed_paths=["test_app.py"])
    assert accepted[0]["argv"] == ["pytest", "test_app.py"]
    assert accepted[0]["source"] == "agent-proposal"
    assert accepted[0]["scope"] == "focused"


def test_validate_agent_argv_full_mode_skips_anti_omission_and_allows_repository_scope(tmp_path: Path):
    _python_repo(tmp_path)
    (tmp_path / "test_other.py").write_text("def test_other():\n    pass\n")
    # In full mode, a repository-wide command with no coveredPaths at all is
    # still accepted: full mode already means "the whole suite", so there is
    # nothing to omit.
    accepted = verification.validate_agent_argv(
        [{"argv": ["pytest"], "coveredPaths": []}],
        mode="full", root=tmp_path, changed_paths=["test_app.py", "test_other.py"])
    assert accepted[0]["scope"] == "repository"


def test_isolated_environment_keeps_new_toolchain_caches_off_the_host(tmp_path: Path):
    env = check_execution.isolated_environment(str(tmp_path))
    for key in ("NUGET_PACKAGES", "COMPOSER_HOME", "MIX_HOME", "HEX_HOME", "COURSIER_CACHE"):
        assert str(tmp_path) in env[key], key


# --- Values that used to be derived by one-line jq tasks ----------------------


def test_pr_commit_checks_names_a_pending_ci_state(monkeypatch):
    from gitops import tasks as gitops_tasks
    from conductor.client.http.models.task import Task

    def checks(_repo, sha, state):
        monkeypatch.setattr(gitops_tasks.github, "commit_checks",
                            lambda *_a, **_k: {"sha": sha, "checks": [], "links": [],
                                               "checkCount": 0, "verificationState": state})
        t = Task(); t.task_id = "t"; t.workflow_instance_id = "w"
        t.input_data = {"repo": "o/r", "sha": sha}
        return gitops_tasks.pr_commit_checks(t).output_data

    # Blank or "empty" means GitHub has not reported yet, which is a pending
    # poll rather than a verdict. A jq task used to draw this distinction.
    assert checks("o/r", "a" * 40, "")["ciState"] == "pending"
    assert checks("o/r", "a" * 40, "empty")["ciState"] == "pending"
    assert checks("o/r", "a" * 40, "passed")["ciState"] == "passed"
    assert checks("o/r", "a" * 40, "failed")["ciState"] == "failed"


def test_campaign_checkpoint_derives_its_own_blocking_verdict():
    from campaign.tasks import _blocking_passed

    # Four separate one-line jq tasks used to compute this input.
    assert _blocking_passed({"blockingStatus": "success"}) is True
    assert _blocking_passed({"blockingStatus": "failed"}) is False
    assert _blocking_passed({"blockingStatus": "success", "blockingValid": True}) is True
    assert _blocking_passed({"blockingStatus": "success", "blockingValid": False}) is False
    assert _blocking_passed({"blockingStatus": "success", "blockingIntegrated": False}) is False
    # No signals at all stays permissive, and an explicit value still wins.
    assert _blocking_passed({}) is True
    assert _blocking_passed({"blockingPassed": False, "blockingStatus": "success"}) is False


def test_campaign_validate_plan_publishes_the_write_scope_it_authorizes():
    from campaign.tasks import validate_plan

    result = validate_plan({"tasks": [
        {"id": "a", "title": "A", "description": "d", "files": ["src/a.py"], "dependsOn": []},
        {"id": "b", "title": "B", "description": "d", "files": ["src/b.py", "src/a.py"],
         "dependsOn": []},
    ]}, max_tasks=25)
    scope = sorted({p for t in result["tasks"] for p in t["files"]})
    assert scope == ["src/a.py", "src/b.py"]


def test_merge_worktrees_names_its_own_merge_state():
    from common import verification as _v  # noqa: F401  (module import guard)
    import inspect
    from gitops import tasks as gitops_tasks

    source = inspect.getsource(gitops_tasks.merge_worktrees)
    # Anything left unresolved means the merge did not complete; a jq task used
    # to derive this from the unresolved array.
    assert '"mergeState": "merged" if not unresolved else "conflicted"' in source


# --- allowAgentAuthoredTests: validate_authored_test_shape / discard_authored_test_attempt / verify_authored_test ---

def test_validate_authored_test_shape_rejects_multiple_touched_paths(tmp_git_repo: Path):
    (tmp_git_repo / "TestA.py").write_text("def test_a():\n    assert True\n")
    (tmp_git_repo / "TestB.py").write_text("def test_b():\n    assert True\n")
    candidate = git.head(str(tmp_git_repo))

    result = verification.validate_authored_test_shape(
        str(tmp_git_repo), candidate_commit=candidate, changed_paths=["module.py"])

    assert result["accepted"] is False
    assert "2 paths" in result["reason"]
    assert sorted(result["touchedPaths"]) == ["TestA.py", "TestB.py"]


def test_validate_authored_test_shape_rejects_a_modified_existing_file(tmp_git_repo: Path):
    (tmp_git_repo / "README.md").write_text("# tmp git repo\n\nmodified in place.\n")
    candidate = git.head(str(tmp_git_repo))

    result = verification.validate_authored_test_shape(
        str(tmp_git_repo), candidate_commit=candidate, changed_paths=[])

    assert result["accepted"] is False
    assert "modified an existing file" in result["reason"]
    assert result["touchedPaths"] == ["README.md"]


def test_validate_authored_test_shape_rejects_a_non_test_shaped_new_file(tmp_git_repo: Path):
    (tmp_git_repo / "helper.py").write_text("def helper():\n    return 1\n")
    candidate = git.head(str(tmp_git_repo))

    result = verification.validate_authored_test_shape(
        str(tmp_git_repo), candidate_commit=candidate, changed_paths=[])

    assert result["accepted"] is False
    assert "is not a test-shaped file" in result["reason"]
    assert result["touchedPaths"] == ["helper.py"]


def test_validate_authored_test_shape_rejects_a_file_already_present_at_baseline(tmp_git_repo: Path):
    (tmp_git_repo / "test_thing.py").write_text("def test_old():\n    assert True\n")
    _commit(tmp_git_repo, "baseline adds an existing test")
    (tmp_git_repo / "test_thing.py").unlink()
    candidate = _commit(tmp_git_repo, "candidate deletes it")
    # The agent's new attempt recreates the exact same path the baseline already had.
    (tmp_git_repo / "test_thing.py").write_text("def test_old():\n    assert True\n")

    result = verification.validate_authored_test_shape(
        str(tmp_git_repo), candidate_commit=candidate, changed_paths=[])

    assert result["accepted"] is False
    assert "already exists at the pre-candidate baseline" in result["reason"]


def test_validate_authored_test_shape_rejects_when_content_names_nothing_changed(tmp_git_repo: Path):
    (tmp_git_repo / "test_new.py").write_text("def test_something():\n    assert 1 == 1\n")
    candidate = git.head(str(tmp_git_repo))

    result = verification.validate_authored_test_shape(
        str(tmp_git_repo), candidate_commit=candidate, changed_paths=["module_under_change.py"])

    assert result["accepted"] is False
    assert "does not reference any changed file" in result["reason"]
    assert result["authoredPath"] == "test_new.py"


def test_validate_authored_test_shape_accepts_a_well_formed_new_test_referencing_the_changed_module(tmp_git_repo: Path):
    (tmp_git_repo / "test_widget.py").write_text(
        "import widget\n\n\ndef test_widget_behavior():\n    assert widget.value == 1\n")
    candidate = git.head(str(tmp_git_repo))

    result = verification.validate_authored_test_shape(
        str(tmp_git_repo), candidate_commit=candidate, changed_paths=["widget.py"])

    assert result["accepted"] is True
    assert result["reason"] == ""
    assert result["authoredPath"] == "test_widget.py"
    assert result["matchedIdentifier"] == "widget.py"


def test_discard_authored_test_attempt_removes_the_untracked_file_and_restores_a_modified_one(tmp_git_repo: Path):
    (tmp_git_repo / "test_new.py").write_text("def test_x():\n    assert True\n")
    (tmp_git_repo / "README.md").write_text("# tmp git repo\n\nmodified accidentally\n")

    result = verification.discard_authored_test_attempt(
        str(tmp_git_repo), ["test_new.py", "README.md"])

    assert result["discarded"] == ["README.md", "test_new.py"]
    assert not (tmp_git_repo / "test_new.py").exists()
    assert git.status_changes(str(tmp_git_repo)) == {}


_REAL_GRADLE_TEST = "#!/bin/sh\ntest \"$(cat value.txt)\" = good\n"
_VACUOUS_GRADLE_TEST = "#!/bin/sh\nexit 0\n"
_ALWAYS_FAILING_GRADLE_TEST = "#!/bin/sh\nexit 1\n"


def _author_test_commits(repo: Path, gradlew_script: str) -> tuple[str, str]:
    """Seed baseline -> pre_candidate_commit -> authored_commit for verify_authored_test tests.

    ``value.txt`` stands in for the real source change: "bad" at the baseline,
    "good" at the candidate. ``authored_commit`` adds one new test-shaped file
    on top of the candidate, changing nothing else -- exactly the shape
    validate_authored_test_shape would have already accepted.
    """
    (repo / "gradlew").write_text(gradlew_script)
    (repo / "value.txt").write_text("bad\n")
    _commit(repo, "baseline")
    (repo / "value.txt").write_text("good\n")
    pre_candidate = _commit(repo, "candidate fix")
    (repo / "NewTest.java").write_text("class NewTest {}\n")
    authored = _commit(repo, "agent-authored test")
    return pre_candidate, authored


def test_verify_authored_test_accepts_a_test_that_fails_at_baseline_and_passes_at_candidate(tmp_git_repo: Path):
    pre_candidate, authored = _author_test_commits(tmp_git_repo, _REAL_GRADLE_TEST)

    result = verification.verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, authored_commit=authored,
        commands=_focused_gradle_command())

    assert result["accepted"] is True
    assert result["reason"] == ""
    assert result["red"]["passed"] is False
    assert result["green"]["passed"] is True
    assert result["worktreeIsDisposable"] is True


def test_verify_authored_test_rejects_a_vacuous_test_that_passes_at_the_baseline(tmp_git_repo: Path):
    pre_candidate, authored = _author_test_commits(tmp_git_repo, _VACUOUS_GRADLE_TEST)

    result = verification.verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, authored_commit=authored,
        commands=_focused_gradle_command())

    assert result["accepted"] is False
    assert "vacuous" in result["reason"]
    assert result["red"]["passed"] is True
    assert result["green"]["passed"] is True


def test_verify_authored_test_rejects_when_green_still_fails(tmp_git_repo: Path):
    pre_candidate, authored = _author_test_commits(tmp_git_repo, _ALWAYS_FAILING_GRADLE_TEST)

    result = verification.verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, authored_commit=authored,
        commands=_focused_gradle_command())

    assert result["accepted"] is False
    assert "still fails against the candidate" in result["reason"]
    assert result["red"]["passed"] is False
    assert result["green"]["passed"] is False


def test_verify_authored_test_blocks_when_the_candidate_touched_no_non_test_path(tmp_git_repo: Path):
    (tmp_git_repo / "gradlew").write_text(_VACUOUS_GRADLE_TEST)
    _commit(tmp_git_repo, "baseline")
    (tmp_git_repo / "ExistingTest.java").write_text("class ExistingTest {}\n")
    pre_candidate = _commit(tmp_git_repo, "candidate touches only a test file")
    (tmp_git_repo / "NewTest.java").write_text("class NewTest {}\n")
    authored = _commit(tmp_git_repo, "agent-authored test")

    with pytest.raises(verification.VerificationBlocked, match="no non-test source path"):
        verification.verify_authored_test(
            str(tmp_git_repo), pre_candidate_commit=pre_candidate, authored_commit=authored,
            commands=_focused_gradle_command())


def test_verify_authored_test_reuses_one_disposable_checkout_for_both_red_and_green(
        monkeypatch: pytest.MonkeyPatch, tmp_git_repo: Path):
    pre_candidate, authored = _author_test_commits(tmp_git_repo, _REAL_GRADLE_TEST)
    calls: list[str] = []
    real_checkout = verification._disposable_checkout

    def counting(*args, **kwargs):
        calls.append(args[-1] if args else kwargs.get("commit"))
        return real_checkout(*args, **kwargs)

    monkeypatch.setattr(verification, "_disposable_checkout", counting)

    result = verification.verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, authored_commit=authored,
        commands=_focused_gradle_command())

    assert result["accepted"] is True
    assert len(calls) == 1


def test_verify_authored_test_never_mutates_the_source_repository_refs(tmp_git_repo: Path):
    escape_script = ("#!/bin/sh\ngit update-ref refs/heads/verifier-escape HEAD\n"
                     "test \"$(cat value.txt)\" = good\n")
    pre_candidate, authored = _author_test_commits(tmp_git_repo, escape_script)

    result = verification.verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, authored_commit=authored,
        commands=_focused_gradle_command())

    assert result["accepted"] is True
    assert result["gitMetadataIsIsolated"] is True
    assert result["sourceRemoteDetached"] is True
    assert git.git(str(tmp_git_repo), "rev-parse", "refs/heads/verifier-escape", check=False).code != 0


# --- commit_and_verify_authored_test: commit + discover + redgreen in one call ---
#
# Replaces what used to be three Conductor tasks (commit, test_discover,
# test_authored_test_redgreen) plus a SWITCH between them. These tests exercise
# real discovery (not the hand-built _focused_gradle_command()), so they use a
# flat Python layout -- module.py and test_module.py as repo-root siblings --
# which pytest imports without any packaging/sys.path setup.

def _python_author_commits(repo: Path, module_bad: str, module_good: str) -> str:
    """Seed baseline (module=bad) -> pre_candidate (module=good). Returns
    pre_candidate_commit; the caller writes the uncommitted authored file."""
    (repo / "module.py").write_text(module_bad)
    _commit(repo, "baseline")
    (repo / "module.py").write_text(module_good)
    return _commit(repo, "candidate fix")


def test_commit_and_verify_authored_test_accepts_a_real_test(tmp_git_repo: Path):
    pre_candidate = _python_author_commits(
        tmp_git_repo, "def value():\n    return 'bad'\n", "def value():\n    return 'good'\n")
    (tmp_git_repo / "test_module.py").write_text(
        "import module\n\n\ndef test_value():\n    assert module.value() == 'good'\n")

    result = verification.commit_and_verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, message="author test")

    assert result["accepted"] is True
    assert result["reason"] == ""
    assert result["commit"] != pre_candidate
    assert git.head(str(tmp_git_repo)) == result["commit"]
    assert result["commands"][0]["argv"] == ["pytest", "test_module.py"]
    assert result["red"]["passed"] is False
    assert result["green"]["passed"] is True


def test_commit_and_verify_authored_test_rolls_back_a_vacuous_test(tmp_git_repo: Path):
    pre_candidate = _python_author_commits(
        tmp_git_repo, "def value():\n    return 'bad'\n", "def value():\n    return 'good'\n")
    (tmp_git_repo / "test_module.py").write_text("def test_always_passes():\n    assert True\n")

    result = verification.commit_and_verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, message="author test")

    assert result["accepted"] is False
    assert "vacuous" in result["reason"]
    assert result["commit"] == pre_candidate
    # Rolled back -- HEAD is exactly the pre-candidate commit, so an unproven
    # test file never rides along in the delivered diff.
    assert git.head(str(tmp_git_repo)) == pre_candidate
    assert not (tmp_git_repo / "test_module.py").exists()


def test_commit_and_verify_authored_test_rolls_back_when_discovery_cannot_resolve_the_file(tmp_git_repo: Path):
    pre_candidate = _python_author_commits(
        tmp_git_repo, "def value():\n    return 'bad'\n", "def value():\n    return 'good'\n")
    (tmp_git_repo / "notes.py").write_text("NOTE = 'not a test'\n")

    result = verification.commit_and_verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, message="author test")

    assert result["accepted"] is False
    assert "could not be resolved to a runnable command" in result["reason"]
    assert result["commit"] == pre_candidate
    assert git.head(str(tmp_git_repo)) == pre_candidate


def test_commit_and_verify_authored_test_handles_a_noop_commit(tmp_git_repo: Path):
    pre_candidate = git.head(str(tmp_git_repo))

    result = verification.commit_and_verify_authored_test(
        str(tmp_git_repo), pre_candidate_commit=pre_candidate, message="author test")

    assert result["accepted"] is False
    assert "no committable change" in result["reason"]
    assert result["commit"] == pre_candidate
