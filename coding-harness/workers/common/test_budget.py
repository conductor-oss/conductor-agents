"""Test runs with an expected duration: a test that runs far past how long it usually takes is stopped and
investigated, never waited on.

A Node test that leaves a server or socket open keeps `node --test` alive after every test has finished;
the agent's command timeout then killed only its shell, the node process kept its port, and the next run
collided with it. In run 221d632a fifteen such runs cost 28 minutes, and nine agents ended up waiting
half an hour each on commands that would never end. Some suites honestly take minutes; most finish in
seconds. So the limit is the command's own history, not a fixed number:

* every session's `.cc-cache/bin` (first on its PATH, on every backend, and on the harness's own check
  runs) has wrappers for the common test runners (``SHIMS``); anything that is not a test run passes
  straight through to the real tool;
* a test run's budget is ``BUDGET_FACTOR`` times the longest of its recent finished runs in this tree, plus
  ``BUDGET_MARGIN_S`` (at least ``MIN_BUDGET_S``); with no history yet, the tool's default
  (``DEFAULTS``); ``TEST_BUDGET_S=<seconds>`` sets it for a suite that needs longer;
* the run gets its own process group; past its budget the group is stopped and the agent is told what
  usually hangs a test and to investigate before re-running; when the agent's own timeout kills the
  wrapper, the wrapper takes the group down with it, so nothing is left holding a port;
* `node --test` also gets `--test-force-exit` (exit once the tests are done, whatever they left open).
"""
from __future__ import annotations

import json
import os
import stat

BIN_REL = os.path.join(".cc-cache", "bin")
TIMES_REL = os.path.join(".cc-cache", "test-times.json")
from . import stacks as _stacks

# Each platform pack's test runners too (common/stacks.py): `dotnet test`, `xcodebuild test`, `mix test`, `rspec`, ...
PACK_TESTS = {tool: [list(words), match] for tool, (_s, words, match) in _stacks.test_runners().items()}
SHIMS = ("node", "npm", "npx", "pnpm", "yarn", "pytest", "go", "cargo", "mvn", "gradle", "gradlew") + tuple(
    t for t in PACK_TESTS if t not in ("node", "npm", "go", "cargo", "mvn", "gradle"))
DEFAULTS = {"node": 120, "npm": 180, "npx": 180, "pnpm": 180, "yarn": 180, "pytest": 180, "go": 300,
            "cargo": 300, "mvn": 600, "gradle": 600, "gradlew": 600,
            **{tool: seconds for tool, (seconds, _w, _m) in _stacks.test_runners().items()}}
BUDGET_FACTOR = 3
BUDGET_MARGIN_S = 20
MIN_BUDGET_S = 30
HISTORY = 5
CHECK_DEFAULT_S = 300  # an acceptance check run by an agent the first time (a browser journey takes minutes)
LOG_REL = os.path.join(".cc-cache", "test-runs.jsonl")
# What the harness sets on its own check runs: they always run (TEST_RERUN), keep the harness's own limits,
# and are recorded so an agent reuses the harness's result instead of re-running the same check on the same tree.
HARNESS_ENV = {"CONDUCTOR_HARNESS_RUN": "1", "TEST_RERUN": "1"}

RUNNER = r'''#!/usr/bin/env python3
"""A test runner wrapper of the coding harness (workers/common/test_budget.py). Every test run is logged; one
already run on exactly this tree is not run again (its result is shown); one that goes far past how long it
usually takes is stopped, with its whole process group. Everything else passes straight through."""
import fcntl, json, os, signal, subprocess, sys, threading, time
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.abspath(os.path.join(here, "..", ".."))
cache = os.path.join(root, ".cc-cache")
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
real = ""
for d in os.environ.get("PATH", "").split(os.pathsep):
    p = os.path.join(d, tool)
    if d and os.path.abspath(d) != here and os.path.isfile(p) and os.access(p, os.X_OK):
        real = p
        break
if not real:
    sys.stderr.write(f"{tool}: not found on PATH\n")
    sys.exit(127)
DEFAULTS = __DEFAULTS__
PACK_TESTS = __PACK_TESTS__
FACTOR, MARGIN, FLOOR, KEEP, CHECK_DEFAULT = __FACTOR__, __MARGIN__, __FLOOR__, __KEEP__, __CHECK__
NPM_MUTATING = {"install", "i", "add", "ci", "install-clean", "clean-install", "isntall", "uninstall", "remove", "rm",
                "un", "unlink", "update", "up", "upgrade", "dedupe", "ddp", "prune", "link", "ln", "rebuild", "rb"}
words = [a for a in args if not a.startswith("-")]
sub = words[0] if words else ""
first = next((i for i, a in enumerate(args) if not a.startswith("-")), len(args))


def is_test():
    if tool == "node":
        # A node test run, or one of the run's acceptance checks (`node .cc/checks/<name>.mjs`).
        script = os.path.relpath(os.path.abspath(args[first]), root) if first < len(args) else ""
        return "--test" in args[:first] or script.startswith(os.path.join(".cc", "checks") + os.sep)
    if tool in ("npm", "pnpm", "yarn"):
        return sub in ("test", "t", "tst") or (sub in ("run", "run-script") and len(words) > 1 and "test" in words[1])
    if tool == "npx":
        return sub in ("vitest", "jest", "mocha", "ava", "tap")
    if tool == "pytest":
        return True
    if tool in ("go", "cargo"):
        return sub == "test"
    if tool == "mvn":
        return any(w in ("test", "verify", "integration-test") or w.endswith(":test") for w in words)
    if tool in ("gradle", "gradlew"):
        return any(w in ("test", "check") or w.endswith(":test") for w in words)
    if tool in PACK_TESTS:
        tests, match = PACK_TESTS[tool]
        if match == "all":
            return True
        if match == "any":
            return any(w in tests for w in words)
        return " ".join(words[:2]) in tests or sub in tests
    return False


if tool == "npm" and sub in NPM_MUTATING:
    # Teammates share this tree: one install at a time (concurrent installs left node_modules broken, run c6c1e7a7).
    lock = open(os.path.join(cache, "npm.lock"), "a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
if not is_test():
    os.execv(real, [real] + args)
if tool == "node" and "--test" in args[:first] and "--test-force-exit" not in args[:first]:
    args = ["--test-force-exit"] + args
where = os.path.relpath(os.getcwd(), root)
key = tool + " " + " ".join(sys.argv[1:]) + ("" if where == "." else f"  (in {where})")
times_path, log_path = os.path.join(cache, "test-times.json"), os.path.join(cache, "test-runs.jsonl")


def git(*a, env=None):
    try:
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              env=env).stdout
    except OSError:
        return ""


def tree_digest():
    """The content of the whole working tree (what `git add -A && git write-tree` would record, in a scratch
    index): the same files give the same digest before and after the harness commits them."""
    index = git("rev-parse", "--git-path", "index").strip()
    scratch = os.path.join(cache, f"tree-index.{os.getpid()}")
    try:
        if index:
            path = index if os.path.isabs(index) else os.path.join(root, index)
            if os.path.isfile(path):
                with open(path, "rb") as src, open(scratch, "wb") as dst:
                    dst.write(src.read())
        env = {**os.environ, "GIT_INDEX_FILE": scratch}
        git("add", "-A", env=env)
        return git("write-tree", env=env).strip() or f"unknown-{time.time()}"
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass


def locked(update):
    with open(times_path + ".lock", "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            data = json.load(open(times_path))
        except (OSError, ValueError):
            data = {}
        out = update(data)
        tmp = times_path + ".tmp"
        json.dump(data, open(tmp, "w"))
        os.replace(tmp, times_path)
        return out


def log(**entry):
    entry.update(ts=round(time.time(), 1), tool=tool, cmd=key, session=os.environ.get("CONDUCTOR_LAUNCH_ID", ""))
    with open(log_path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


harness_run = bool(os.environ.get("CONDUCTOR_HARNESS_RUN"))
tree = tree_digest()
past = locked(lambda d: list(d.get(key) or []))
last = past[-1] if past else {}
if last.get("tree") == tree and last.get("reusable") and not os.environ.get("TEST_RERUN", "").strip():
    ago = time.time() - float(last.get("ts") or time.time())
    sys.stderr.write(
        f"[harness] `{tool} {' '.join(sys.argv[1:])}` already ran on exactly this tree {ago:.0f} s ago "
        f"(exit {last.get('exit')}, {last.get('s')} s{', by the harness' if last.get('harness') else ''}); "
        "the same code gives the same result, so it was not run again. Its output ended:\n"
        + (last.get("tail") or "(no output)") + "\n[harness] Change something first, or run it with TEST_RERUN=1 "
        "(to check for flakiness, say).\n")
    log(s=0, exit=last.get("exit"), outcome="reused", tree=tree)
    sys.exit(int(last.get("exit") or 0))
finished = [h["s"] for h in past if h.get("done")]
check = tool == "node" and "--test" not in args[:first]
if harness_run:
    # The harness's own check runs keep the harness's rules (swarm_measure: the check's expected duration and
    # the progress rule); the wrapper only records them so agents can reuse the result.
    budget, why = None, ""
elif os.environ.get("TEST_BUDGET_S", "").strip():
    budget, why = float(os.environ["TEST_BUDGET_S"]), "TEST_BUDGET_S"
elif finished:
    typical = max(finished[-KEEP:])
    budget, why = max(FACTOR * typical + MARGIN, FLOOR), f"it has taken at most {typical:.0f} s in this tree"
elif check:
    budget, why = float(CHECK_DEFAULT), "the first-run default for an acceptance check"
else:
    budget, why = float(DEFAULTS.get(tool, 180)), f"the first-run default for {tool} tests"
proc = subprocess.Popen([real] + args, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
tail = []


def pump(src, dst):
    for chunk in iter(lambda: src.read1(4096), b""):
        dst.buffer.write(chunk)
        dst.flush()
        tail.append(chunk)
        while sum(map(len, tail)) > 6000 and len(tail) > 1:
            tail.pop(0)


pumps = [threading.Thread(target=pump, args=(proc.stdout, sys.stdout), daemon=True),
         threading.Thread(target=pump, args=(proc.stderr, sys.stderr), daemon=True)]
for t in pumps:
    t.start()


def stop(*_):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(50):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def on_signal(signum, _frame):
    stop()
    sys.exit(128 + signum)


for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(s, on_signal)
started = time.monotonic()


def record(entry):
    def update(d):
        d[key] = ((d.get(key) or []) + [entry])[-KEEP:]
    locked(update)


harness = harness_run
try:
    code = proc.wait(timeout=budget)
except subprocess.TimeoutExpired:
    stop()
    took = round(time.monotonic() - started, 1)
    record({"s": took, "done": False, "ts": time.time(), "tree": tree})
    log(s=took, exit=124, outcome="stopped", tree=tree, harness=harness)
    sys.stderr.write(
        f"\n[harness] stopped `{tool} {' '.join(sys.argv[1:])}` after {budget:.0f} s ({why}). A test run this far "
        "past its usual time is hung, not slow: usually a server, socket, timer or child process the tests opened "
        "and never closed (the runner waits for it), a fixed port that a teammate's server already holds, or a wait "
        "with no bound. Find which test and why before you run it again (run the file alone, add a per-test "
        "timeout, close what each test opens, pick ports at run time). A suite that truly needs longer: "
        "TEST_BUDGET_S=<seconds>.\n")
    sys.exit(124)
for t in pumps:
    t.join(timeout=5)
took = round(time.monotonic() - started, 1)
text = b"".join(tail).decode("utf-8", "replace")[-1500:]
# A run that changed the tree itself (a build, a snapshot) counts for its usual duration but is not reused.
record({"s": took, "done": True, "reusable": tree_digest() == tree, "ts": time.time(), "tree": tree, "exit": code,
        "tail": text, "harness": harness})
log(s=took, exit=code, outcome="passed" if code == 0 else "failed", tree=tree, harness=harness)
sys.exit(code)
'''


def runner_text() -> str:
    return (RUNNER.replace("__DEFAULTS__", repr(DEFAULTS)).replace("__PACK_TESTS__", repr(PACK_TESTS)).replace("__FACTOR__", str(BUDGET_FACTOR))
            .replace("__MARGIN__", str(BUDGET_MARGIN_S)).replace("__FLOOR__", str(MIN_BUDGET_S))
            .replace("__KEEP__", str(HISTORY)).replace("__CHECK__", str(CHECK_DEFAULT_S)))


def runs(worktree: str, since: float = 0.0) -> list[dict]:
    """The test runs logged in this tree since ``since`` (epoch seconds)."""
    out = []
    try:
        with open(os.path.join(worktree, LOG_REL), encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if float(entry.get("ts") or 0) >= since:
                    out.append(entry)
    except OSError:
        pass
    return out


def summary(entries: list[dict]) -> dict:
    """How much time went to test runs, and to which: runs by outcome, seconds, the most-run commands and the
    same command run again by one session (what the reuse rule saves when the tree had not changed)."""
    agent = [e for e in entries if not e.get("harness")]
    by_cmd: dict[str, int] = {}
    per_session: dict[tuple, int] = {}
    for e in agent:
        if e.get("outcome") != "reused":
            by_cmd[e.get("cmd")] = by_cmd.get(e.get("cmd"), 0) + 1
            k = (e.get("session"), e.get("cmd"))
            per_session[k] = per_session.get(k, 0) + 1
    count = lambda o: sum(1 for e in agent if e.get("outcome") == o)
    return {"runs": len(agent) - count("reused"), "seconds": round(sum(float(e.get("s") or 0) for e in agent), 1),
            "passed": count("passed"), "failed": count("failed"), "stopped": count("stopped"), "reused": count("reused"),
            "stoppedSeconds": round(sum(float(e.get("s") or 0) for e in agent if e.get("outcome") == "stopped"), 1),
            "harnessRuns": len(entries) - len(agent),
            "mostRun": [{"cmd": c, "runs": n} for c, n in sorted(by_cmd.items(), key=lambda x: -x[1])[:5]],
            "sameSessionRepeats": sum(n - 1 for n in per_session.values() if n > 1)}


def summary_text(s: dict) -> str:
    if not s.get("runs") and not s.get("reused"):
        return "no test runs"
    top = "; ".join(f"{m['cmd']} x{m['runs']}" for m in s.get("mostRun") or [])
    return (f"{s['runs']} test run(s) by the agents, {s['seconds']:.0f} s in all ({s['passed']} passed, {s['failed']} "
            f"failed, {s['stopped']} stopped past their usual time = {s['stoppedSeconds']:.0f} s); {s['reused']} not run "
            f"again because the tree had not changed; {s['sameSessionRepeats']} repeat(s) of a command by the same "
            f"session; most run: {top or 'none'}")


def install(worktree: str) -> list[str]:
    """Write the wrappers into ``<worktree>/.cc-cache/bin`` (rewritten only when they changed)."""
    text = runner_text()
    folder = os.path.join(worktree, BIN_REL)
    os.makedirs(folder, exist_ok=True)
    out = []
    for name in SHIMS:
        path = os.path.join(folder, name)
        try:
            current = open(path, encoding="utf-8").read()
        except OSError:
            current = ""
        if current != text:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        out.append(path)
    return out
