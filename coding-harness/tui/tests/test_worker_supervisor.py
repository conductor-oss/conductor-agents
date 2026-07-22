"""TUI worker lifecycle tests without launching real pollers."""

from __future__ import annotations

import pytest

from tui.worker_supervisor import WorkerSupervisor


class FakeProcess:
    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.waited = False

    async def wait(self):
        self.waited = True
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_supervisor_forwards_selected_server_and_auth(monkeypatch, tmp_path):
    script = tmp_path / "workers" / "run_workers.sh"
    python = tmp_path / "workers" / ".venv" / "bin" / "python"
    script.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    python.write_text("")
    monkeypatch.setenv("CONDUCTOR_AUTH_KEY", "test-key")
    monkeypatch.setenv("CONDUCTOR_AUTH_SECRET", "test-secret")
    process = FakeProcess()
    captured = {}

    async def fake_subprocess(*argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return process

    supervisor = WorkerSupervisor(
        "http://selected/api", root=tmp_path, log_path=tmp_path / "workers.log"
    )
    monkeypatch.setattr("tui.worker_supervisor.asyncio.create_subprocess_exec", fake_subprocess)
    killed = []
    monkeypatch.setattr("tui.worker_supervisor.os.killpg", lambda pid, sig: killed.append((pid, sig)))

    assert await supervisor.start()
    env = captured["kwargs"]["env"]
    assert env["CONDUCTOR_SERVER_URL"] == "http://selected/api"
    assert env["CONDUCTOR_AUTH_KEY"] == "test-key"
    assert env["CONDUCTOR_AUTH_SECRET"] == "test-secret"
    assert captured["kwargs"]["start_new_session"] is True

    await supervisor.stop()
    assert killed and killed[0][0] == process.pid
    assert process.waited


@pytest.mark.asyncio
async def test_supervisor_reports_missing_worker_environment(tmp_path):
    script = tmp_path / "workers" / "run_workers.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    supervisor = WorkerSupervisor("http://selected/api", root=tmp_path)

    assert not await supervisor.start()
    assert "./run.sh setup" in (supervisor.last_error or "")
