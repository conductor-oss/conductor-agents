"""App UI checks on devices, as a host service: an iOS simulator, an Android emulator or an Electron window.

The agent sandbox cannot boot a simulator (CoreSimulator is a launchd service), start an emulator (it needs the
hypervisor) or open an Electron window, for the same reason it cannot launch a browser (common/browser_service).
So, like the browser, the device is a host service over loopback: a session or a check run calls

    .cc-cache/bin/device ios --scheme App [--project App.xcodeproj | --workspace App.xcworkspace] [--only-testing T]
    .cc-cache/bin/device android [--module app] [--avd NAME]
    .cc-cache/bin/device electron .cc/checks/<name>.mjs [--main main.js]

and the service runs that one fixed job on the host, in that tree, and answers with its output and exit code:

* ios: a simulator of its own (``simctl create``), ``xcodebuild test`` against it with the build output in the
  tree (``-derivedDataPath .cc-cache/DerivedData``), the simulator deleted afterwards;
* android: an emulator of its own (``-read-only``, so several run at once, on a free port), the instrumented
  tests (``gradlew connectedDebugAndroidTest``) against it, the emulator stopped afterwards;
* electron: Playwright's Electron driver launches the tree's Electron on its main entry and hands the
  scenario ``{app, page}`` (the first window), like PLAYWRIGHT_SCENARIO does for a page.

The service takes jobs only for a harness tree (it has ``.cc-cache``) and only with this host's token (in the
session's environment); it builds every command itself from the job's fields, never a shell line. One service
per host, started by whichever worker first needs it, as the browser server is. A job is held to the expected
duration of its kind (common/check_execution), never waited on forever.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.environ.get("CONDUCTOR_DEVICE_HOME") or os.path.expanduser("~/.cache/conductor-harness/devices")
STATE = os.path.join(HOME, "server.json")
CLIENT_REL = os.path.join(".cc-cache", "bin", "device")
DRIVERS = ("ios", "android", "electron")
EXPECTED_S = {"ios": 1200, "android": 900, "electron": 300}  # first-run expectations: a cold build included
NEEDS = {"ios", "android", "electron", "flutter", "react-native"}  # the packs whose UI checks use a device
_ELECTRON_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "electron_scenario.mjs")

CLIENT = r'''#!/usr/bin/env python3
"""device ios|android|electron ...: an app UI check on a device the harness runs on the host (common/device_service.py)."""
import json, os, sys, urllib.request
url, token = os.environ.get("DEVICE_SERVICE", ""), os.environ.get("DEVICE_TOKEN", "")
if not url:
    sys.stderr.write("device: this host has no device service (DEVICE_SERVICE is not set)\n"); sys.exit(127)
args = sys.argv[1:]
if not args or args[0] not in ("ios", "android", "electron"):
    sys.stderr.write(__doc__ + "\n"); sys.exit(2)
job = {"driver": args[0], "tree": os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")),
       "cwd": os.getcwd(), "args": args[1:]}
req = urllib.request.Request(url + "/run", data=json.dumps(job).encode(), method="POST",
                             headers={"Content-Type": "application/json", "X-Device-Token": token})
try:
    # Straight to loopback: never through the sandbox's network proxy (HTTP_PROXY is set in a session).
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as r:
        out = json.load(r)
except Exception as e:
    sys.stderr.write(f"device: the device service did not answer: {e}\n"); sys.exit(1)
sys.stdout.write(out.get("stdout") or ""); sys.stderr.write(out.get("stderr") or "")
sys.exit(int(out.get("exit", 1)))
'''


# ---------------------------------------------------------------- the harness side: start it, hand it to sessions
def _read_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _alive(state: dict) -> bool:
    pid, port = int(state.get("pid") or 0), int(state.get("port") or 0)
    if not pid or not port:
        return False
    try:
        os.kill(pid, 0)
        with socket.create_connection(("127.0.0.1", port)):  # loopback: answered at once, no wait needed
            return True
    except OSError:
        return False


def ensure(tree: str, packs: list[str]) -> dict:
    """``{DEVICE_SERVICE, DEVICE_TOKEN}`` for the packs that check an app on a device, with the ``device`` command
    written into the tree; {} for every other run (nothing is started)."""
    if not NEEDS & set(packs or []):
        return {}
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, ".lock"), "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = _read_state()
            if not _alive(state):
                state = _start()
        _install_client(tree)
    except OSError:
        return {}
    return {"DEVICE_SERVICE": f"http://127.0.0.1:{state['port']}", "DEVICE_TOKEN": state["token"]} if state else {}


def _install_client(tree: str) -> None:
    path = os.path.join(tree, CLIENT_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if open(path, encoding="utf-8").read() == CLIENT:
            return
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(CLIENT)
    os.chmod(path, 0o755)


def _start() -> dict:
    token = secrets.token_hex(16)
    log = open(os.path.join(HOME, "server.log"), "ab")
    from common import check_execution
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", token], stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True, env=check_execution.inherited_environment(),
                            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    while proc.poll() is None:
        state = _read_state()
        if state.get("pid") == proc.pid and _alive(state):
            return state
        time.sleep(0.2)
    return {}


# ---------------------------------------------------------------- the service: one fixed job per request
def _flag(args: list[str], name: str, default: str = "") -> str:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def _inside(tree: str, path: str) -> str:
    """``path`` (relative to the tree) resolved, or a ValueError when it leaves the tree."""
    full = os.path.realpath(os.path.join(tree, path))
    if full != tree and not full.startswith(tree + os.sep):
        raise ValueError(f"{path} is outside the tree")
    return full


def _cmd(argv: list[str]):
    """A short host command (simctl, adb, emulator -list-avds) through the harness's shared runner."""
    from common.exec import run
    return run(argv, check=False)


def _run(argv: list[str], tree: str, env: dict, expected_s: float):
    from common import check_execution
    return check_execution.execute(argv, cwd=tree, env=env, expected_s=expected_s)


def _ios(tree: str, args: list[str], env: dict) -> tuple[int, str, str]:
    scheme = _flag(args, "--scheme")
    if not scheme:
        return 2, "", "device ios: --scheme is required\n"
    got = _cmd(["xcrun", "simctl", "list", "-j", "devicetypes", "runtimes"])
    if got.code != 0:
        return 1, "", "device ios: this host has no iOS Simulator (Xcode with a simulator runtime is needed)\n"
    listing = json.loads(got.stdout)
    runtimes = [r for r in listing.get("runtimes", []) if r.get("isAvailable") and r.get("platform", "iOS") == "iOS"]
    if not runtimes:
        return 1, "", "device ios: this host has no iOS Simulator runtime\n"
    want = _flag(args, "--device", "iPhone")
    types = [t for t in listing.get("devicetypes", []) if want.lower() in str(t.get("name", "")).lower()]
    if not types:
        return 1, "", f"device ios: no simulator device type matches {want!r}\n"
    made = _cmd(["xcrun", "simctl", "create", f"cc-{secrets.token_hex(3)}", types[-1]["identifier"],
                 runtimes[-1]["identifier"]])
    if made.code != 0:
        return 1, "", f"device ios: could not create a simulator: {made.stderr.strip()[:300]}\n"
    udid = made.stdout.strip()
    try:
        argv = ["xcodebuild", "test", "-scheme", scheme, "-destination", f"id={udid}",
                "-derivedDataPath", _inside(tree, ".cc-cache/DerivedData")]
        for flag in ("--project", "--workspace"):
            if _flag(args, flag):
                argv += [flag[1:], _inside(tree, _flag(args, flag))]
        if _flag(args, "--only-testing"):
            argv.append(f"-only-testing:{_flag(args, '--only-testing')}")
        r = _run(argv, tree, env, EXPECTED_S["ios"])
        return r.exit_code, r.stdout, r.stderr
    finally:
        _cmd(["xcrun", "simctl", "shutdown", udid])
        _cmd(["xcrun", "simctl", "delete", udid])


def _free_emulator_port() -> int:
    for port in range(5554, 5682, 2):  # an emulator takes a console port and the next one
        try:
            for p in (port, port + 1):
                with socket.socket() as s:
                    s.bind(("127.0.0.1", p))
            return port
        except OSError:
            continue
    raise RuntimeError("no free emulator port")


def _android(tree: str, args: list[str], env: dict) -> tuple[int, str, str]:
    avd = _flag(args, "--avd") or next(iter(_cmd(["emulator", "-list-avds"]).stdout.split()), "")
    if not avd:
        return 1, "", "device android: this host has no Android virtual device (create one with avdmanager)\n"
    module = _flag(args, "--module", "app")
    if not module.replace("-", "").replace("_", "").isalnum():
        return 2, "", f"device android: {module!r} is not a module name\n"
    port = _free_emulator_port()
    serial = f"emulator-{port}"
    from common import check_execution
    emu = subprocess.Popen(["emulator", "-avd", avd, "-port", str(port), "-no-window", "-no-snapshot", "-no-audio",
                            "-read-only"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                           start_new_session=True, env=check_execution.inherited_environment())
    try:
        _cmd(["adb", "-s", serial, "wait-for-device"])
        while _cmd(["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"]).stdout.strip() != "1":
            if emu.poll() is not None:
                return 1, "", f"device android: the emulator for {avd} exited (code {emu.returncode}) before it booted\n"
            time.sleep(1)
        gradlew = _inside(tree, "gradlew")
        r = _run([gradlew, f":{module}:connectedDebugAndroidTest"], tree, {**env, "ANDROID_SERIAL": serial},
                 EXPECTED_S["android"])
        return r.exit_code, r.stdout, r.stderr
    finally:
        _cmd(["adb", "-s", serial, "emu", "kill"])
        try:
            os.killpg(emu.pid, signal.SIGTERM)
        except OSError:
            pass


def _electron(tree: str, args: list[str], env: dict) -> tuple[int, str, str]:
    positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    if not positional:
        return 2, "", "device electron: name the scenario script (.cc/checks/<name>.mjs)\n"
    from common import browser_service
    if not browser_service.ensure():
        return 1, "", "device electron: this host has no Playwright (node and npm are needed)\n"
    node = shutil.which("node") or "node"
    r = _run([node, _ELECTRON_RUNNER, _inside(tree, positional[0]), _inside(tree, _flag(args, "--main", "."))], tree,
             {**env, "PLAYWRIGHT_MODULE": browser_service.MODULE}, EXPECTED_S["electron"])
    return r.exit_code, r.stdout, r.stderr


def handle(job: dict, token: str, sent_token: str) -> dict:
    """One job: check it, run it, answer ``{exit, stdout, stderr}``."""
    if not secrets.compare_digest(str(sent_token or ""), token):
        return {"exit": 1, "stderr": "device: refused (not this host's token)\n"}
    driver, tree = str(job.get("driver") or ""), os.path.realpath(str(job.get("tree") or ""))
    if driver not in DRIVERS:
        return {"exit": 2, "stderr": f"device: unknown driver {driver!r}\n"}
    if not os.path.isdir(os.path.join(tree, ".cc-cache")):
        return {"exit": 1, "stderr": "device: refused (not a harness tree)\n"}
    from common import check_execution
    env = check_execution.inherited_environment()
    try:
        code, out, err = {"ios": _ios, "android": _android, "electron": _electron}[driver](
            tree, [str(a) for a in job.get("args") or []], env)
    except (OSError, ValueError, RuntimeError) as e:
        code, out, err = 1, "", f"device {driver}: {e}\n"
    return {"exit": code, "stdout": out, "stderr": err}


def serve(token: str) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                job = json.loads(body or b"{}")
            except ValueError:
                job = {}
            answer = json.dumps(handle(job, token, self.headers.get("X-Device-Token"))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "port": server.server_address[1], "token": token, "startedAt": time.time()}, fh)
    os.replace(tmp, STATE)
    server.serve_forever()


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "serve":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    serve(sys.argv[2])
