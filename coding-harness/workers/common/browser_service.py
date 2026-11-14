"""One browser for every session and check on this host, as a service over loopback.

The agent sandbox cannot launch a browser (Chromium's Mach ports are denied), so a frozen browser
scenario could not be run by the implementer who had to make it pass; the harness ran it after the
fact and sent failures back, a loop of patches over one mismatch (runs 806e18ba to 7601683b). The
mismatch goes away when the browser is a service: Playwright's browser server runs on the host, the
sandbox allows loopback, and the scenario connects instead of launching. Every session and every check
run gets ``PLAYWRIGHT_MODULE`` (the Playwright library to import) and ``PLAYWRIGHT_WS_ENDPOINT`` (the
server) in its environment, so the implementer runs the scenario itself like any other command.
Installed once per host under ``~/.cache/conductor-harness/playwright``; one server process per host,
started by whichever worker first needs it and reused while it lives.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import time
from urllib.parse import urlparse

from . import check_execution
from .exec import run

HOME = os.environ.get("CONDUCTOR_PLAYWRIGHT_HOME") or os.path.expanduser("~/.cache/conductor-harness/playwright")
MODULE = os.path.join(HOME, "node_modules", "playwright")
STATE = os.path.join(HOME, "server.json")
# The scenario helper (browser_scenario.mjs) lives beside Playwright so the sandbox can read it: it owns
# the connection's lifecycle, so a scenario always lets its process exit.
SCENARIO = os.path.join(HOME, "scenario.mjs")
_SCENARIO_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_scenario.mjs")


def _install_scenario() -> bool:
    try:
        with open(_SCENARIO_SOURCE, encoding="utf-8") as fh:
            want = fh.read()
        try:
            with open(SCENARIO, encoding="utf-8") as fh:
                if fh.read() == want:
                    return True
        except OSError:
            pass
        tmp = f"{SCENARIO}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(want)
        os.replace(tmp, SCENARIO)
        return True
    except OSError:
        return False


def _env(state: dict) -> dict:
    env = {"PLAYWRIGHT_MODULE": MODULE, "PLAYWRIGHT_WS_ENDPOINT": state["wsEndpoint"]}
    if _install_scenario():
        env["PLAYWRIGHT_SCENARIO"] = SCENARIO
    return env


def _alive(state: dict) -> bool:
    pid, ws = int(state.get("pid") or 0), str(state.get("wsEndpoint") or "")
    if not pid or not ws:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    u = urlparse(ws)
    try:
        # Loopback: the kernel answers a connect at once (accepted or refused), so no timeout is needed.
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80)):
            return True
    except OSError:
        return False


def _read_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _install() -> bool:
    """Playwright and Chromium once per host (the host's own cache); never inside a worktree."""
    npm, node = shutil.which("npm"), shutil.which("node")
    if not npm or not node:
        return False
    os.makedirs(HOME, exist_ok=True)
    if not os.path.isfile(os.path.join(MODULE, "cli.js")):
        r = run([npm, "install", "--no-audit", "--no-fund", "--prefix", HOME, "playwright"], cwd=HOME, check=False)
        if r.code != 0 or not os.path.isfile(os.path.join(MODULE, "cli.js")):
            return False
    marker = os.path.join(HOME, "chromium.installed")
    if not os.path.isfile(marker):
        r = run([node, os.path.join(MODULE, "cli.js"), "install", "chromium"], cwd=HOME, check=False)
        if r.code != 0:
            return False
        open(marker, "w").close()
    return True


def _start() -> dict:
    node = shutil.which("node")
    log = open(os.path.join(HOME, "server.log"), "ab")
    proc = subprocess.Popen([node, os.path.join(MODULE, "cli.js"), "run-server", "--host", "127.0.0.1", "--port", "0"],
                            cwd=HOME, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True, env=check_execution.inherited_environment())
    ws = ""
    while not ws and proc.poll() is None:
        time.sleep(0.2)
        try:
            with open(os.path.join(HOME, "server.log"), "rb") as fh:
                fh.seek(max(0, os.path.getsize(os.path.join(HOME, "server.log")) - 4000))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            tail = ""
        for line in reversed(tail.splitlines()):
            if "ws://" in line:
                ws = line[line.index("ws://"):].split()[0]
                break
    if not ws:
        return {}
    state = {"pid": proc.pid, "wsEndpoint": ws, "startedAt": time.time()}
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)
    return state


def ensure() -> dict:
    """``{PLAYWRIGHT_MODULE, PLAYWRIGHT_WS_ENDPOINT, PLAYWRIGHT_SCENARIO}`` for a running server, or {} when this host has no
    node/npm or the install fails (a session then has no browser, as before). Cheap when alive."""
    state = _read_state()
    if _alive(state):
        return _env(state)
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, ".lock"), "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = _read_state()
            if not _alive(state):
                if not _install():
                    return {}
                state = _start()
    except OSError:
        return {}
    if not state:
        return {}
    return _env(state)
