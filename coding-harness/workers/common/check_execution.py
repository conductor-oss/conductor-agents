"""Shared execution primitives for campaign and exact-SHA checks.

These commands are trusted, but they still need one reproducible environment
and one lifecycle.  In particular, a check must not silently depend on the
supervisor's complete environment, nor leave a process tree behind when a
Conductor task is explicitly cancelled.
"""

from __future__ import annotations

import os
import errno
from contextlib import contextmanager
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_MAX_CAPTURE_CHARS = 1_000_000
_TRANSIENT_SPAWN_ERRNOS = {errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE}
JAVA_ENTRYPOINTS = {"./gradlew", "gradle", "./mvnw", "mvn"}
_RUNTIME_COMMANDS = (
    "java", "go", "python", "python3", "ruby", "bundle", "bundler", "rake",
    "node", "npm", "npx", "pnpm", "yarn", "tsc",
)
_RUNTIME_PROBES = {
    "java": ["java", "-version"],
    "go": ["go", "version"],
    "python": ["python", "--version"],
    "python3": ["python3", "--version"],
    "ruby": ["ruby", "--version"],
    "bundle": ["bundle", "--version"],
    "dotnet": ["dotnet", "--version"],
    "php": ["php", "--version"],
    "composer": ["composer", "--version"],
    "mix": ["mix", "--version"],
    "sbt": ["sbt", "--version"],
    "bazel": ["bazel", "--version"],
    "rake": ["rake", "--version"],
    "node": ["node", "--version"],
    "npm": ["npm", "--version"],
    "npx": ["npx", "--version"],
    "pnpm": ["pnpm", "--version"],
    "yarn": ["yarn", "--version"],
}
_OPTIONAL_RUNTIME_FAMILIES = {"dotnet", "php", "elixir"}
_RUNTIME_FAMILIES = {
    "java": ("java",),
    "go": ("go",),
    "python": ("python", "python3"),
    "ruby": ("ruby", "bundle", "rake"),
    # A repository supplies its TypeScript compiler in devDependencies. The
    # worker guarantees the runtime/package-launch surface used to invoke it.
    "typescript": ("node", "npm", "npx"),
    # Optional: reported so an operator can see what this host can verify, but
    # excluded from `healthy` so a machine that never builds .NET/PHP/Elixir is
    # not declared unhealthy for lacking toolchains it does not need.
    "dotnet": ("dotnet",),
    "php": ("php", "composer"),
    "elixir": ("mix",),
}
_POWER_ASSERTION_STATE = threading.local()


def candidate_jdk_homes(version: str = "") -> list[str]:
    """Return explicit and conventional JDK homes without invoking Java.

    Runtime discovery belongs in the shared command layer so every worker that
    launches repository checks sees the same JDK, including workers started by
    launchd/systemd without an interactive ``JAVA_HOME``.
    """
    homes: list[str] = []
    if version:
        homes.extend([os.environ.get(f"JAVA{version}_HOME", ""),
                      os.environ.get(f"JDK{version}_HOME", "")])
        tags = (f"openjdk@{version}",)
    else:
        homes.append(os.environ.get("JAVA_HOME", ""))
        tags = ("openjdk@21", "openjdk")
    for prefix in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
        for tag in tags:
            homes.append(str(prefix / tag / "libexec/openjdk.jdk/Contents/Home"))
    for cellar in (Path("/opt/homebrew/Cellar"), Path("/usr/local/Cellar")):
        for tag in tags:
            homes.extend(str(path / "libexec/openjdk.jdk/Contents/Home")
                         for path in sorted(cellar.glob(f"{tag}/*"), reverse=True))
    if version:
        homes.extend([f"/usr/lib/jvm/java-{version}-openjdk",
                      f"/usr/lib/jvm/java-{version}-openjdk-amd64"])
    else:
        homes.extend(str(path) for path in
                     sorted(Path("/usr/lib/jvm").glob("java-*-openjdk*"), reverse=True))
    return [home for home in homes if home]


def resolve_java_home(version: object = None) -> str | None:
    """Resolve an installed JDK for all worker-launched repository commands."""
    value = str(version or "").strip()
    for candidate in candidate_jdk_homes(value):
        if Path(candidate, "bin", "java").is_file():
            return candidate
    return None


def runtime_bin_directories() -> list[str]:
    """Resolve concrete runtime bin directories without relying on shell init."""
    candidates = [
        Path(__file__).resolve().parents[1] / ".venv" / "bin",
        Path(sys.executable).resolve().parent,
        Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
        Path("/opt/homebrew/opt/go/bin"), Path("/usr/local/opt/go/bin"),
        Path("/opt/homebrew/opt/ruby/bin"), Path("/usr/local/opt/ruby/bin"),
        Path("/opt/homebrew/opt/node/bin"), Path("/usr/local/opt/node/bin"),
        Path("/Library/Frameworks/Python.framework/Versions/Current/bin"),
    ]
    host_path = os.environ.get("PATH", "")
    for command in _RUNTIME_COMMANDS:
        executable = shutil.which(command, path=host_path)
        if executable:
            candidates.append(Path(executable).resolve().parent)
    found: list[str] = []
    for candidate in candidates:
        value = str(candidate)
        if candidate.is_dir() and value not in found:
            found.append(value)
    return found


def install_runtime_environment(env: dict[str, str] | None = None, *,
                                java_version: object = None) -> str | None:
    """Install the resolved JDK into a worker/command environment in place.

    This is idempotent and deliberately centralized: worker supervisors call it
    once for all child processes, while isolated check environments call it
    again after stripping credentials and incidental host state.
    """
    target = os.environ if env is None else env
    # Every command the harness runs is non-interactive. CI=1 is the signal test runners honour:
    # without it `npm test` ran Vitest in WATCH mode in the team's final test cycle and the run sat
    # in it for 436 s until the operator stopped it (run 82fe7c00); jest, Playwright and npm itself
    # read the same variable.
    target.setdefault("CI", "1")
    java_home = resolve_java_home(java_version)
    runtime_bins = runtime_bin_directories()
    if java_home:
        target["JAVA_HOME"] = java_home
        runtime_bins.insert(0, str(Path(java_home) / "bin"))
    path_parts = [part for part in target.get("PATH", "").split(os.pathsep) if part]
    # A worktree's own tool bin goes first (CONDUCTOR_PATH_PREPEND): the shims there wrap the real
    # tools, e.g. one `npm install` at a time per shared tree.
    first = [part for part in target.get("CONDUCTOR_PATH_PREPEND", "").split(os.pathsep) if part]
    target["PATH"] = os.pathsep.join([
        *first,
        *[part for part in runtime_bins if part not in first],
        *[part for part in path_parts if part not in runtime_bins and part not in first],
    ])
    return java_home


def inherited_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return the complete worker environment with concrete runtimes installed."""
    env = dict(os.environ)
    if overrides:
        env.update(overrides)
    install_runtime_environment(env)
    return env


def idle_sleep_inhibition_active() -> bool:
    """Return whether the current worker call is protected from idle sleep."""
    return bool(getattr(_POWER_ASSERTION_STATE, "active", False))


@contextmanager
def prevent_idle_system_sleep(reason: str):
    """Keep a macOS worker host awake while one Conductor task is active.

    Conductor lease renewal runs in the worker process. If macOS suspends that
    process, the server can enforce its normalized response lease during a dark
    wake before Python gets CPU time to send a heartbeat. ``caffeinate -i`` is
    an assertion, not a deadline: it exists only for the duration of the worker
    call and is explicitly released when that call returns or is cancelled.
    """
    previous = idle_sleep_inhibition_active()
    assertion = None
    if sys.platform == "darwin" and not previous:
        executable = "/usr/bin/caffeinate"
        if not Path(executable).is_file():
            raise RuntimeError("macOS idle-sleep inhibition is unavailable: /usr/bin/caffeinate missing")
        assertion = subprocess.Popen(
            [executable, "-i"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if assertion.poll() is not None:
            raise RuntimeError(f"macOS idle-sleep inhibition failed for {reason}")
    _POWER_ASSERTION_STATE.active = True
    try:
        yield
    finally:
        _POWER_ASSERTION_STATE.active = previous
        if assertion is not None:
            _terminate_process_group(assertion)


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    cancelled: bool = False
    output_truncated: bool = False
    # Stopped by the progress rule (``stall_after_s``): no output and next to no CPU for the window.
    stalled: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.cancelled

    @property
    def outcome(self) -> str:
        if self.cancelled:
            return "cancelled"
        return "passed" if self.exit_code == 0 else "code_failed"


def isolated_environment(
    state_dir: str | Path,
    *,
    java_home: str | None = None,
    resolve_default_java: bool = True,
    required_env: Iterable[str] = (),
    dependency_cache_dir: str | Path | None = None,
) -> dict[str, str]:
    """Return the worker's real environment for a repository command.

    Deployment controls what is actually present here, not this function: a
    Docker image sets the env vars a build needs, a local run uses the
    operator's own shell/config (``~/.gradle/gradle.properties``, ``~/.npmrc``,
    ``~/.m2/settings.xml``, ...), a sandboxed host has them injected before the
    worker starts. This inherits ``os.environ`` and every HOME-rooted
    config/cache location unchanged, so a repository that needs an
    authenticated package registry -- or any other machine-level
    configuration -- just works, the same way it would for a human running
    the same command by hand. ``required_env``/``dependency_cache_dir`` are
    accepted for call-site compatibility (``state_dir``/``dependency_cache_dir``
    still get created on disk for verification.py's own diagnostic
    bookkeeping) but no longer filter or redirect anything: everything named
    in ``required_env`` is already present via full inheritance.
    """
    root = Path(state_dir)
    cache = Path(dependency_cache_dir) if dependency_cache_dir else root / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    # Service managers commonly provide only /usr/bin:/bin.  Resolve the
    # ordinary package-manager locations explicitly so a worker launched by
    # launchd/systemd sees the same installed runtimes as an interactive shell.
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    for part in ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
                 "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if Path(part).is_dir() and part not in path_parts:
            path_parts.append(part)
    env["PATH"] = os.pathsep.join(path_parts)
    if java_home:
        env["JAVA_HOME"] = java_home
        java_bin = str(Path(java_home) / "bin")
        env["PATH"] = os.pathsep.join(
            [java_bin, *[part for part in env.get("PATH", "").split(os.pathsep) if part != java_bin]]
        )
    elif resolve_default_java:
        install_runtime_environment(env)
    return env


def executable_for(argv: list[str], *, cwd: str, env: dict[str, str]) -> str | None:
    """Resolve an argv entrypoint in the same environment that will run it."""
    if not argv:
        return None
    command = argv[0]
    if "/" in command:
        path = Path(cwd) / command if not Path(command).is_absolute() else Path(command)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(command, path=env.get("PATH"))


def _reap_leftovers(proc: subprocess.Popen[str]) -> None:
    """After a command has exited, stop anything it left running in its own session: a test or a
    check that started a server and never stopped it. A respcache from a run the day before was still
    listening on 127.0.0.1:2345 and every later check tested it instead of the head under test (runs
    5456297e, 351812b2). The command itself is already finished, so this is cleanup, never a deadline."""
    if os.name != "posix":
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        return  # nothing left in the group
    deadline_free_wait = 0
    while deadline_free_wait < 50:  # give the leftovers up to five seconds to exit, then SIGKILL
        try:
            os.killpg(proc.pid, 0)
        except OSError:
            return
        time.sleep(0.1)
        deadline_free_wait += 1
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


# Every process a check run or an agent's writing session starts carries this variable, with a value
# unique to that launch. Children inherit it whatever process group or session they move to, so when
# the launch ends everything still carrying its value is a leftover of it. Reaping by process group
# alone missed a `start.sh` that runs with `set -m` (Maven and Vite in groups of their own): the app a
# Codex session started kept its ports for the rest of run 3e3991fc, every later scenario met a stale
# app, and the team spent three hours teaching the launcher to detect and reuse it.
LAUNCH_MARKER = "CONDUCTOR_LAUNCH_ID"


def new_launch_marker() -> str:
    return uuid.uuid4().hex


def _marked_pids(marker: str) -> list[int]:
    needle = f"{LAUNCH_MARKER}={marker}"
    me = os.getpid()
    found: list[int] = []
    if os.path.isdir("/proc/self"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == me:
                continue
            try:
                with open(f"/proc/{entry}/environ", "rb") as fh:
                    env = fh.read().split(b"\0")
            except OSError:
                continue
            if needle.encode() in env:
                found.append(int(entry))
        return found
    try:
        out = subprocess.run(["ps", "-A", "-E", "-ww", "-o", "pid=,command="], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, errors="replace").stdout
    except OSError:
        return found
    for line in out.splitlines():
        head, _, rest = line.strip().partition(" ")
        if head.isdigit() and int(head) != me and (f" {needle} " in f" {rest} "):
            found.append(int(head))
    return found


def _cpu_seconds(pids: list[int]) -> float:
    """Total CPU time (user + system) the given live processes have used so far."""
    if not pids:
        return 0.0
    if os.path.isdir("/proc/self"):
        total, tick = 0.0, os.sysconf("SC_CLK_TCK")
        for pid in pids:
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                    fields = fh.read().rsplit(")", 1)[1].split()
                total += (int(fields[11]) + int(fields[12])) / tick
            except (OSError, IndexError, ValueError):
                continue
        return total
    try:
        out = subprocess.run(["ps", "-o", "time=", "-p", ",".join(str(p) for p in pids)],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True).stdout
    except OSError:
        return 0.0
    total = 0.0
    for line in out.split():
        days, _, clock = line.rpartition("-")
        parts = [float(x) for x in clock.split(":")]
        seconds = sum(v * 60 ** n for n, v in enumerate(reversed(parts)))
        total += seconds + (int(days) * 86400 if days.isdigit() else 0)
    return total


# The progress rule (``execute(stall_after_s=...)``): a command is stalled when, for a whole window, it
# wrote no output and every process it started together used less CPU than this share of one core.
# A build or a test suite is never that quiet however long it runs; a check blocked forever on a socket
# is (its client and its server idle at ~0%: the pgwire scenario of run 169589fd waited 736 s on a
# reply it had already dropped). An idle JVM's background ticks stay well under 2%.
STALL_CPU_SHARE = 0.02


def _watch_progress(proc: subprocess.Popen, marker: str, streams: list, stall_after_s: float,
                    stalled: threading.Event, done: threading.Event) -> None:
    """Stop ``proc`` and everything it started once it has made no progress for ``stall_after_s``."""
    interval = max(min(stall_after_s / 6.0, 10.0), 0.2)
    def sample() -> tuple[int, float]:
        pids = sorted(set(_marked_pids(marker)) | {proc.pid}) if marker else [proc.pid]
        return sum(s.seen for s in streams), _cpu_seconds(pids)
    last_output, window_cpu = sample()
    window_start = time.monotonic()
    while not done.wait(interval):
        output, cpu = sample()
        if output != last_output or cpu - window_cpu >= STALL_CPU_SHARE * stall_after_s:
            last_output, window_cpu, window_start = output, cpu, time.monotonic()
            continue
        if time.monotonic() - window_start >= stall_after_s:
            stalled.set()
            if marker:
                reap_marked(marker)  # TERM, then KILL whatever ignores it, wherever it moved
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig) if os.name == "posix" else proc.kill()
                except OSError:
                    pass
                waited = 0
                while proc.poll() is None and waited < 50:
                    time.sleep(0.1)
                    waited += 1
                if proc.poll() is not None:
                    break
            return


def reap_marked(marker: str) -> int:
    """Stop every process still carrying this launch's marker; the launch itself has already ended, so
    this is cleanup, never a deadline. Returns how many processes were found."""
    if os.name != "posix" or not marker:
        return 0
    pids = _marked_pids(marker)
    if not pids:
        return 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    waited = 0
    while waited < 50:  # up to five seconds to exit, then SIGKILL
        alive = [pid for pid in pids if _alive(pid)]
        if not alive:
            return len(pids)
        time.sleep(0.1)
        waited += 1
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return len(pids)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie is gone for our purposes
        waited, _ = os.waitpid(pid, os.WNOHANG)
        return waited == 0
    except ChildProcessError:
        return True
    except OSError:
        return True


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    """Stop a check and descendants after explicit cancellation."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait()
    except OSError:
        pass


class _BoundedStream:
    """Drain a subprocess pipe without retaining unbounded candidate output."""

    def __init__(self, stream, limit: int | None = _MAX_CAPTURE_CHARS):
        self._stream = stream
        self._limit = limit
        self._parts: list[str] = []
        self._size = 0
        self.seen = 0  # every character ever read, kept or not: output is progress
        self.truncated = False
        self._thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> str:
        self._thread.join()
        return "".join(self._parts)

    def _drain(self) -> None:
        try:
            # Whatever is available, as it arrives: a text read(n) waits for n characters or the end, so
            # a command printing a line a second looked silent until it exited (the progress rule
            # stopped one mid-run).
            import codecs
            decoder = codecs.getincrementaldecoder(getattr(self._stream, "encoding", None) or "utf-8")("replace")
            fd = self._stream.fileno()
            while True:
                raw = os.read(fd, 64 * 1024)
                chunk = decoder.decode(raw, final=not raw)
                if not raw and not chunk:
                    break
                if not chunk:
                    continue
                # The universal-newline translation the text pipe did (\r\n and \r become \n).
                chunk = chunk.replace("\r\n", "\n").replace("\r", "\n")
                self.seen += len(chunk)
                self._parts.append(chunk)
                self._size += len(chunk)
                while self._limit is not None and self._size > self._limit and self._parts:
                    excess = self._size - self._limit
                    first = self._parts[0]
                    if len(first) <= excess:
                        self._parts.pop(0)
                        self._size -= len(first)
                    else:
                        self._parts[0] = first[excess:]
                        self._size -= excess
                    self.truncated = True
        finally:
            self._stream.close()


def execute(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    capture_limit: int | None = _MAX_CAPTURE_CHARS,
    reap_escaped: bool = True,
    stall_after_s: float | None = None,
) -> CommandResult:
    """Run one trusted check until completion or explicit cancellation.

    ``capture_limit`` bounds what is kept of each stream (the tail): right for a check's log, which a
    person reads. ``None`` keeps everything: for output the harness parses (``exec.run``), where a cut
    is a wrong answer, not a shorter one.

    ``reap_escaped`` marks the command's environment (``LAUNCH_MARKER``) and, once it ends, stops
    anything it started that left its process group. The harness's own short commands (git, gh)
    through ``exec.run`` skip the scan.

    ``stall_after_s`` applies the progress rule: the command (and everything it started) is stopped and
    reported ``stalled`` once it has written no output and used next to no CPU for that long. It is
    never a limit on how long working code may run; it is for commands an agent wrote, which the
    harness must not wait on forever when they block (``swarm_measure``)."""
    started = time.monotonic()
    marker = ""
    if reap_escaped:
        marker = new_launch_marker()
        env = {**env, LAUNCH_MARKER: marker}
    proc = None
    for attempt, delay in enumerate((0.0, 0.1, 0.25, 0.5)):
        if delay:
            time.sleep(delay)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name == "posix",
            )
            break
        except OSError as exc:
            if exc.errno not in _TRANSIENT_SPAWN_ERRNOS or attempt == 3:
                raise
    assert proc is not None
    stdout_stream = _BoundedStream(proc.stdout, capture_limit)
    stderr_stream = _BoundedStream(proc.stderr, capture_limit)
    stdout_stream.start()
    stderr_stream.start()
    stalled, finished = threading.Event(), threading.Event()
    if stall_after_s:
        threading.Thread(target=_watch_progress, daemon=True,
                         args=(proc, marker, [stdout_stream, stderr_stream], float(stall_after_s),
                               stalled, finished)).start()
    try:
        proc.wait()
        finished.set()
        _reap_leftovers(proc)
        if marker:
            reap_marked(marker)
        stdout, stderr = stdout_stream.join(), stderr_stream.join()
        if stalled.is_set():
            stderr += (f"\n[harness] stopped: no output and next to no CPU for {float(stall_after_s):.0f} s "
                       "(the command and everything it started were idle; it was waiting on something "
                       "that never came)\n")
        return CommandResult(argv, (proc.returncode or 0) if not stalled.is_set() else (proc.returncode or 1),
                             stdout, stderr, round(time.monotonic() - started, 3),
                             output_truncated=stdout_stream.truncated or stderr_stream.truncated,
                             stalled=stalled.is_set())
    except KeyboardInterrupt:
        finished.set()
        _terminate_process_group(proc)
        if marker:
            reap_marked(marker)
        stdout, stderr = stdout_stream.join(), stderr_stream.join()
        return CommandResult(argv, 130, stdout, stderr,
                             round(time.monotonic() - started, 3), cancelled=True,
                             output_truncated=stdout_stream.truncated or stderr_stream.truncated)


def probe(argv: list[str], *, cwd: str, env: dict[str, str]) -> dict[str, str | int | bool]:
    """Return executable evidence without treating an unavailable runtime as code failure."""
    executable = executable_for(argv, cwd=cwd, env=env)
    if not executable:
        return {"available": False, "reason": f"executable not found: {argv[0]}"}
    try:
        result = execute(argv, cwd=cwd, env=env)
    except OSError as exc:
        return {"available": False, "executable": executable, "reason": str(exc)}
    output = (result.stdout or result.stderr).strip().replace("\n", " ")
    return {"available": result.passed, "executable": executable, "exitCode": result.exit_code,
            "version": output[:500], "reason": "" if result.passed else output[:500]}


def runtime_capabilities(*, isolated: bool) -> dict[str, dict]:
    """Execute every supported runtime probe in inherited or isolated mode."""
    with tempfile.TemporaryDirectory(prefix="conductor-runtime-health-") as state_dir:
        env = isolated_environment(state_dir) if isolated else inherited_environment()
        return {name: probe(argv, cwd=state_dir, env=env)
                for name, argv in _RUNTIME_PROBES.items()}


def runtime_health_report() -> dict:
    """Return an executable, two-environment proof for supported runtimes."""
    inherited = runtime_capabilities(isolated=False)
    isolated = runtime_capabilities(isolated=True)
    families = {}
    for family, tools in _RUNTIME_FAMILIES.items():
        inherited_ok = all(bool(inherited[tool].get("available")) for tool in tools)
        isolated_ok = all(bool(isolated[tool].get("available")) for tool in tools)
        families[family] = {
            "requiredTools": list(tools),
            "inheritedAvailable": inherited_ok,
            "isolatedAvailable": isolated_ok,
            "available": inherited_ok and isolated_ok,
            "required": family not in _OPTIONAL_RUNTIME_FAMILIES,
        }
    lease_renewal_enabled = os.environ.get(
        "CONDUCTOR_WORKER_ALL_LEASE_EXTEND_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    return {
        "healthy": all(item["available"] for item in families.values()
                       if item["required"]) and lease_renewal_enabled,
        "optionalFamilies": sorted(_OPTIONAL_RUNTIME_FAMILIES),
        "families": families,
        "executionDeadlinePolicy": "run-until-completion-or-explicit-cancellation",
        "leaseRenewalEnabled": lease_renewal_enabled,
        "idleSleepInhibitionActive": idle_sleep_inhibition_active(),
        "idleSleepInhibitionMechanism": "caffeinate -i" if sys.platform == "darwin" else "not-required",
        "typescriptCompilerPolicy": "repository-local compiler invoked through npm/npx",
        "inheritedCapabilities": inherited,
        "isolatedCapabilities": isolated,
    }


# ─────────────────────────── sandboxed checks (H17, closure plan WP9) ───────────────────────────
# Agent SESSIONS are OS-sandboxed by the Agent SDK; the repository CHECKS a verifier runs execute agent-
# written code too, so they get the same containment: writes only inside the disposable checkout, the
# verifier's scratch/cache dirs and the per-user package caches; no network except loopback. A missing
# sandbox tool is a named block (``sandbox_unavailable``), never a silent unconfined run.
_HOME_CACHES = (".cache", "Library/Caches", ".gradle", ".m2", ".npm", ".cargo", "go", ".pytest_cache")


class SandboxUnavailable(RuntimeError):
    pass


def _sbpl_quote(path: str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_profile(writable: list[str], *, allow_network: bool = False) -> str:
    """A Seatbelt (SBPL) profile: read anywhere, write only under ``writable`` (+ temp/devices), and —
    unless ``allow_network`` — no network except loopback and Unix sockets."""
    paths = [os.path.realpath(p) for p in writable if p]
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             "(allow file-write* " + " ".join(f"(subpath {_sbpl_quote(p)})" for p in paths)
             + ' (subpath "/private/tmp") (subpath "/private/var/folders") (subpath "/dev"))']
    if not allow_network:
        # Only the REMOTE side may name loopback for outbound: a (local ip "localhost:*") filter matches
        # every socket's local side and would re-open the internet (measured on macOS Seatbelt).
        lines += ["(deny network*)",
                  '(allow network-outbound (remote ip "localhost:*"))',
                  '(allow network-bind (local ip "localhost:*"))',
                  '(allow network-inbound (local ip "localhost:*"))',
                  "(allow network* (local unix-socket) (remote unix-socket))"]
    return "\n".join(lines) + "\n"


def sandboxed_argv(argv: list[str], *, cwd: str, writable: list[str] | None = None,
                   allow_network: bool = False) -> list[str]:
    """Wrap a check's argv in the platform OS sandbox (macOS ``sandbox-exec``, Linux ``bwrap``)."""
    home = os.path.expanduser("~")
    roots = [cwd, *(writable or []), *(os.path.join(home, c) for c in _HOME_CACHES),
             tempfile.gettempdir()]
    if sys.platform == "darwin":
        tool = shutil.which("sandbox-exec") or ("/usr/bin/sandbox-exec" if os.path.exists("/usr/bin/sandbox-exec") else None)
        if not tool:
            raise SandboxUnavailable("sandbox-exec not found")
        return [tool, "-p", macos_profile(roots, allow_network=allow_network), *argv]
    tool = shutil.which("bwrap")
    if not tool:
        raise SandboxUnavailable("bubblewrap (bwrap) not installed; install it to run sandboxed checks")
    args = [tool, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--die-with-parent"]
    for root in roots:
        if os.path.isdir(root):
            args += ["--bind", root, root]
    if not allow_network:
        args += ["--unshare-net"]
    return [*args, "--chdir", cwd, *argv]
