"""Workflow-registration runner tests (no live Conductor server)."""

from __future__ import annotations

import os

import pytest

from tui import registration


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
