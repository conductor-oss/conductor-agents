"""The progress rule (check_execution.execute(stall_after_s=...)), on real processes.

Run 169589fd: the architect's scenario dropped a batched Postgres reply and waited forever; the harness
ran it with no limit and the run sat 736 s until a human stopped the server. A stalled command is one
that writes nothing and uses next to no CPU for the window: it is stopped and reported, while work that
is slow but alive is never touched.
"""
from __future__ import annotations

import socket
import time

from common.check_execution import execute, inherited_environment

_BLOCKED = """
const net = require('node:net');
const server = net.createServer(sock => sock.on('data', () => {})).listen(0, '127.0.0.1', () => {
  const c = net.createConnection(server.address().port, '127.0.0.1', () => c.write('hello'));
  c.on('data', () => process.exit(0));   // the reply never comes
});
"""


def _run(tmp_path, script: str, name: str, window: float):
    (tmp_path / name).write_text(script)
    started = time.monotonic()
    r = execute(["node", name], cwd=str(tmp_path), env=inherited_environment(), stall_after_s=window)
    return r, time.monotonic() - started


def test_a_command_blocked_on_a_reply_that_never_comes_is_stopped_and_reported(tmp_path):
    r, took = _run(tmp_path, _BLOCKED, "blocked.cjs", 3)
    assert r.stalled and r.exit_code != 0 and "no output and next to no CPU" in r.stderr
    assert took < 20


def test_busy_work_longer_than_the_window_is_never_stopped(tmp_path):
    r, took = _run(tmp_path, "const end = Date.now() + 6000; while (Date.now() < end) {} console.log('built');",
                   "busy.cjs", 2)
    assert not r.stalled and r.exit_code == 0 and "built" in r.stdout and took >= 6


def test_quiet_work_that_keeps_printing_is_never_stopped(tmp_path):
    r, _ = _run(tmp_path, "let n = 0; const t = setInterval(() => { console.log('step', ++n); "
                          "if (n === 6) { clearInterval(t); } }, 1000);", "printing.cjs", 2.5)
    assert not r.stalled and r.exit_code == 0 and "step 6" in r.stdout


def test_a_stalled_check_takes_the_server_it_started_down_with_it(tmp_path):
    """The server runs in its own process group (`set -m`), as a launcher's does."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (tmp_path / "server.cjs").write_text(
        "require('node:net').createServer(() => {}).listen(%d, '127.0.0.1');" % port)
    (tmp_path / "check.sh").write_text("set -m\nnode server.cjs &\nsleep 1\nexec node blocked.cjs\n")
    (tmp_path / "blocked.cjs").write_text(_BLOCKED)
    r = execute(["bash", "check.sh"], cwd=str(tmp_path), env=inherited_environment(), stall_after_s=3)
    assert r.stalled
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0, "the server the stalled check started is still up"


def test_without_the_rule_nothing_changes(tmp_path):
    r = execute(["node", "-e", "console.log('ok')"], cwd=str(tmp_path), env=inherited_environment())
    assert r.exit_code == 0 and not r.stalled
