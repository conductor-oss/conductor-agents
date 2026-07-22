"""Bootstrap reachability/authentication decisions in run.sh."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ENV_HELPER = ROOT / "scripts" / "conductor_env.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\nset -eu\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _environment(tmp_path: Path, conductor_body: str, curl_body: str) -> tuple[dict, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    _executable(fake_bin / "conductor", conductor_body)
    _executable(fake_bin / "curl", curl_body)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["CALL_LOG"] = str(call_log)
    for key in (
        "CONDUCTOR_AUTH_KEY",
        "CONDUCTOR_AUTH_SECRET",
        "CONDUCTOR_AUTH_TOKEN",
        "CONDUCTOR_SERVER_TYPE",
    ):
        env.pop(key, None)
    return env, call_log


def _register(env: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "./run.sh", "register"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _load_environment(env: dict, env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$ENV_HELPER"; load_harness_environment "$ENV_FILE" || exit $?; '
            'printf "%s|%s|%s" "$CONDUCTOR_SERVER_URL" '
            '"${CONDUCTOR_AUTH_KEY:+key-set}" "${CONDUCTOR_AUTH_SECRET:+secret-set}"',
        ],
        env={**env, "ENV_HELPER": str(ENV_HELPER), "ENV_FILE": str(env_file)},
        text=True,
        capture_output=True,
        check=False,
    )


def test_explicit_conductor_environment_overrides_dotenv_defaults(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CONDUCTOR_SERVER_URL=http://dotenv.example/api\n"
        "CONDUCTOR_AUTH_KEY=dotenv-key\n"
        "CONDUCTOR_AUTH_SECRET=dotenv-secret\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "CONDUCTOR_SERVER_URL": "https://enterprise.example/api",
        "CONDUCTOR_AUTH_KEY": "process-key",
        "CONDUCTOR_AUTH_SECRET": "process-secret",
    })

    result = _load_environment(env, env_file)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "https://enterprise.example/api|key-set|secret-set"


@pytest.mark.parametrize("script", ["run.sh", "workers/register.sh", "workers/run_workers.sh"])
def test_shell_cli_entrypoints_use_shared_conductor_environment(script):
    assert "load_harness_environment" in (ROOT / script).read_text(encoding="utf-8")


@pytest.mark.parametrize("present,missing", [
    ("CONDUCTOR_AUTH_KEY", "CONDUCTOR_AUTH_SECRET"),
    ("CONDUCTOR_AUTH_SECRET", "CONDUCTOR_AUTH_KEY"),
])
def test_shell_environment_rejects_partial_credentials_without_exposing_values(
        tmp_path, present, missing):
    env = os.environ.copy()
    for key in ("CONDUCTOR_AUTH_KEY", "CONDUCTOR_AUTH_SECRET"):
        env.pop(key, None)
    env[present] = "do-not-render-this"

    result = _load_environment(env, tmp_path / "missing.env")

    assert result.returncode == 2
    assert missing in result.stderr
    assert "do-not-render-this" not in result.stderr


@pytest.mark.parametrize(("status", "label"), [("401", "Unauthorized"), ("403", "Forbidden")])
def test_reachable_auth_failure_never_starts_local_oss(tmp_path, status, label):
    env, call_log = _environment(
        tmp_path,
        f'printf "%s\\n" "$*" >> "$CALL_LOG"\nprintf "HTTP {status} {label}\\n" >&2\nexit 1\n',
        f"printf '{status}'\n",
    )
    env["CONDUCTOR_SERVER_URL"] = "http://localhost:8080/api"

    result = _register(env)

    assert result.returncode != 0
    assert "authentication/authorization failed" in result.stderr
    assert "server start" not in call_log.read_text(encoding="utf-8")


def test_unreachable_authenticated_server_never_starts_local_oss(tmp_path):
    env, call_log = _environment(
        tmp_path,
        'printf "%s\\n" "$*" >> "$CALL_LOG"\nprintf "connection refused\\n" >&2\nexit 1\n',
        "exit 7\n",
    )
    env.update({
        "CONDUCTOR_SERVER_URL": "http://localhost:8080/api",
        "CONDUCTOR_AUTH_KEY": "test-key",
        "CONDUCTOR_AUTH_SECRET": "test-secret",
    })

    result = _register(env)

    assert result.returncode != 0
    assert "Refusing to start a local OSS server" in result.stderr
    assert "server start" not in call_log.read_text(encoding="utf-8")


def test_unreachable_open_local_server_still_bootstraps_oss(tmp_path):
    state = tmp_path / "started"
    env, call_log = _environment(
        tmp_path,
        'printf "%s\\n" "$*" >> "$CALL_LOG"\n'
        'if [ "$*" = "workflow list" ] && [ ! -f "$SERVER_STATE" ]; then exit 1; fi\n'
        'if [ "$*" = "server start" ]; then touch "$SERVER_STATE"; fi\n'
        "exit 0\n",
        "exit 7\n",
    )
    env["CONDUCTOR_SERVER_URL"] = "http://localhost:8080/api"
    env["SERVER_STATE"] = str(state)

    result = _register(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "server start" in call_log.read_text(encoding="utf-8")


def test_cli_receives_server_and_key_secret_environment(tmp_path):
    env, call_log = _environment(
        tmp_path,
        'printf "%s|%s|%s|%s\\n" "$*" "$CONDUCTOR_SERVER_URL" '
        '"${CONDUCTOR_AUTH_KEY:+key-set}" "${CONDUCTOR_AUTH_SECRET:+secret-set}" '
        '>> "$CALL_LOG"\nexit 0\n',
        "printf '200'\n",
    )
    env.update({
        "CONDUCTOR_SERVER_URL": "https://enterprise.example/api",
        "CONDUCTOR_AUTH_KEY": "test-key",
        "CONDUCTOR_AUTH_SECRET": "test-secret",
    })

    result = _register(env)

    assert result.returncode == 0, result.stdout + result.stderr
    first = call_log.read_text(encoding="utf-8").splitlines()[0]
    assert first == (
        "workflow list|https://enterprise.example/api|key-set|secret-set"
    )
