"""The harness's browser scenario helper (common/browser_scenario.mjs via browser_service).

Run 8064c234: the frozen scenario check closed its browser context but never the connection to the shared
browser service, so after the scenario PASSED Node never exited and the delivery audit waited on it for
8 hours. The helper owns the connection, so a scenario's process always exits, with the scenario's verdict.
These run against the real browser service on this host and skip where it cannot start (no node/npm).
"""
from __future__ import annotations

import subprocess
import threading
import time

import pytest

from common import browser_service
from common.check_execution import execute, inherited_environment


@pytest.fixture(scope="module")
def browser_env():
    env = browser_service.ensure()
    if not env:
        pytest.skip("no browser service on this host")
    return env


def _run_check(tmp_path, body: str, env: dict, wait: float = 60):
    script = tmp_path / ".cc" / "checks" / "scenario.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", execute(
        ["node", ".cc/checks/scenario.mjs"], cwd=str(tmp_path), env=inherited_environment(env))), daemon=True)
    t.start()
    t.join(wait)
    return out.get("r")


_PAGE = "data:text/html,<button onclick=\"this.textContent='done'\">Go</button>"


def test_a_passing_scenario_exits_0_and_closes_the_connection(tmp_path, browser_env):
    assert browser_env["PLAYWRIGHT_SCENARIO"].endswith("scenario.mjs")
    started = time.monotonic()
    r = _run_check(tmp_path, f"""
const {{ scenario }} = await import(process.env.PLAYWRIGHT_SCENARIO);
await scenario(async ({{ page }}) => {{
  await page.goto({_PAGE!r});
  await page.getByRole('button', {{ name: 'Go' }}).click();
  await page.getByRole('button', {{ name: 'done' }}).waitFor();
  console.log('scenario passed');
}}, {{ viewport: {{ width: 1280, height: 800 }} }});
""", browser_env)
    assert r is not None, "the scenario process never exited"
    assert r.exit_code == 0 and "scenario passed" in r.stdout, (r.stdout, r.stderr)
    assert time.monotonic() - started < 30


def test_a_failing_scenario_exits_1_with_a_one_line_reason(tmp_path, browser_env):
    r = _run_check(tmp_path, f"""
const {{ scenario }} = await import(process.env.PLAYWRIGHT_SCENARIO);
await scenario(async ({{ page }}) => {{
  await page.goto({_PAGE!r});
  await page.getByRole('button', {{ name: 'Missing' }}).click({{ timeout: 1000 }});
}});
""", browser_env)
    assert r is not None, "the scenario process never exited"
    assert r.exit_code == 1 and "scenario failed:" in r.stderr


def test_a_scenario_that_connects_itself_and_leaves_the_connection_open_never_exits(tmp_path, browser_env):
    """The shape the helper replaces (run 8064c234): it passes, closes its context, and hangs."""
    script = tmp_path / "own_connect.mjs"
    script.write_text("""
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE + '/index.mjs');
const browser = await chromium.connect(process.env.PLAYWRIGHT_WS_ENDPOINT);
const context = await browser.newContext();
await (await context.newPage()).goto('about:blank');
await context.close();
console.log('scenario passed');
""")
    proc = subprocess.Popen(["node", str(script)], env=inherited_environment(browser_env),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(8)
    finally:
        proc.kill()
        proc.wait()


def test_the_planner_and_implementer_are_told_to_use_the_helper():
    from pathlib import Path
    prompts = Path(__file__).resolve().parents[1] / "defaults" / "prompts"
    for name in ("program_plan.md", "team_implement.md"):
        text = (prompts / name).read_text()
        assert "PLAYWRIGHT_SCENARIO" in text and "await chromium.connect(" not in text


_APP = """
const http = require('node:http');
const port = Number(process.env.APP_PORT);
setTimeout(() => http.createServer((q, r) => { r.setHeader('content-type', 'text/html');
  r.end('<nav><a href="/">User Directory</a></nav><h1>User Directory</h1>'); }).listen(port, '127.0.0.1'), 1500);
"""


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    import socket
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_the_helper_starts_the_app_waits_for_http_and_stops_it(tmp_path, browser_env):
    """Run 3e3991fc: readiness by a heading's text matched two elements, the retry loop swallowed the
    error, and a working app was reported not ready. The helper waits for an HTTP answer instead."""
    port = _free_port()
    (tmp_path / "app.cjs").write_text(_APP)
    (tmp_path / "start.sh").write_text("set -m\nnode app.cjs &\nwait\n")
    r = _run_check(tmp_path, f"""
const {{ scenario }} = await import(process.env.PLAYWRIGHT_SCENARIO);
await scenario(async ({{ page }}) => {{
  await page.getByRole('heading', {{ name: 'User Directory' }}).waitFor();
  console.log('scenario passed');
}}, {{ start: ['bash', 'start.sh'], env: {{ APP_PORT: '{port}' }}, url: 'http://127.0.0.1:{port}' }});
""", browser_env)
    assert r is not None and r.exit_code == 0 and "scenario passed" in r.stdout, (r and r.stderr)
    assert not _listening(port), "the app outlived the scenario"


def test_an_app_that_never_answers_fails_with_its_own_log(tmp_path, browser_env):
    (tmp_path / "start.sh").write_text("echo 'Port 18474 is already in use' >&2\nexit 3\n")
    r = _run_check(tmp_path, """
const { scenario } = await import(process.env.PLAYWRIGHT_SCENARIO);
await scenario(async () => {}, { start: ['bash', 'start.sh'], url: 'http://127.0.0.1:9' });
""", browser_env)
    assert r is not None and r.exit_code == 1
    assert "the app exited (code 3)" in r.stderr and "already in use" in r.stderr


def test_a_check_run_stops_what_it_started_even_outside_its_process_group(tmp_path):
    """A `set -m` launcher moves its server into a group of its own; the launch marker still finds it."""
    port = _free_port()
    (tmp_path / "app.cjs").write_text(_APP.replace("1500", "0"))
    (tmp_path / "leak.sh").write_text(f"set -m\nAPP_PORT={port} nohup node app.cjs >/dev/null 2>&1 &\n"
                                      f"for i in $(seq 1 50); do nc -z 127.0.0.1 {port} && exit 0; sleep 0.1; done\n")
    r = execute(["bash", "leak.sh"], cwd=str(tmp_path), env=inherited_environment())
    assert r.exit_code == 0
    assert not _listening(port), "a server the check left behind in another process group survived"


def test_an_agent_session_stops_what_it_left_running(tmp_path, monkeypatch):
    import asyncio
    import os
    import sys
    from common import check_execution, coding_agent
    port = _free_port()
    (tmp_path / "app.cjs").write_text(_APP.replace("1500", "0"))

    async def fake_session(*args, **kwargs):
        env = {**os.environ, "APP_PORT": str(port),
               check_execution.LAUNCH_MARKER: coding_agent._SESSION_MARKER.get()}
        subprocess.Popen(["node", str(tmp_path / "app.cjs")], env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if _listening(port):
                break
            time.sleep(0.1)
        assert _listening(port)
        return {"status": "success"}

    monkeypatch.setattr(coding_agent, "_run_session", fake_session)
    assert asyncio.run(coding_agent.run_coding_agent("p", worktree=str(tmp_path)))["status"] == "success"
    assert not _listening(port), "a server the session left running survived it"


def test_two_runs_of_one_scenario_at_once_each_get_their_own_ports(tmp_path, browser_env):
    """Fixed ports collided whenever two sessions or an evaluation started the app at once (run 3e3991fc)."""
    (tmp_path / "app.cjs").write_text(_APP.replace("1500", "300"))
    (tmp_path / "start.sh").write_text("exec node app.cjs\n")
    body = """
const { scenario } = await import(process.env.PLAYWRIGHT_SCENARIO);
await scenario(async ({ page, ports }) => {
  await page.getByRole('heading', { name: 'User Directory' }).waitFor();
  console.log('passed on ' + ports.APP_PORT);
}, { start: ['bash', 'start.sh'], ports: ['APP_PORT'], url: 'http://127.0.0.1:{APP_PORT}' });
"""
    results = {}
    def one(k):
        d = tmp_path / k
        d.mkdir()
        for f in ("app.cjs", "start.sh"):
            (d / f).write_text((tmp_path / f).read_text())
        results[k] = _run_check(d, body, browser_env)
    threads = [threading.Thread(target=one, args=(k,)) for k in ("a", "b")]
    [t.start() for t in threads]
    [t.join(90) for t in threads]
    assert all(r is not None and r.exit_code == 0 for r in results.values()), {k: r and r.stderr for k, r in results.items()}
    assert results["a"].stdout != results["b"].stdout
