from __future__ import annotations

import json
import os
import errno
import multiprocessing
import time
from pathlib import Path

import pytest

from campaign.checks import run_profile
from common import check_execution, verification
from common.exec import RunError, run
from common.polling import (configure_registered_workers, configure_sdk_polling,
                            registered_worker_guard_report)
from common.worker_lock import singleton_worker


def _fake_jdk(root: Path) -> Path:
    home = root / "jdk"
    java = home / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text("#!/bin/sh\necho 'openjdk version 21-test' >&2\n")
    java.chmod(0o755)
    return home


def test_isolated_check_environment_inherits_the_real_worker_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Deployment controls what is present (Docker env, local shell/config
    # files, sandbox-injected credentials) -- isolated_environment no longer
    # filters or redirects any of it, so a repository that needs an
    # authenticated package registry (or any other machine-level
    # configuration) just works, the same way it would for a human running
    # the same command by hand. required_env is accepted for call-site
    # compatibility but is now a no-op: everything is already inherited.
    monkeypatch.setenv("GH_TOKEN", "a-real-worker-credential")
    monkeypatch.setenv("CAMPAIGN_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("GRADLE_USER_HOME", "/real/home/.gradle")

    env = check_execution.isolated_environment(tmp_path, required_env=["CAMPAIGN_URL"])

    assert env["CAMPAIGN_URL"] == "http://127.0.0.1:9999"
    assert env["GH_TOKEN"] == "a-real-worker-credential"
    # GRADLE_USER_HOME (and every other tool's config/cache location) is left
    # exactly as the real environment set it -- never redirected into a
    # synthetic per-run directory, so ~/.gradle/gradle.properties-style
    # credentials resolve the same way they would outside the harness.
    assert env["GRADLE_USER_HOME"] == "/real/home/.gradle"


def test_shared_environment_resolves_java_without_interactive_java_home(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = _fake_jdk(tmp_path)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(check_execution, "candidate_jdk_homes", lambda _version="": [str(home)])

    env = check_execution.isolated_environment(tmp_path / "state")

    assert env["JAVA_HOME"] == str(home)
    assert env["PATH"].split(os.pathsep)[0] == str(home / "bin")
    assert check_execution.probe([str(home / "bin" / "java"), "-version"],
                                 cwd=str(tmp_path), env=env)["available"] is True


def test_worker_runtime_installation_covers_all_child_processes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = _fake_jdk(tmp_path)
    target = {"PATH": "/usr/bin:/bin"}
    monkeypatch.setattr(check_execution, "candidate_jdk_homes", lambda _version="": [str(home)])

    resolved = check_execution.install_runtime_environment(target)

    assert resolved == str(home)
    assert target["JAVA_HOME"] == str(home)
    assert target["PATH"].split(os.pathsep)[0] == str(home / "bin")


def test_worker_runtime_installation_does_not_depend_on_java(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(check_execution, "resolve_java_home", lambda _version=None: None)
    monkeypatch.setattr(check_execution, "runtime_bin_directories", lambda: ["/runtime/bin"])
    target = {"PATH": "/usr/bin:/bin"}

    assert check_execution.install_runtime_environment(target) is None
    assert target["PATH"].split(os.pathsep)[0] == "/runtime/bin"


def test_all_deterministic_repository_command_runners_use_the_shared_executor():
    """Prevent a new raw subprocess from bypassing runtime/environment setup."""
    root = Path(__file__).resolve().parents[1]
    runner_sources = [
        root / "openspecops" / "tasks.py",
        root / "campaign" / "checks.py",
        root / "verification" / "tasks.py",
        root / "common" / "verification.py",
    ]
    forbidden = ("subprocess.run(", "subprocess.Popen(", "subprocess.check_call(",
                 "subprocess.check_output(")

    for source in runner_sources:
        text = source.read_text(encoding="utf-8")
        assert not any(call in text for call in forbidden), source

    assert "check_execution.execute(" in (root / "openspecops" / "tasks.py").read_text()
    assert "check_execution.execute(" in (root / "campaign" / "checks.py").read_text()
    assert "verification.verify_candidate(" in (root / "verification" / "tasks.py").read_text()
    assert "result = run(command[\"argv\"]" in (root / "common" / "verification.py").read_text()


def test_every_production_subprocess_uses_a_shared_runtime_environment():
    """Architectural gate for every current and future command-launch surface."""
    root = Path(__file__).resolve().parents[1]
    allowed = {
        root / "common" / "check_execution.py",
        root / "common" / "codex.py",
        root / "common" / "gemini.py",
    }
    direct_launchers = []
    for source in root.rglob("*.py"):
        if "tests" in source.parts or ".venv" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        if any(call in text for call in ("subprocess.run(", "subprocess.Popen(",
                                         "subprocess.check_call(", "subprocess.check_output(")):
            direct_launchers.append(source)

    assert set(direct_launchers) == allowed
    for source in (root / "common" / "codex.py", root / "common" / "gemini.py"):
        assert "env=check_execution.inherited_environment()" in source.read_text(encoding="utf-8")


def test_runtime_health_contract_covers_required_language_families():
    # Required families gate runtime_health.healthy, which AGENTS.md uses as the
    # proof that a command-launch change is sound. Optional families are
    # reported so an operator can see what a host can verify, but a machine that
    # never builds .NET, PHP, or Elixir must not be declared unhealthy for
    # lacking toolchains it does not need.
    required = {name for name in check_execution._RUNTIME_FAMILIES
                if name not in check_execution._OPTIONAL_RUNTIME_FAMILIES}
    assert required == {"java", "go", "python", "ruby", "typescript"}
    assert check_execution._OPTIONAL_RUNTIME_FAMILIES == {"dotnet", "php", "elixir"}
    assert check_execution._OPTIONAL_RUNTIME_FAMILIES < set(check_execution._RUNTIME_FAMILIES)


def test_shared_executor_waits_for_command_completion_without_a_deadline(tmp_path: Path):
    env = check_execution.isolated_environment(tmp_path)
    started = time.monotonic()
    result = check_execution.execute(["sh", "-c", "sleep 0.2; printf done"], cwd=str(tmp_path), env=env)

    assert result.passed is True
    assert result.stdout == "done"
    assert time.monotonic() - started >= 0.2


def test_registered_workers_hold_sleep_inhibition_for_the_entire_call(monkeypatch: pytest.MonkeyPatch):
    from conductor.client.automator.task_handler import _decorated_functions

    observed = []

    def worker(_task):
        observed.append(check_execution.idle_sleep_inhibition_active())
        return "done"

    record = {"func": worker, "poll_timeout": 100, "poll_interval": 100}
    monkeypatch.setitem(_decorated_functions, ("sleep_inhibition_probe", ""), record)
    monkeypatch.setattr(check_execution.sys, "platform", "linux")

    configure_registered_workers()
    assert record["func"](object()) == "done"
    assert observed == [True]
    assert check_execution.idle_sleep_inhibition_active() is False
    assert record["lease_extend_enabled"] is True


def test_shared_executor_retries_transient_process_spawn_pressure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = check_execution.isolated_environment(tmp_path)
    original = check_execution.subprocess.Popen
    attempts = 0

    def pressured(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError(errno.EAGAIN, "resource temporarily unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(check_execution.subprocess, "Popen", pressured)
    result = check_execution.execute(["sh", "-c", "echo ready"], cwd=str(tmp_path), env=env)

    assert attempts == 3
    assert result.passed is True
    assert result.stdout.strip() == "ready"


def test_shared_executor_bounds_captured_output_without_bounding_runtime(tmp_path: Path):
    env = check_execution.isolated_environment(tmp_path)
    result = check_execution.execute(
        ["sh", "-c", "yes output | head -c 1500000"],
        cwd=str(tmp_path), env=env,
    )

    assert result.passed is True
    assert result.output_truncated is True
    assert len(result.stdout) <= 1_000_000


def test_common_exec_preserves_nonzero_exit_details_after_shared_executor_migration(tmp_path: Path):
    with pytest.raises(RunError) as caught:
        run(["sh", "-c", "echo broken >&2; exit 7"], cwd=str(tmp_path))

    assert caught.value.code == 7
    assert "broken" in caught.value.stderr


def test_campaign_uses_shared_isolated_environment_and_external_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A "none" mode profile still runs with the worker's real environment
    # (isolated_environment now inherits it unconditionally) -- "none" only
    # means this profile declares no extra requiredEnv/attached precondition,
    # not that the check runs env-free.
    monkeypatch.setenv("GH_TOKEN", "a-real-worker-credential")
    artifact_root = tmp_path / "harness-artifacts"
    monkeypatch.setenv("CONDUCTOR_ARTIFACT_ROOT", str(artifact_root))
    config = {
        "version": 2,
        "checks": {"unit": {"command": ["sh", "-c", "printf '%s' \"${GH_TOKEN-absent}\""], "blocking": True}},
        "profiles": {"wave": {"checks": ["unit"], "environment": {"mode": "none"}}},
        "defaults": {},
    }
    cfg = tmp_path / ".conductor-code" / "checks.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps(config))

    result = run_profile(str(tmp_path), "wave")

    assert result["executionOutcome"] == "passed"
    assert result["artifactDir"].startswith(str(artifact_root))
    assert not str(result["artifactDir"]).startswith(str(tmp_path / ".conductor-code"))
    assert result["checks"][0]["outputTail"] == "a-real-worker-credential"


def test_campaign_configuration_rejects_execution_timeouts(tmp_path: Path):
    config = {
        "version": 2,
        "checks": {"unit": {"command": ["sh", "-c", "printf done"], "timeoutSeconds": 0.1}},
        "profiles": {"wave": {"checks": ["unit"], "environment": {"mode": "none"}}},
        "defaults": {},
    }
    cfg = tmp_path / ".conductor-code" / "checks.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps(config))

    with pytest.raises(ValueError, match="must not set timeoutSeconds"):
        run_profile(str(tmp_path), "wave")


def test_runtime_requirements_read_repository_metadata(tmp_path: Path):
    (tmp_path / "build.gradle.kts").write_text("java { toolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }")
    (tmp_path / ".nvmrc").write_text("20\n")
    (tmp_path / ".python-version").write_text("3.12.2\n")
    (tmp_path / "go.mod").write_text("module example.com/test\n\ngo 1.23\n")
    (tmp_path / "rust-toolchain.toml").write_text("[toolchain]\nchannel = '1.82.0'\n")

    assert verification.runtime_requirements(tmp_path) == {
        "java": "21", "node": "20", "python": "3.12", "go": "1.23", "rust": "1.82.0",
    }


def test_node_engine_range_accepts_newer_compatible_runtime(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"engines":{"node":">=20"}}')

    requirements = verification.runtime_requirements(tmp_path)

    assert requirements["node"] == ">=20"
    assert verification._runtime_requirement_reason(
        ["npm", "test"], {"version": "v24.1.0"}, requirements
    ) is None


def test_sdk_batch_poll_uses_the_configured_long_poll_timeout(monkeypatch: pytest.MonkeyPatch):
    from conductor.client.http.api.task_resource_api import TaskResourceApi

    observed: dict[str, object] = {}

    def original(_self, tasktype, **kwargs):
        observed["tasktype"] = tasktype
        observed.update(kwargs)
        return []

    monkeypatch.setenv("CONDUCTOR_POLL_TIMEOUT_MS", "5000")
    monkeypatch.setattr(TaskResourceApi, "batch_poll", original)
    monkeypatch.setattr(TaskResourceApi, "_conductor_harness_poll_patch", False, raising=False)
    configure_sdk_polling()

    TaskResourceApi.batch_poll(object(), "coding_agent", count=8, timeout=100)

    assert observed == {"tasktype": "coding_agent", "count": 8, "timeout": 5000}


def test_every_registered_worker_renews_the_server_response_lease(monkeypatch: pytest.MonkeyPatch):
    from conductor.client.automator import task_handler

    def worker(_task):
        return None

    records = {
        ("one", None): {"func": worker, "poll_timeout": 100, "poll_interval": 100,
                        "lease_extend_enabled": False},
        ("two", None): {"func": worker, "poll_timeout": 2000, "poll_interval": 250,
                        "lease_extend_enabled": False},
    }
    monkeypatch.setattr(task_handler, "_decorated_functions", records)

    configure_registered_workers()

    assert all(record["lease_extend_enabled"] is True for record in records.values())
    assert all(getattr(record["func"], "_conductor_harness_sleep_inhibited", False)
               for record in records.values())
    report = registered_worker_guard_report()
    assert report["healthy"] is True
    assert report["registeredWorkers"] == report["guardedWorkers"] == 2


def test_identical_worker_deployments_cannot_take_two_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_DIR", str(tmp_path))
    with singleton_worker("http://localhost:8080/api", ["campaign", "gitops"]):
        with pytest.raises(RuntimeError, match="duplicate worker deployment refused"):
            with singleton_worker("http://localhost:8080/api", ["gitops", "campaign"]):
                pass


def test_overlapping_worker_deployments_cannot_duplicate_a_task_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_DIR", str(tmp_path))
    with singleton_worker("http://localhost:8080/api", ["campaign", "gitops"]):
        with pytest.raises(RuntimeError, match="module=campaign"):
            with singleton_worker("http://localhost:8080/api", ["campaign"]):
                pass


def test_forked_poller_child_does_not_keep_the_supervisor_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not hasattr(os, "register_at_fork"):
        pytest.skip("requires POSIX fork hooks")
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_DIR", str(tmp_path))

    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()

    def forked_poller(signal) -> None:
        signal.send("ready")
        signal.recv()
        try:
            with singleton_worker("http://localhost:8080/api", ["campaign"]):
                signal.send("acquired")
        except RuntimeError:
            signal.send("blocked")

    with singleton_worker("http://localhost:8080/api", ["campaign"]):
        process = context.Process(target=forked_poller, args=(child,))
        process.start()
        assert parent.recv() == "ready"
    # Parent release must be sufficient while the forked child remains alive.
    # This would be ``blocked`` if that child retained the inherited lock FD.
    parent.send("try")
    assert parent.recv() == "acquired"
    process.join()
    assert process.exitcode == 0
