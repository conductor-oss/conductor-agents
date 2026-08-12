"""Version-2 profile-driven check runner used by campaign checkpoints."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from common import check_execution


class ChecksConfigError(ValueError):
    pass


def load_config(repo: str, relpath: str = ".conductor-code/checks.json") -> dict[str, Any]:
    path = Path(repo) / relpath
    if not path.is_file():
        return {"version": 2, "checks": {}, "profiles": {},
                "defaults": {"waveProfile": "", "finalProfile": ""}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 2:
        raise ChecksConfigError(f"{relpath} must use version 2")
    checks = data.get("checks") or {}
    profiles = data.get("profiles") or {}
    if not isinstance(checks, dict) or not isinstance(profiles, dict):
        raise ChecksConfigError("checks and profiles must be objects")
    for check_id, check in checks.items():
        if not isinstance(check, dict):
            raise ChecksConfigError(f"check {check_id} must be an object")
        if any(key in check for key in ("env", "environmentVariables", "secrets", "credentials")):
            raise ChecksConfigError(
                f"check {check_id} must not contain credential values; use worker environment variables")
        if "timeoutSeconds" in check:
            raise ChecksConfigError(
                f"check {check_id} must not set timeoutSeconds; commands run until completion or cancellation")
    for name, profile in profiles.items():
        selected = profile.get("checks") or []
        missing = sorted(set(selected) - set(checks))
        if missing:
            raise ChecksConfigError(f"profile {name} references unknown checks: {', '.join(missing)}")
        mode = (profile.get("environment") or {}).get("mode", "none")
        if mode not in ("none", "managed", "attached"):
            raise ChecksConfigError(f"profile {name} has invalid environment mode {mode!r}")
        if mode == "managed":
            env = profile.get("environment") or {}
            if "timeoutSeconds" in env:
                raise ChecksConfigError(
                    f"managed profile {name} must not set timeoutSeconds; commands run until completion or cancellation")
            for key in ("up", "readyCheck", "down"):
                if not env.get(key):
                    raise ChecksConfigError(f"managed profile {name} requires environment.{key}")
        if mode == "attached":
            env = profile.get("environment") or {}
            if env.get("down"):
                raise ChecksConfigError(f"attached profile {name} must never define teardown")
            required = env.get("requiredEnv") or []
            if any(not isinstance(item, str) or not item.isidentifier() or item.upper() != item
                   for item in required):
                raise ChecksConfigError(
                    f"attached profile {name} requiredEnv must contain environment-variable names only")
    return data


def _argv(command: Any) -> list[str]:
    if isinstance(command, list) and all(isinstance(x, str) for x in command):
        return command
    if isinstance(command, str) and command.strip():
        return shlex.split(command)
    raise ChecksConfigError("check command must be a string or argv array")


def _safe_output(value: str, limit: int = 12_000) -> str:
    return "".join(char for char in str(value or "")
                   if char in {"\n", "\r", "\t"} or ord(char) >= 32)[-limit:]


def _run(command: Any, *, cwd: str, log_path: Path, tail_chars: int,
         env: dict[str, str]) -> dict[str, Any]:
    argv = _argv(command)
    try:
        proc = check_execution.execute(argv, cwd=cwd, env=env)
        text = _safe_output((proc.stdout or "") + ("\n" if proc.stdout and proc.stderr else "") + (proc.stderr or ""))
        outcome = proc.outcome
        exit_code = proc.exit_code
        duration = proc.duration_seconds
    except OSError as exc:
        text = _safe_output(f"unable to start {' '.join(argv)}: {exc}")
        outcome, exit_code, duration = "infra_blocked", 127, 0.0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text, encoding="utf-8")
    return {"passed": outcome == "passed", "outcome": outcome, "exitCode": exit_code,
            "durationSeconds": duration, "outputTail": text[-tail_chars:], "logPath": str(log_path),
            "runtime": {"entrypoint": argv[0], "executable": check_execution.executable_for(argv, cwd=cwd, env=env) or ""}}


def run_profile(repo: str, profile_name: str, *, requested: list[str] | None = None,
                config_path: str = ".conductor-code/checks.json", attached_confirmed: bool = False,
                tail_chars: int = 8000) -> dict[str, Any]:
    config = load_config(repo, config_path)
    profiles, definitions = config["profiles"], config["checks"]
    if profile_name not in profiles:
        raise ChecksConfigError(f"unknown check profile {profile_name!r}")
    profile = profiles[profile_name]
    selected = list(requested or profile.get("checks") or [])
    unknown = sorted(set(selected) - set(profile.get("checks") or []))
    if unknown:
        raise ChecksConfigError(f"requested checks are not in profile {profile_name}: {', '.join(unknown)}")

    environment = profile.get("environment") or {"mode": "none"}
    mode = environment.get("mode", "none")
    required = environment.get("requiredEnv") or []
    missing_env = [name for name in required if not os.environ.get(name)]
    if missing_env:
        return {"passed": False, "blockingPassed": False, "executionOutcome": "configuration_blocked", "profile": profile_name,
                "environmentMode": mode, "missingEnvironmentVariables": missing_env,
                "checks": [], "teardownRan": False,
                "error": "required attached environment variables are missing"}
    if mode == "attached" and not attached_confirmed:
        return {"passed": False, "blockingPassed": False, "executionOutcome": "configuration_blocked", "profile": profile_name,
                "environmentMode": mode, "confirmationRequired": True, "checks": [],
                "teardownRan": False, "error": "fresh HUMAN confirmation required"}

    artifact_root = Path(os.environ.get("CONDUCTOR_ARTIFACT_ROOT", tempfile.gettempdir())) / "conductor-check-artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = Path(tempfile.mkdtemp(prefix="campaign-", dir=artifact_root))
    state_dir = artifact_dir / "state"
    execution_env = check_execution.isolated_environment(state_dir, required_env=required)
    results: list[dict[str, Any]] = []
    teardown_ran = False
    setup_error = ""
    setup_result: dict[str, Any] = {}
    try:
        if mode == "managed":
            up = _run(environment["up"], cwd=repo, log_path=artifact_dir / "environment-up.log",
                      tail_chars=tail_chars, env=execution_env)
            setup_result["setup"] = up
            if not up["passed"]:
                setup_error = "managed environment up command failed"
            else:
                ready = _run(environment["readyCheck"], cwd=repo,
                             log_path=artifact_dir / "environment-ready.log", tail_chars=tail_chars,
                             env=execution_env)
                setup_result["readyCheck"] = ready
                if not ready["passed"]:
                    setup_error = "managed environment readiness check failed"
        elif mode == "attached" and environment.get("readyCheck"):
            ready = _run(environment["readyCheck"], cwd=repo,
                         log_path=artifact_dir / "environment-ready.log", tail_chars=tail_chars,
                         env=execution_env)
            setup_result["readyCheck"] = ready
            if not ready["passed"]:
                setup_error = "attached environment readiness check failed"

        for check_id in ([] if setup_error else selected):
            spec = definitions[check_id]
            attempts = max(1, int(spec.get("retries", 0)) + 1)
            attempt_results = []
            for attempt in range(1, attempts + 1):
                result = _run(spec.get("command", spec.get("cmd")),
                              cwd=str(Path(repo) / spec.get("cwd", ".")),
                              log_path=artifact_dir / f"{check_id}-{attempt}.log",
                              tail_chars=tail_chars, env=execution_env)
                result["attempt"] = attempt
                attempt_results.append(result)
                if result["passed"]:
                    break
            last = attempt_results[-1]
            results.append({"id": check_id, "blocking": bool(spec.get("blocking", True)),
                            "passed": bool(last["passed"]), "attempts": attempt_results,
                            "exitCode": last["exitCode"],
                            "outcome": last["outcome"], "runtime": last["runtime"],
                            "durationSeconds": round(sum(a["durationSeconds"] for a in attempt_results), 3),
                            "outputTail": last["outputTail"], "logPath": last["logPath"]})
    finally:
        # Attached environments are owned by the user and are never torn down.
        if mode == "managed":
            teardown_ran = True
            _run(environment["down"], cwd=repo, log_path=artifact_dir / "environment-down.log",
                 tail_chars=tail_chars, env=execution_env)

    blocking_passed = not setup_error and all(r["passed"] or not r["blocking"] for r in results)
    outcome = "passed" if not setup_error and all(r["passed"] for r in results) else next(
        (r["outcome"] for r in results if r.get("outcome") in {"infra_blocked", "cancelled"}),
        "configuration_blocked" if setup_error else "code_failed",
    )
    return {"passed": not setup_error and all(r["passed"] for r in results), "blockingPassed": blocking_passed,
            "executionOutcome": outcome,
            "profile": profile_name, "environmentMode": mode, "checks": results,
            "teardownRan": teardown_ran, "artifactDir": str(artifact_dir), "error": setup_error,
            **setup_result}
