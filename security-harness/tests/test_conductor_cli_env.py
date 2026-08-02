from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq required for assess CLI test")


ROOT = Path(__file__).resolve().parents[1]
ENV_HELPER = ROOT / "conductor" / "env.sh"
ENSURE_STACK = ROOT / "conductor" / "ensure-stack.sh"


def _run_bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], env=env, text=True, capture_output=True, check=False
    )


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\nset -u\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _base_env(tmp_path: Path, conductor_body: str, curl_body: str) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    _executable(fake_bin / "conductor", conductor_body)
    _executable(fake_bin / "curl", curl_body)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "CALL_LOG": str(call_log),
        "SC_REPO_ROOT": str(ROOT),
        "SC_CONDUCTOR_ENV_LOADED": "1",
    })
    for key in (
        "CONDUCTOR_AUTH_KEY",
        "CONDUCTOR_AUTH_SECRET",
        "CONDUCTOR_AUTH_TOKEN",
        "CONDUCTOR_SERVER_TYPE",
    ):
        env.pop(key, None)
    return env, call_log


def test_explicit_environment_overrides_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        "CONDUCTOR_SERVER_URL=http://dotenv.example/api\n"
        "CONDUCTOR_AUTH_KEY=dotenv-key\n"
        "CONDUCTOR_AUTH_SECRET=dotenv-secret\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "ENV_HELPER": str(ENV_HELPER),
        "ENV_ROOT": str(tmp_path),
        "CONDUCTOR_SERVER_URL": "https://enterprise.example/api",
        "CONDUCTOR_AUTH_KEY": "process-key",
        "CONDUCTOR_AUTH_SECRET": "process-secret",
    })

    result = _run_bash(
        'source "$ENV_HELPER"; sc_load_conductor_environment "$ENV_ROOT"; '
        'printf "%s|%s|%s" "$CONDUCTOR_SERVER_URL" '
        '"${CONDUCTOR_AUTH_KEY:+key-set}" "${CONDUCTOR_AUTH_SECRET:+secret-set}"',
        env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "https://enterprise.example/api|key-set|secret-set"


@pytest.mark.parametrize("status", ["401", "403"])
def test_reachable_auth_failure_never_starts_local_oss(tmp_path, status):
    env, call_log = _base_env(
        tmp_path,
        'printf "%s\\n" "$*" >> "$CALL_LOG"\nexit 1\n',
        f"printf '{status}'\n",
    )
    env["CONDUCTOR_SERVER_URL"] = "http://localhost:8080/api"
    env["ENSURE_STACK"] = str(ENSURE_STACK)

    result = _run_bash('source "$ENSURE_STACK"; _sc_ensure_server', env)

    assert result.returncode != 0
    assert "authentication/authorization failed" in result.stderr
    assert "server start" not in call_log.read_text(encoding="utf-8")


def test_unreachable_authenticated_server_never_starts_local_oss(tmp_path):
    env, call_log = _base_env(
        tmp_path,
        'printf "%s\\n" "$*" >> "$CALL_LOG"\nexit 1\n',
        "exit 7\n",
    )
    env.update({
        "CONDUCTOR_SERVER_URL": "http://localhost:8080/api",
        "CONDUCTOR_AUTH_KEY": "test-key",
        "CONDUCTOR_AUTH_SECRET": "test-secret",
        "ENSURE_STACK": str(ENSURE_STACK),
    })

    result = _run_bash('source "$ENSURE_STACK"; _sc_ensure_server', env)

    assert result.returncode != 0
    assert "Refusing to start a local OSS server" in result.stderr
    assert "server start" not in call_log.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", [
    "scan",
    "assess",
    "sast",
    "conductor/register.sh",
    "conductor/schedule.sh",
    "conductor/rag-setup.sh",
    "conductor/ensure-stack.sh",
])
def test_conductor_cli_entrypoints_use_shared_environment(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    assert "env.sh" in text
    assert "sc_load_conductor_environment" in text


# --- OOB preflight gate (item #1): a capability>=2 campaign cannot CONFIRM blind SSRF/RCE/exfil
# without an out-of-band collaborator, so it must refuse rather than emit a meaningless
# "0 runtime-confirmed" report. --allow-no-oob is the explicit opt-out; read-only runs are exempt.

def _run_assess(tmp_path: Path, args: list[str]) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    call_log = tmp_path / "assess_calls.log"
    _executable(
        fake_bin / "conductor",
        'printf "%s\\n" "$*" >> "$CALL_LOG"\n'
        'case "$*" in *"workflow start"*) printf \'{"workflowId":"wf-test"}\' ;; esac\n',
    )
    _executable(fake_bin / "curl", 'printf "200"\n')
    _executable(fake_bin / "docker", "exit 0\n")  # codeexec sandbox image reported present
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "CALL_LOG": str(call_log),
        "SC_REPO_ROOT": str(ROOT),
        "SC_CONDUCTOR_ENV_LOADED": "1",
        "CONDUCTOR_SERVER_URL": "http://localhost:8080/api",
        "SC_OOB_STATE_FILE": str(tmp_path / "no-oob.json"),  # absent -> no collaborator detected
    })
    result = subprocess.run(
        ["bash", str(ROOT / "assess"), "http://target.example", *args],
        env=env, text=True, capture_output=True, check=False,
    )
    return result, call_log


_OOB_REFUSAL = "Blind SSRF/RCE/exfil cannot"


@_needs_jq
def test_capability2_without_oob_is_refused(tmp_path):
    result, call_log = _run_assess(
        tmp_path, ["--authorized", "--capability", "2", "--no-bootstrap", "--no-wait"])
    assert result.returncode == 1
    assert _OOB_REFUSAL in result.stderr
    # Gate exits before `conductor` is ever executed, so the call log may not exist at all.
    log_text = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "workflow start" not in log_text


@_needs_jq
def test_capability2_without_oob_allowed_with_flag(tmp_path):
    _, call_log = _run_assess(
        tmp_path,
        ["--authorized", "--capability", "2", "--no-bootstrap", "--no-wait", "--allow-no-oob"])
    # Gate passed: the campaign proceeded to start the workflow (downstream may fail on the stub).
    assert "workflow start" in call_log.read_text(encoding="utf-8")


@_needs_jq
def test_readonly_capability_needs_no_oob(tmp_path):
    _, call_log = _run_assess(tmp_path, ["--authorized", "--no-bootstrap", "--no-wait"])
    assert "workflow start" in call_log.read_text(encoding="utf-8")
