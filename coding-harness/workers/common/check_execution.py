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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_BASE_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "TERM",
)
_RUNTIME_ENV_KEYS = (
    "JAVA_HOME", "GRADLE_HOME", "MAVEN_HOME", "M2_HOME", "NODE_HOME",
    "PYENV_ROOT", "ASDF_DIR", "RUSTUP_HOME", "CARGO_HOME", "GOROOT",
    # Toolchain installation/configuration paths are not credentials. Preserve
    # them so launchd/systemd workers behave like the trusted interactive host
    # for Android, Apple/Swift, C/C++, Go, and .NET repositories.
    "ANDROID_HOME", "ANDROID_SDK_ROOT", "DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS",
    "CC", "CXX", "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH", "VCPKG_ROOT", "CONAN_HOME",
    "GOPATH", "DOTNET_ROOT", "MSBuildSDKsPath", "NUGET_PACKAGES", "SWIFT_EXEC",
)
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
    java_home = resolve_java_home(java_version)
    runtime_bins = runtime_bin_directories()
    if java_home:
        target["JAVA_HOME"] = java_home
        runtime_bins.insert(0, str(Path(java_home) / "bin"))
    path_parts = [part for part in target.get("PATH", "").split(os.pathsep) if part]
    target["PATH"] = os.pathsep.join([
        *runtime_bins,
        *[part for part in path_parts if part not in runtime_bins],
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
    """Create a repeatable command environment with per-run homes and caches.

    ``required_env`` is deliberately name-based: attached campaign profiles
    can pass a declared endpoint or feature flag without inheriting every
    credential and incidental shell setting from the worker process.
    """
    root = Path(state_dir)
    home = root / "home"
    cache = Path(dependency_cache_dir) if dependency_cache_dir else root / "cache"
    for directory in (home, cache, home / ".config", home / ".local" / "share"):
        directory.mkdir(parents=True, exist_ok=True)

    env = {key: os.environ[key] for key in _BASE_ENV_KEYS if os.environ.get(key)}
    # Service managers commonly provide only /usr/bin:/bin.  Resolve the
    # ordinary package-manager locations explicitly so a worker launched by
    # launchd/systemd sees the same installed runtimes as an interactive shell.
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    for part in ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
                 "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if Path(part).is_dir() and part not in path_parts:
            path_parts.append(part)
    env["PATH"] = os.pathsep.join(path_parts)
    # Tool-manager paths are intentionally not copied.  A manager's shim often
    # points into its original HOME, which defeats cache isolation and produces
    # version drift.  Explicit runtime homes remain supported below.
    for key in _RUNTIME_ENV_KEYS:
        if key in {"JAVA_HOME", "CARGO_HOME", "RUSTUP_HOME"}:
            continue
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "GRADLE_USER_HOME": str(cache / "gradle"),
        "MAVEN_USER_HOME": str(cache / "maven"),
        "MAVEN_OPTS": f"-Dmaven.repo.local={cache / 'm2'}",
        "npm_config_cache": str(cache / "npm"),
        "npm_config_userconfig": str(home / ".npmrc"),
        "PNPM_HOME": str(cache / "pnpm-home"),
        "YARN_CACHE_FOLDER": str(cache / "yarn"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        # Do not override GEM_HOME: doing so hides gems installed with the
        # runtime itself and can make /usr/bin/bundle fail before it reads a
        # repository Gemfile. Keep downloaded metadata and bundle installs in
        # the per-run cache without changing Ruby's runtime load path.
        "GEM_SPEC_CACHE": str(cache / "ruby" / "specs"),
        "BUNDLE_USER_HOME": str(cache / "ruby" / "bundle-user"),
        "BUNDLE_PATH": str(cache / "ruby" / "bundle"),
        "CARGO_HOME": str(cache / "cargo"),
        # rustup toolchains are installations, not disposable build state. A
        # fresh empty RUSTUP_HOME makes an installed cargo proxy fail with
        # "no default toolchain". Preserve an explicitly configured install
        # while keeping Cargo's downloaded dependency cache isolated/shared.
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        "GOMODCACHE": str(cache / "go" / "pkg" / "mod"),
        "GOCACHE": str(cache / "go" / "build"),
        # These deliberately shadow any host value so a verification run cannot
        # write into the operator's shared package caches, matching the GEM_*
        # and CARGO_HOME treatment above.
        "NUGET_PACKAGES": str(cache / "nuget"),
        "DOTNET_CLI_HOME": str(home / "dotnet"),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "COMPOSER_HOME": str(cache / "composer"),
        "COMPOSER_CACHE_DIR": str(cache / "composer" / "cache"),
        "COMPOSER_NO_INTERACTION": "1",
        "MIX_HOME": str(cache / "mix"),
        "HEX_HOME": str(cache / "hex"),
        "COURSIER_CACHE": str(cache / "coursier"),
        # Browser binaries are provisioned on the host, never downloaded by a
        # verification run; these only point at where a provisioned cache lives.
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH", str(cache / "playwright")),
        "CYPRESS_CACHE_FOLDER": os.environ.get(
            "CYPRESS_CACHE_FOLDER", str(cache / "cypress")),
    })
    for value in required_env:
        name = str(value)
        if os.environ.get(name) is not None:
            env[name] = os.environ[name]
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

    def __init__(self, stream, limit: int = _MAX_CAPTURE_CHARS):
        self._stream = stream
        self._limit = limit
        self._parts: list[str] = []
        self._size = 0
        self.truncated = False
        self._thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> str:
        self._thread.join()
        return "".join(self._parts)

    def _drain(self) -> None:
        try:
            while chunk := self._stream.read(64 * 1024):
                self._parts.append(chunk)
                self._size += len(chunk)
                while self._size > self._limit and self._parts:
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
) -> CommandResult:
    """Run one trusted check until completion or explicit cancellation."""
    started = time.monotonic()
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
    stdout_stream = _BoundedStream(proc.stdout)
    stderr_stream = _BoundedStream(proc.stderr)
    stdout_stream.start()
    stderr_stream.start()
    try:
        proc.wait()
        stdout, stderr = stdout_stream.join(), stderr_stream.join()
        return CommandResult(argv, proc.returncode or 0, stdout, stderr,
                             round(time.monotonic() - started, 3),
                             output_truncated=stdout_stream.truncated or stderr_stream.truncated)
    except KeyboardInterrupt:
        _terminate_process_group(proc)
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


def runtime_evidence(argv: list[str], *, cwd: str, env: dict[str, str]) -> dict:
    """Describe the executable/runtime used by one repository command."""
    entrypoint = argv[0] if argv else ""
    evidence = {"entrypoint": entrypoint,
                "executable": executable_for(argv, cwd=cwd, env=env) or ""}
    if entrypoint in JAVA_ENTRYPOINTS:
        java_home = env.get("JAVA_HOME", "")
        java = str(Path(java_home, "bin", "java")) if java_home else "java"
        return {**evidence, "runtime": "java", "javaHome": java_home,
                "java": probe([java, "-version"], cwd=cwd, env=env)}
    return evidence


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
