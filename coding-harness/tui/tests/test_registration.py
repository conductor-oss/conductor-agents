"""Workflow-registration runner tests (no live Conductor server)."""

from __future__ import annotations

import os

import pytest

from tui import registration


@pytest.fixture(autouse=True)
def _clear_conductor_auth(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_AUTH_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_AUTH_SECRET", raising=False)


@pytest.mark.asyncio
async def test_register_definitions_passes_selected_server(monkeypatch, tmp_path):
    script = tmp_path / "register.sh"
    script.write_text('#!/bin/bash\necho "server=$CONDUCTOR_SERVER_URL"\n')
    script.chmod(0o755)
    monkeypatch.setattr(registration, "script_path", lambda: script)
    monkeypatch.setenv("CONDUCTOR_SERVER_URL", "http://wrong/api")

    result = await registration.register_definitions("http://selected/api")

    assert result.ok
    assert result.output == "server=http://selected/api"
    assert os.environ["CONDUCTOR_SERVER_URL"] == "http://wrong/api"


@pytest.mark.asyncio
async def test_register_definitions_reports_script_failure(monkeypatch, tmp_path):
    script = tmp_path / "register.sh"
    script.write_text('#!/bin/bash\necho "registration broke"\nexit 7\n')
    script.chmod(0o755)
    monkeypatch.setattr(registration, "script_path", lambda: script)

    result = await registration.register_definitions("http://selected/api")

    assert not result.ok
    assert "registration broke" in result.output


@pytest.mark.asyncio
async def test_register_definitions_forwards_auth_pair(monkeypatch, tmp_path):
    script = tmp_path / "register.sh"
    script.write_text(
        "#!/bin/bash\n"
        "if [[ -n \"$CONDUCTOR_AUTH_KEY\" && -n \"$CONDUCTOR_AUTH_SECRET\" ]]; then\n"
        "  echo 'auth=present'\n"
        "else\n"
        "  echo 'auth=missing'\n"
        "  exit 2\n"
        "fi\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(registration, "script_path", lambda: script)
    monkeypatch.setenv("CONDUCTOR_AUTH_KEY", "test-key")
    monkeypatch.setenv("CONDUCTOR_AUTH_SECRET", "test-secret")

    result = await registration.register_definitions("http://selected/api")

    assert result.ok
    assert result.output == "auth=present"


@pytest.mark.asyncio
async def test_register_definitions_rejects_partial_auth(monkeypatch, tmp_path):
    script = tmp_path / "register.sh"
    script.write_text("#!/bin/bash\necho should-not-run\n")
    script.chmod(0o755)
    monkeypatch.setattr(registration, "script_path", lambda: script)
    monkeypatch.setenv("CONDUCTOR_AUTH_SECRET", "do-not-render-this")

    result = await registration.register_definitions("http://selected/api")

    assert not result.ok
    assert "CONDUCTOR_AUTH_KEY" in result.output
    assert "do-not-render-this" not in result.output
    assert "should-not-run" not in result.output
