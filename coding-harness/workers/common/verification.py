"""Deterministic, read-only build discovery and candidate verification.

Coding agents may suggest commands, but they never get authority to execute a
verification command.  This module derives a small argv-only command set from
repository evidence and runs it in a detached disposable worktree at an exact
commit.  It deliberately has no shell fallback.
"""

from __future__ import annotations

import os
import json
import time
import fnmatch
import re
import shlex
import shutil
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from hashlib import sha256
from pathlib import Path

from .exec import run
from . import git
from . import check_execution


_GUIDES = ("AGENTS.md", "AGENT.md", "CLAUDE.md", "CONTRIBUTING.md", "README.md")
_SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "<<", "&"}
_ENTRYPOINTS = {
    "./gradlew", "gradle", "./mvnw", "mvn", "npm", "pnpm", "yarn",
    "pytest", "go", "cargo", "make", "cmake", "ctest", "swift",
    "bundle", "dotnet", "composer", "mix", "sbt", "bazel",
}
_RAKE_GOALS = {"test", "spec", "default", "check"}
_DOTNET_GOALS = {"test", "build", "restore"}
_COMPOSER_GOALS = {"test", "run-script", "install"}
_MIX_GOALS = {"test", "compile", "deps.get"}
# npx is deliberately absent from _ENTRYPOINTS: it resolves and fetches from the
# network at run time, so the command that runs is not the command validated.
_JS_RUNNERS = {"jest", "vitest", "mocha", "ava", "tap"}
_BROWSER_RUNNERS = {"playwright", "cypress"}
_GRADLE_GOALS = {"test", "check", "build"}
_MAVEN_GOALS = {"test", "verify", "package"}
_MAKE_GOALS = {"test", "check", "build"}
_GENERATED_PARTS = {".gradle", ".gradle-local", ".conductor-verify-build", "build", "target", "dist",
                    "out", "coverage", "__pycache__", ".pytest_cache", ".ruff_cache", ".tox"}
# Directories a manifest scan must never descend into. Deliberately wider than
# _GENERATED_PARTS, which also classifies *changed* paths: a vendored tree is
# not a generated build output, but scanning it would attribute a dependency's
# manifest to this repository.
_SCAN_SKIP_PARTS = _GENERATED_PARTS | {"node_modules", "vendor", "_build", "deps", "bin", "obj", ".venv"}
_JAVA_ENTRYPOINTS = check_execution.JAVA_ENTRYPOINTS
_HEAVY_MARKERS = ("acceptance", "benchmark", "docker", "e2e", "integration", "performance", "publish", "release", "spotless", "test-harness")
# Flags whose value slot carries one already-selected test's identifier (an
# exact class name, a workspace/package name, a rake/CTest name filter)
# rather than a task/script/module name a human or agent chose freely. Every
# one of this module's own changed-scope planners derives that value
# mechanically from a real changed file or package (the Java/Kotlin FQCN in
# _targeted_gradle_candidates/_targeted_maven_candidates, the workspace name
# in _targeted_javascript_candidates, the class list in the PHP/composer
# planner, ...), so scanning it for "e2e"/"integration"/etc. only ever
# produces a false block when the test/class/package itself happens to be
# named that -- which does not make running that one target broad. A heavy
# TASK or SCRIPT name (Gradle's own e2eTest, a Makefile target,
# run-integration-suite.sh) sits in a different argv slot -- the
# goal/entrypoint position, checked separately by each tool's own goal
# allowlist above -- and stays untouched by this exemption.
_FILTER_FLAGS_WITH_SEPARATE_VALUE = {"--tests", "--filter", "-R"}
_FILTER_VALUE_PREFIXES = ("-Dtest=", "TEST=", "SPEC=")
_GRADLE_PROJECT_DIR = re.compile(
    r"project\(\s*['\"]:(?P<name>[^'\"]+)['\"]\s*\)\s*\.projectDir\s*=\s*file\(\s*['\"](?P<directory>[^'\"]+)['\"]\s*\)"
)
_GRADLE_ROOT_NAME = re.compile(r"rootProject\.name\s*=\s*['\"](?P<value>[^'\"]+)['\"]")
_GRADLE_ROOT_CHILD_RENAME = re.compile(
    r"rootProject\.children\.each\s*\{[^\n]*?\.name\s*=\s*['\"](?P<prefix>[^'\"]*)\$\{[^}]*\.name\}(?P<suffix>[^'\"]*)['\"]"
)
_GRADLE_INCLUDE_LINE = re.compile(r"\binclude\s*(?:\(|\s)")
_QUOTED_VALUE = re.compile(r"['\"](?P<value>[^'\"]+)['\"]")
_JAVA_TOOLCHAIN = re.compile(r"JavaLanguageVersion\.of\(\s*(?P<version>\d+)")
_XML_JAVA_VERSION = re.compile(r"<(?:maven\.compiler\.(?:release|source|target)|java\.version)>(?P<version>[^<]+)</")
_NODE_ENGINE = re.compile(r'"node"\s*:\s*"(?P<constraint>[^"]+)"')
_GO_VERSION = re.compile(r"^go\s+(?P<version>\d+(?:\.\d+)?)", re.MULTILINE)
_RUST_CHANNEL = re.compile(r"channel\s*=\s*['\"](?P<version>[^'\"]+)")
_TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]+|[^/]+_(?:test|tests))\.py$")
_JVM_TEST_PATH = re.compile(r"^(?:(?P<module>.*?)/)?src/test/(?:java|kotlin)/(?P<class>.+)\.(?:java|kt)$")
_NO_TEST_EXTENSIONS = {".md", ".mdx", ".rst", ".adoc", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                       # Compiled or cached output: never a reason to demand a test.
                       ".pyc", ".pyo", ".class", ".o", ".obj", ".lock"}
_BUILD_METADATA_NAMES = {
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradle.properties",
    "pom.xml", "package.json", "pnpm-workspace.yaml", "yarn.lock", "package-lock.json", "pnpm-lock.yaml",
    "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
}


class VerificationBlocked(ValueError):
    """The repository did not provide safe, sufficient verification evidence."""

    def __init__(self, message: str, *, changed_paths: list[str] | None = None) -> None:
        super().__init__(message)
        # Carried so a caller that must fail soft (test_discover's invalid-input branch) can still
        # report what was changed.
        self.changed_paths = list(changed_paths) if changed_paths else []


def _git_root(path: Path) -> Path:
    resolved = git.git(str(path), "rev-parse", "--show-toplevel", check=False)
    if resolved.code != 0 or not resolved.stdout.strip():
        raise VerificationBlocked(f"repoPath is not a git worktree: {path}")
    return Path(resolved.stdout.strip()).resolve()


def _read_first(root: Path, names: tuple[str, ...]) -> str:
    for name in names:
        path = root / name
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                continue
    return ""


def runtime_requirements(repo_path: str | Path) -> dict[str, str]:
    """Read pinned runtime versions from ordinary repository metadata.

    This is evidence only: it never executes package scripts or CI YAML.  The
    returned requirements are later proved by a probe in the final check env.
    """
    root = Path(repo_path)
    requirements: dict[str, str] = {}
    java_text = _read_first(root, (".java-version", "gradle.properties", "build.gradle", "build.gradle.kts", "pom.xml"))
    java = _JAVA_TOOLCHAIN.search(java_text) or _XML_JAVA_VERSION.search(java_text)
    if not java and (root / ".java-version").is_file():
        value = java_text.strip()
        if value:
            requirements["java"] = re.search(r"\d+", value).group(0) if re.search(r"\d+", value) else value
    elif java:
        requirements["java"] = java.group("version").strip()

    node_text = _read_first(root, (".nvmrc", ".node-version", "package.json"))
    if (root / ".nvmrc").is_file() or (root / ".node-version").is_file():
        match = re.search(r"\d+", node_text)
    else:
        match = _NODE_ENGINE.search(node_text)
    if match:
        requirements["node"] = match.groupdict().get("constraint") or match.group(0)

    python_text = _read_first(root, (".python-version", "pyproject.toml"))
    match = re.search(r"(?:python\s*[=~><! ]*|requires-python\s*=\s*['\"]?[=~><! ]*)(?P<version>\d+(?:\.\d+)?)", python_text, re.I)
    if not match and (root / ".python-version").is_file():
        match = re.search(r"(?P<version>\d+(?:\.\d+)?)", python_text)
    if match:
        requirements["python"] = match.group("version")
    # requires-python is a range, like package.json's engines. Reading ">=3.9" as the bare version 3.9
    # refused every newer interpreter as infrastructure failure (run c2ccb06d: Python 3.14.5).
    declared = re.search(r"requires-python\s*=\s*['\"](?P<range>[^'\"]+)['\"]", python_text, re.I)
    if declared:
        requirements["python"] = " ".join(declared.group("range").replace(",", " ").split())

    go = _GO_VERSION.search(_read_first(root, ("go.mod",)))
    if go:
        # The `go` directive is the MINIMUM language version the module needs, never a toolchain pin:
        # read as a bare version it refused go 1.26 for a module declaring `go 1.21`, the program's
        # integration check stayed red at every head and the Architect's ladder looped (run 3c84dd5d).
        requirements["go"] = ">=" + go.group("version")
    rust = _RUST_CHANNEL.search(_read_first(root, ("rust-toolchain.toml", "rust-toolchain")))
    if rust:
        requirements["rust"] = rust.group("version")
    dotnet = re.search(r'"version"\s*:\s*"(?P<version>[0-9][^"]*)"',
                       _read_first(root, ("global.json",)))
    if dotnet:
        requirements["dotnet"] = dotnet.group("version")
    elixir = re.search(r"elixir:\s*['\"][^0-9]*(?P<version>\d+(?:\.\d+)*)",
                       _read_first(root, ("mix.exs",)))
    if elixir:
        requirements["elixir"] = elixir.group("version")
    ruby_text = _read_first(root, (".ruby-version", "Gemfile"))
    ruby = re.search(r"^\s*ruby\s+['\"](?P<version>[0-9][^'\"]*)", ruby_text, re.M)
    if not ruby and (root / ".ruby-version").is_file():
        ruby = re.search(r"(?P<version>\d+(?:\.\d+)*)", ruby_text)
    if ruby:
        requirements["ruby"] = ruby.group("version")
    return requirements


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _candidate_from_guide(text: str, source: str, *, allow_heavy: bool = False,
                          root: Path | None = None) -> list[dict]:
    """Extract documented single argv build/test commands, never CI commands."""
    found: list[dict] = []
    snippets = re.findall(r"`([^`\n]+)`", text)
    snippets.extend(line.strip() for line in text.splitlines())
    for raw_command in snippets:
        try:
            argv = shlex.split(raw_command)
            validate_argv(argv, allow_heavy=allow_heavy, root=root)
        # shlex.split itself and the argv validator both use ValueError for
        # malformed or unsupported commands.  Do not reference a shlex-specific
        # exception: its public name differs across Python versions.
        except ValueError:
            continue
        found.append({"argv": argv, "source": source, "kind": "documented"})
    return found


def _is_lightweight(argv: list[str], root: Path | None = None) -> bool:
    """Keep verification to ordinary build/test targets, never end-to-end CI suites.

    A marker only disqualifies a command when it appears in a token the caller
    chose, never in one that merely names a file the repository happens to
    store under ``integration/`` or ``e2e/``.  Matching on paths made a project
    unverifiable purely because of how it organizes its tests. The same
    principle applies to a single-target filter flag's value (see
    _is_target_filter_value): a test class genuinely named ...E2ETest is not
    a broad suite merely because its author chose that name, when the
    invocation still runs exactly that one class.
    """
    for index, value in enumerate(argv):
        if not any(marker in value.lower() for marker in _HEAVY_MARKERS):
            continue
        if root is not None and _names_existing_path(root, value):
            continue
        if _is_target_filter_value(argv, index):
            continue
        return False
    return True


def _is_target_filter_value(argv: list[str], index: int) -> bool:
    """True when argv[index] sits in the VALUE slot of a single-target filter flag.

    Purely shape/position based, never string based: this asks which argv
    slot a token occupies, never what characters it contains. A heavy TASK or
    SCRIPT name occupies a different slot (the goal/entrypoint position) and
    is untouched -- see the module comment above _FILTER_FLAGS_WITH_SEPARATE_VALUE.
    """
    value = argv[index]
    if value.startswith(_FILTER_VALUE_PREFIXES):
        return True
    return index > 0 and argv[index - 1] in _FILTER_FLAGS_WITH_SEPARATE_VALUE


def _names_existing_path(root: Path, value: str) -> bool:
    """True when an argv token resolves to a real path inside the repository."""
    candidate = value.split("=", 1)[-1] if "=" in value else value
    candidate = candidate.split("::", 1)[0].lstrip("./")
    if not candidate or Path(candidate).is_absolute():
        return False
    try:
        target = (root / candidate).resolve()
        target.relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return target.exists()


def validate_argv(argv: object, *, allow_heavy: bool = False, root: Path | None = None) -> list[str]:
    """Validate a verification command as argv, never a shell command string.

    ``allow_heavy`` is only ever set by an explicit repository-scope request.
    It is never inferred, because relaxing the marker filter turns a documented
    command in a guide into an execution vector.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        raise VerificationBlocked("verification command must be a non-empty argv array")
    values = [str(value) for value in argv]
    if any(not value or value in _SHELL_TOKENS or any(mark in value for mark in ("|", ";", ">", "<", "`", "$("))
           for value in values):
        raise VerificationBlocked("shell syntax is not allowed in verification commands")
    command = values[0]
    if command not in _ENTRYPOINTS:
        raise VerificationBlocked(f"unsupported verification entrypoint: {command}")
    if command in {"npm", "pnpm", "yarn"} and "test" not in values[1:]:
        raise VerificationBlocked(f"unsupported {command} verification invocation")
    if command in {"go", "cargo"}:
        # `go` alone has no "manifest path" flag; `-C <dir>` (Go 1.20+) is the
        # only way to point it at a nested module, so it is the one prefix
        # allowed before the goal itself.
        after_goal_flag = values[1:]
        if command == "go" and after_goal_flag[:1] == ["-C"]:
            after_goal_flag = after_goal_flag[2:]
        if not after_goal_flag or after_goal_flag[0] not in {"test", "build", "check", "vet"}:
            raise VerificationBlocked(f"unsupported {command} verification invocation")
    if command in _JAVA_ENTRYPOINTS:
        # Options whose NEXT argv slot holds a value rather than a goal.
        # Gradle's -p/--project-dir names a project directory: without it
        # listed here that directory was counted as a second "goal", so every
        # nested-project `gradle -p <dir> test` candidate this module infers
        # was rejected as having no single explicit goal.
        options_with_values = {"--tests", "-pl", "--projects", "-f", "--file",
                               "-p", "--project-dir"}
        goals: list[str] = []
        skip = False
        for value in values[1:]:
            if skip:
                skip = False
                continue
            if value in options_with_values:
                skip = True
                continue
            if value.startswith("-"):
                continue
            goals.append(value)
        if len(goals) != 1:
            raise VerificationBlocked(f"{command} requires exactly one explicit build or test goal")
        goal = goals[0].rsplit(":", 1)[-1].lower()
        allowed = _GRADLE_GOALS if command in {"./gradlew", "gradle"} else _MAVEN_GOALS
        if goal not in allowed:
            raise VerificationBlocked(f"unsupported {command} verification goal: {goals[0]}")
    if command == "make" and (len(values) < 2 or (values[1] not in _MAKE_GOALS
                                                  and not _TEST_TARGET.search(values[1]))):
        raise VerificationBlocked("make requires a build/test goal or a test-shaped target")
    if command == "dotnet" and (len(values) < 2 or values[1] not in _DOTNET_GOALS):
        raise VerificationBlocked("dotnet requires one of: test, build, restore")
    if command == "composer" and (len(values) < 2 or values[1] not in _COMPOSER_GOALS):
        raise VerificationBlocked("composer requires one of: test, run-script, install")
    if command == "mix" and (len(values) < 2 or values[1] not in _MIX_GOALS):
        raise VerificationBlocked("mix requires one of: test, compile, deps.get")
    if command == "sbt":
        # sbt parses its own command line, so one element may contain a space
        # ("core/testOnly a.b.C"), but the task itself must still be a test task.
        task = values[1].split("/")[-1].split()[0] if len(values) == 2 and values[1].strip() else ""
        if len(values) != 2 or task not in {"test", "testOnly", "testQuick"}:
            raise VerificationBlocked("sbt requires a single test, testOnly, or testQuick task")
    if command == "bazel" and (len(values) < 2 or values[1] not in {"test", "build"}):
        raise VerificationBlocked("bazel requires test or build")
    if command == "bundle":
        if values[1:2] == ["install"]:
            pass
        elif values[1:2] != ["exec"] or values[2:3] not in (["rspec"], ["rake"]):
            raise VerificationBlocked("bundle must run `exec rspec` or `exec rake`")
        elif values[2] == "rake" and values[3:4] and values[3] not in _RAKE_GOALS:
            raise VerificationBlocked(f"unsupported rake goal: {values[3]}")
    if not allow_heavy and not _is_lightweight(values, root):
        raise VerificationBlocked("heavyweight verification targets are not allowed")
    return values


def validate_configured_argv(argv: object, *, allow_heavy: bool = False,
                             root: Path | None = None) -> list[str]:
    """Validate a repository-mapped scoped command without a tool allowlist."""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise VerificationBlocked("configured verification command must be a non-empty argv array")
    values = [str(value) for value in argv]
    if any(not value or value in _SHELL_TOKENS or any(mark in value for mark in ("|", ";", ">", "<", "`", "$("))
           for value in values):
        raise VerificationBlocked("shell syntax is not allowed in configured verification commands")
    command = Path(values[0]).name.lower()
    interpreters = {"sh", "bash", "zsh", "fish", "cmd", "cmd.exe", "powershell", "pwsh",
                    "python", "python3", "node", "ruby", "perl", "env", "xargs", "sudo"}
    if command in interpreters:
        raise VerificationBlocked(f"generic interpreter is not a verification entrypoint: {values[0]}")
    if not allow_heavy and not _is_lightweight(values, root):
        raise VerificationBlocked("heavyweight verification targets are not allowed")
    return values


def _prepared_scoped(entrypoint: str, argv: list[str], directory: str) -> list[str] | None:
    """Insert this tool's own "run in this directory" flag; unchanged at the root.

    ``None`` means this entrypoint has no known directory-scoped form yet, so a
    nested-only occurrence is left undetected rather than emitting a command
    that would silently run against the wrong directory (or the root, which
    may have no manifest at all).
    """
    if directory == ".":
        return argv
    if entrypoint == "npm":
        return [argv[0], "--prefix", directory, *argv[1:]]
    if entrypoint == "pnpm":
        return [argv[0], "--dir", directory, *argv[1:]]
    if entrypoint == "yarn":
        return [argv[0], "--cwd", directory, *argv[1:]]
    if entrypoint == "composer":
        return [*argv, f"--working-dir={directory}"]
    return None


def _tracked(root: Path) -> set[str] | None:
    """Paths git tracks under ``root``, relative to it; ``None`` outside a repository, where what is
    on disk is all there is."""
    result = git.git(str(root), "ls-files", "-z", check=False)
    if result.code != 0:
        return None
    return {p for p in result.stdout.split("\0") if p}


def _committed(root: Path, path: Path, tracked: set[str] | None) -> bool:
    return tracked is None or _relative(root, path) in tracked


# Build systems that resolve dependencies inside the build itself: for them the test command IS the
# dependency install, so it needs the registry exactly as the prepare phase does.
_RESOLVES_DURING_BUILD = frozenset({"mvn", "./mvnw", "mvnw", "gradle", "./gradlew", "gradlew", "cargo", "go",
                                    "dotnet", "sbt", "mill", "bazel", "bazelisk", "swift"})


def _needs_network(command: dict) -> bool:
    """Whether a check command runs with the network on inside the OS sandbox.

    The PREPARE phase (the dependency install) needs the registry: a fresh folder has a cold dependency
    cache, and a sandboxed `npm install` sat in npm's retry backoff for 60 s or more in every final test
    cycle (runs 74bd988c, 377b3cd9). So does a build system that resolves while it builds: a sandboxed
    `mvn test` could not fetch the parent POM nor the surefire plugin ("Operation not permitted"), every
    item of three teams ended tests_failed_fix_unavailable, and each burned a repair session on a failure
    no code change fixes (run afb90941). Everything else runs with loopback only.
    """
    if command.get("phase") == "prepare":
        return True
    argv = command.get("argv") or []
    return bool(argv) and str(argv[0]) in _RESOLVES_DURING_BUILD


def _prepare_commands(root: Path) -> list[dict]:
    """Return the dependency install(s) a fresh clone needs before it can test.

    ``_disposable_checkout`` clones the candidate and installs nothing. Gradle,
    Maven, Cargo, Go and ``dotnet test`` resolve dependencies as part of the
    build, so they work. Node, Ruby, PHP and Elixir do not: without an install
    step their test runner is simply not on disk, and the command fails with
    "command not found" rather than a real test result.

    One command per nested project directory, not just the repository root --
    the harness's own real-e2e test repository keeps every scenario
    self-contained under its own directory precisely so concurrent/repeated
    runs never collide, and a manifest at the root is just the ``.`` case.
    """
    found: list[dict] = []
    covered: set[str] = set()
    # Only a TRACKED lock file counts: the verification clone has what is committed, not what an agent's
    # own `npm install` left in a shared working tree (calc27: `npm ci` ran in a clone with no lock file
    # and every item's cycle failed at the install step before a single test ran).
    tracked = _tracked(root)
    for detector, argv in (
        ("pnpm-lock.yaml", ["pnpm", "install", "--frozen-lockfile"]),
        ("package-lock.json", ["npm", "ci"]),
        ("Gemfile.lock", ["bundle", "install", "--jobs", "4"]),
        ("composer.lock", ["composer", "install", "--no-interaction", "--no-progress"]),
        ("mix.lock", ["mix", "deps.get"]),
    ):
        for manifest in _scan(root, detector):
            if not _committed(root, manifest, tracked):
                continue
            directory = _relative(root, manifest.parent) or "."
            scoped = _prepared_scoped(argv[0], argv, directory)
            if scoped is None or directory in covered:
                continue
            covered.add(directory)
            found.append({"argv": scoped, "source": f"prepare:{detector}:{directory}",
                          "kind": "prepare", "phase": "prepare",
                          "scope": "affected", "affectedUnit": f"dependencies:{directory}",
                          "coveredPaths": []})
    for manifest in _scan(root, "yarn.lock"):
        directory = _relative(root, manifest.parent) or "."
        if directory in covered or not _committed(root, manifest, tracked):
            continue
        berry = (manifest.parent / ".yarnrc.yml").is_file()
        argv = ["yarn", "install", "--immutable" if berry else "--frozen-lockfile"]
        scoped = _prepared_scoped("yarn", argv, directory)
        if scoped is None:
            continue
        covered.add(directory)
        found.append({"argv": scoped, "source": f"prepare:yarn.lock:{directory}", "kind": "prepare",
                      "phase": "prepare", "scope": "affected",
                      "affectedUnit": f"dependencies:{directory}", "coveredPaths": []})
    # A committed package.json with no committed lock file still needs its install: `npm install`.
    for manifest in _scan(root, "package.json"):
        directory = _relative(root, manifest.parent) or "."
        if directory in covered or not _committed(root, manifest, tracked):
            continue
        scoped = _prepared_scoped("npm", ["npm", "install", "--no-audit", "--no-fund"], directory)
        if scoped is None:
            continue
        covered.add(directory)
        found.append({"argv": scoped, "source": f"prepare:package.json:{directory}", "kind": "prepare",
                      "phase": "prepare", "scope": "affected",
                      "affectedUnit": f"dependencies:{directory}", "coveredPaths": []})
    return found


def validate_prepare_argv(argv: object, *, allow_heavy: bool = False,
                          root: Path | None = None) -> list[str]:
    """Accept only an exact, known dependency-install command.

    Matched against a literal table rather than a pattern: a prepare step runs
    before any test and is the one place a typo would become "install whatever
    this argv says". A leading directory-scope flag (npm ``--prefix <dir>``,
    pnpm ``--dir <dir>``, yarn ``--cwd <dir>``) is stripped before that literal
    match, then restored in the returned value -- the safety property (only a
    known-safe install shape ever runs) is unchanged; only its target directory
    varies.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        raise VerificationBlocked("prepare command must be a non-empty argv array")
    values = [str(value) for value in argv]
    canonical = values
    if len(values) >= 3 and (values[0], values[1]) in (("npm", "--prefix"), ("pnpm", "--dir"), ("yarn", "--cwd")):
        canonical = [values[0], *values[3:]]
    elif values and values[0] == "composer" and values[-1].startswith("--working-dir="):
        canonical = values[:-1]
    if canonical not in _PREPARE_ARGVS:
        raise VerificationBlocked(
            "unsupported dependency preparation command: " + " ".join(values))
    return values


_PREPARE_ARGVS = [
    ["pnpm", "install", "--frozen-lockfile"],
    ["npm", "ci"],
    ["npm", "install", "--no-audit", "--no-fund"],
    ["yarn", "install", "--frozen-lockfile"],
    ["yarn", "install", "--immutable"],
    ["bundle", "install", "--jobs", "4"],
    ["composer", "install", "--no-interaction", "--no-progress"],
    ["mix", "deps.get"],
    ["dotnet", "restore"],
]


def validate_repository_argv(argv: object, *, allow_heavy: bool = False,
                             root: Path | None = None) -> list[str]:
    """Validate a repository-wide command for a deliberate full-suite run: the entrypoint allowlist,
    shell-syntax refusal and per-tool goal rules of :func:`validate_argv`, with heavy suites admitted
    when the caller asked for them."""
    return validate_argv(argv, allow_heavy=allow_heavy, root=root)


_BROWSER_CONFIG_NAMES = ("playwright.config.ts", "playwright.config.js",
                        "cypress.config.ts", "cypress.config.js")
# The script's own first token must be a real browser-test binary, never
# `npx`: npx resolves and fetches from the network at run time, so the
# command that runs would not be the command this validated.
_BROWSER_SCRIPT_NAMES = ("test:e2e", "e2e", "test:browser", "playwright:test", "cypress:run")


def _browser_suite_configured(root: Path) -> bool:
    return any((root / name).exists() for name in _BROWSER_CONFIG_NAMES)


def browser_suite_reason(root: Path) -> str | None:
    """Explain why a browser suite cannot run here, or None when it can.

    Browser binaries are provisioned on the host. A verification run must never
    fetch them: `playwright install --with-deps` downloads binaries and OS
    packages, which is neither reproducible nor something an automated repair
    loop should trigger.
    """
    if not _browser_suite_configured(root):
        return "repository declares no browser test configuration"
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.environ.get("CYPRESS_CACHE_FOLDER")
    if not cache or not Path(cache).is_dir() or not any(Path(cache).iterdir()):
        return ("verification worker has no provisioned browser binaries; install them on the "
                "host and set PLAYWRIGHT_BROWSERS_PATH or CYPRESS_CACHE_FOLDER")
    return None


def _browser_suite_candidate(root: Path) -> dict | None:
    """Return the repository's dedicated e2e/browser script, if one is safe to run.

    Deliberately narrower than the ordinary npm-script path: the script's own
    first token must literally name a known browser runner. A repository
    without such a script has no dedicated suite this can offer -- its
    ordinary `test` script, if any, is already covered by full-mode inference
    and is not this function's concern.
    """
    package_path = root / "package.json"
    if not package_path.is_file():
        return None
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = package.get("scripts") or {}
    for name in _BROWSER_SCRIPT_NAMES:
        value = str(scripts.get(name) or "").strip()
        if not value:
            continue
        try:
            tokens = shlex.split(value)
        except ValueError:
            continue
        if not tokens or Path(tokens[0]).name not in _BROWSER_RUNNERS:
            continue
        manager = ("pnpm" if (root / "pnpm-lock.yaml").is_file()
                   else "yarn" if (root / "yarn.lock").is_file() else "npm")
        argv = [manager, name] if manager == "yarn" else [manager, "run", name]
        return {"argv": argv, "adapter": "browser-suite",
                "source": f"build-metadata:browser:{name}", "kind": "browser-suite",
                "scope": "repository", "affectedUnit": name,
                "repairRoots": [], "coveredPaths": []}
    return None


def validate_browser_argv(argv: object, *, allow_heavy: bool = False, root: Path | None = None) -> list[str]:
    """Validate a dedicated browser/e2e script invocation.

    Bypasses the ordinary npm/pnpm/yarn "test" requirement in
    :func:`validate_argv` deliberately: :func:`_browser_suite_candidate`
    already confirmed the script name against a curated allowlist and that the
    script's own first token is a known browser runner, so re-deriving that
    here would only repeat the same check under a different name.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        raise VerificationBlocked("browser suite command must be a non-empty argv array")
    values = [str(value) for value in argv]
    if any(not value or value in _SHELL_TOKENS or any(mark in value for mark in ("|", ";", ">", "<", "`", "$("))
           for value in values):
        raise VerificationBlocked("shell syntax is not allowed in browser suite commands")
    if values[0] not in {"npm", "pnpm", "yarn"}:
        raise VerificationBlocked(f"unsupported browser suite entrypoint: {values[0]}")
    return values


def _python_test_files(root: Path) -> list[Path]:
    """Every Python test-shaped file in the repo, dotdirs excluded.

    Independent evidence that pytest applies here: unlike a ``Gemfile`` or
    ``package.json``, ``pyproject.toml`` is optional for pytest to run, so a
    bare stdlib module plus a ``test_*.py`` file is a fully valid, idiomatic
    Python project with zero packaging metadata.  Requiring that metadata
    before even considering pytest made a freshly bootstrapped repository
    unverifiable purely because nothing had yet given it packaging metadata --
    confirmed live: a real, fully-passing delivery was reported unverifiable
    for exactly this reason.
    """
    return [path for path in root.rglob("*.py") if _TEST_FILE.search(path.relative_to(root).as_posix())
           and not any(part.startswith(".") for part in path.relative_to(root).parts)]


def _inferred_candidates(root: Path, rejected: list[dict] | None = None, *, allow_heavy: bool = False) -> list[dict]:
    """Fallback only when CI does not expose a simple safe command.

    Every manifest location for every recognized build system contributes its
    own candidate, scoped to that location by baking its relative directory
    into the argv (each tool's own way: ``-f``/``--manifest-path``/``--prefix``/
    a module-relative package path), never by changing the shared execution
    cwd. A repository that accumulates independent nested projects -- this
    harness's own test repository does exactly that, one self-contained
    directory per scenario, by design, to avoid cross-run collisions -- must
    run every one of them; a manifest at the repository root is just the
    special case where that relative directory is ``.``. This used to be an
    elif chain that both ignored anything not sitting at the repository root
    and returned only the first build system it matched; ``pytest`` already
    escaped both limits via ``_python_test_files`` (root.rglob), and a real,
    fully-passing delivery had already hit exactly this gap for Python before
    that fix landed -- this generalizes the same fix to every build system.
    """
    def scoped(pattern: str) -> list[tuple[Path, str]]:
        """(manifest path, POSIX directory relative to root, "." at the root)."""
        return [(path, _relative(root, path.parent) or ".") for path in _scan(root, pattern)]

    choices: list[tuple[list[str], str]] = []

    # `validate_argv`'s entrypoint allowlist only recognizes the literal wrapper
    # paths "./gradlew"/"./mvnw" (a root-relative script, not a PATH binary), so
    # a nested wrapper cannot be expressed as a candidate yet -- fall through to
    # the plain gradle/mvn form for a nested project, same as one with no
    # wrapper at all.
    wrapper_dirs = {directory for _, directory in scoped("gradlew") if directory == "."}
    if wrapper_dirs:
        choices.append((["./gradlew", "test"], "build-system:gradle-wrapper:."))
    # A module a root settings file `include`s is a SUBPROJECT of one build,
    # not an independent nested project: the root build's own `test` task
    # already schedules `:<module>:test` for every one of them (verified
    # against a 25-module build). Emitting a second standalone command per
    # module re-runs exactly the same tests, and because execution stops at
    # the first command that cannot start, each redundant command is another
    # chance to convert a passing run into a blocked one -- a bare `gradle`
    # is not installed on every host that has the repository's own wrapper.
    # Only a genuinely independent nested project (not part of the root
    # build) still earns its own command, which is the accumulating
    # multi-project test repository this fan-out exists for.
    subproject_dirs = set(_gradle_project_directories(root))
    gradle_dirs = sorted({directory for name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
                          for _, directory in scoped(name)} - wrapper_dirs - subproject_dirs)
    for directory in gradle_dirs:
        choices.append((["gradle", "-p", directory, "test"], f"build-system:gradle:{directory}"))

    mvnw_dirs = {directory for _, directory in scoped("mvnw") if directory == "."}
    if mvnw_dirs:
        choices.append((["./mvnw", "test"], "build-system:maven-wrapper:."))
    for manifest, directory in scoped("pom.xml"):
        if directory in mvnw_dirs:
            continue
        choices.append((["mvn", "-f", _relative(root, manifest), "test"], f"build-system:maven:{directory}"))

    for manifest, directory in scoped("package.json"):
        # `--if-present` must be npm's own flag (no `--` separator before it) so
        # npm interprets it itself; putting it after `--` forwards it to the
        # underlying script instead, e.g. producing `vitest run --if-present`
        # (confirmed live: `node: bad option: --if-present` for an npm script
        # that received it this way).
        choices.append((["npm", "--prefix", directory, "test", "--if-present"], f"build-system:npm:{directory}"))

    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() or _python_test_files(root):
        choices.append((["pytest"], "build-system:pytest"))

    for manifest, directory in scoped("go.mod"):
        # Unlike cargo/dotnet/maven, `go` has no "manifest path" flag -- the
        # module has to be the working directory (or named via the `-C`
        # global flag, Go 1.20+) for `./...` to resolve within it at all.
        argv = ["go", "test", "./..."] if directory == "." else ["go", "-C", directory, "test", "./..."]
        choices.append((argv, f"build-system:go:{directory}"))

    for manifest, directory in scoped("Cargo.toml"):
        choices.append((["cargo", "test", "--manifest-path", _relative(root, manifest)],
                        f"build-system:cargo:{directory}"))

    for manifest, directory in scoped("CMakeLists.txt"):
        build_dir = f"{directory}/.conductor-verify-build" if directory != "." else ".conductor-verify-build"
        choices.append((["cmake", "-S", directory, "-B", build_dir], f"build-system:cmake:configure:{directory}"))
        choices.append((["cmake", "--build", build_dir], f"build-system:cmake:build:{directory}"))
        choices.append((["ctest", "--test-dir", build_dir, "--output-on-failure"], f"build-system:ctest:{directory}"))

    for manifest, directory in [*scoped("*.csproj"), *scoped("*.fsproj")]:
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        if "Microsoft.NET.Test.Sdk" not in text:
            continue
        choices.append((["dotnet", "test", _relative(root, manifest)], f"build-system:dotnet:{directory}"))

    candidates: list[dict] = []
    for argv, source in choices:
        # One unrepresentable inferred candidate must not abort the whole
        # inference -- the same rule _changed_scope_candidates already
        # applies to its planners. These argvs are synthesized here from
        # build files rather than chosen by a caller, so a single nested
        # project this module cannot yet express (or one that trips the
        # heavy-marker filter) has to be dropped, leaving the rest --
        # including the repository-root wrapper command -- to carry the run.
        # Raising instead let one nested module leave a fully testable repository untested.
        try:
            validated = validate_argv(argv, allow_heavy=allow_heavy)
        except VerificationBlocked as exc:
            if rejected is not None:
                rejected.append({"argv": [str(value) for value in argv],
                                 "source": source, "reason": str(exc)})
            continue
        candidates.append({"argv": validated, "source": source, "kind": "inferred"})
    return candidates


def _changed_paths(root: Path, candidate_commit: str | None) -> list[str]:
    """Return paths changed by the exact candidate, without inspecting CI files.

    A candidate commit is the only trustworthy scope for targeted checks.  The
    current checkout may have moved while an agent was working, so never derive
    this from its working tree or HEAD.
    """
    commit = str(candidate_commit or "").strip()
    if not commit:
        return []
    git_root = _git_root(root)
    resolved = git.git(str(git_root), "rev-parse", "--verify", f"{commit}^{{commit}}", check=False)
    if resolved.code != 0:
        raise VerificationBlocked(f"candidate commit does not exist: {commit}")
    # `code_parallel` produces a merge commit.  Plain `diff-tree` intentionally
    # emits no file list for merges, which silently selected a broad root test
    # instead of the changed-module test.  Compare with the first parent: that
    # is the branch state immediately before the candidate's merged work.
    diff = git.git(str(git_root), "diff-tree", "--root", "--no-commit-id", "--name-only", "-r",
                   "-m", "--first-parent", resolved.stdout.strip(), check=False)
    if diff.code != 0:
        raise VerificationBlocked("unable to read candidate changed paths")
    prefix = ""
    if root.resolve() != git_root:
        prefix = root.resolve().relative_to(git_root).as_posix().rstrip("/") + "/"
    paths = [path.strip() for path in diff.stdout.splitlines() if path.strip()]
    if not prefix:
        return paths
    return [path[len(prefix):] for path in paths if path.startswith(prefix)]


def _resolve_gradle_name(text: str, root_name: str | None) -> str | None:
    """Substitute ${rootProject.name} in a settings-file string literal.

    Returns None when any interpolation survives, so the caller drops the
    mapping instead of emitting a Gradle task path containing a literal
    "${...}" that Gradle cannot resolve.
    """
    if root_name:
        for form in ("${rootProject.name}", "$rootProject.name"):
            text = text.replace(form, root_name)
    return None if "$" in text else text


def _gradle_project_directories(root: Path) -> dict[str, str]:
    """Map project directories to Gradle project paths from settings files."""
    mapping: dict[str, str] = {}
    includes: set[str] = set()
    root_child_rename: tuple[str, str] | None = None
    root_name: str | None = None
    for name in ("settings.gradle", "settings.gradle.kts"):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _GRADLE_PROJECT_DIR.finditer(text):
            mapping[match.group("directory").strip("/")] = ":" + match.group("name")
        rename = _GRADLE_ROOT_CHILD_RENAME.search(text)
        if rename:
            root_child_rename = (rename.group("prefix"), rename.group("suffix"))
        declared = _GRADLE_ROOT_NAME.search(text)
        if declared:
            root_name = declared.group("value").strip()
        for line in text.splitlines():
            if not _GRADLE_INCLUDE_LINE.search(line) or "includeBuild" in line:
                continue
            for match in _QUOTED_VALUE.finditer(line):
                name = match.group("value").strip().lstrip(":")
                if name:
                    includes.add(name)
    for name in includes:
        directory = name.replace(":", "/")
        if any((root / directory / build).is_file() for build in ("build.gradle", "build.gradle.kts")):
            project_parts = name.split(":")
            if root_child_rename:
                # A rename prefix/suffix may itself interpolate
                # ${rootProject.name} ("${rootProject.name}-${it.name}" is the
                # common multi-module idiom). Substituting it is what makes the
                # resulting project path real; leaving it literal produced task
                # paths like ":${rootProject.name}-common:test" that Gradle
                # rejects. If any interpolation is still unresolved, drop the
                # mapping rather than invent a project name.
                resolved = [_resolve_gradle_name(part, root_name) for part in root_child_rename]
                if any(part is None for part in resolved):
                    continue
                project_parts[0] = resolved[0] + project_parts[0] + resolved[1]
            mapping.setdefault(directory, ":" + ":".join(project_parts))
    return mapping


def _targeted_gradle_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    """Select module test tasks for changed Gradle project directories.

    This intentionally favours a small, source-evidenced test target over a
    root ``build``.  If a change cannot be mapped safely, discovery retains its
    normal documented/inferred fallback instead of inventing a project name.
    """
    if not (root / "gradlew").is_file() or not changed_paths:
        return []
    projects = _gradle_project_directories(root)
    selected: list[tuple[str, str]] = []
    exact_tests: dict[tuple[str, str], list[str]] = {}
    for changed in changed_paths:
        for directory, project in projects.items():
            if changed == directory or changed.startswith(directory + "/"):
                item = (directory, project)
                if item not in selected:
                    selected.append(item)
                match = _JVM_TEST_PATH.match(changed)
                if match and (match.group("module") or "").rstrip("/") == directory:
                    exact_tests.setdefault(item, []).append(match.group("class").replace("/", "."))
    candidates: list[dict] = []
    for directory, project in selected:
        tests = sorted(set(exact_tests.get((directory, project), [])))
        argv = ["./gradlew", f"{project}:test"]
        scope = "affected"
        if tests:
            scope = "focused"
            for test in tests:
                argv.extend(["--tests", test])
        candidates.append({"argv": argv, "source": f"build-metadata:gradle:{directory}",
                           "kind": "changed-scope", "scope": scope,
                           "affectedUnit": project, "repairRoots": [directory],
                           "coveredPaths": [path for path in changed_paths
                           if path == directory or path.startswith(directory + "/")]})
    root_tests = [match for path in changed_paths if (match := _JVM_TEST_PATH.match(path)) and not match.group("module")]
    if root_tests:
        argv = ["./gradlew", "test"]
        for match in root_tests:
            argv.extend(["--tests", match.group("class").replace("/", ".")])
        candidates.append({"argv": argv, "source": "build-metadata:gradle:root-exact-tests",
                           "kind": "changed-scope", "scope": "focused", "affectedUnit": ":",
                           "repairRoots": [],
                           "coveredPaths": [path for path in changed_paths
                                            if (match := _JVM_TEST_PATH.match(path)) and not match.group("module")]})
    return candidates


def _maven_modules(root: Path) -> dict[str, str]:
    """Return Maven module directories from ordinary pom.xml metadata."""
    modules: dict[str, str] = {}
    pending = [root / "pom.xml"]
    seen: set[Path] = set()
    while pending:
        pom = pending.pop()
        if pom in seen or not pom.is_file():
            continue
        seen.add(pom)
        try:
            document = ET.parse(pom).getroot()
        except (OSError, ET.ParseError):
            continue
        for element in document.iter():
            if element.tag.rsplit("}", 1)[-1] != "module" or not (element.text or "").strip():
                continue
            directory = (pom.parent / element.text.strip()).resolve()
            try:
                relative = directory.relative_to(root).as_posix()
            except ValueError:
                continue
            if relative and relative != ".":
                modules[relative] = relative
                pending.append(directory / "pom.xml")
    return modules


def _targeted_maven_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    if not (root / "pom.xml").is_file():
        return []
    executable = "./mvnw" if (root / "mvnw").is_file() else "mvn"
    modules = _maven_modules(root)
    selected: dict[str, list[str]] = {}
    for changed in changed_paths:
        matches = [directory for directory in modules if changed == directory or changed.startswith(directory + "/")]
        if matches:
            selected.setdefault(max(matches, key=len), []).append(changed)
    candidates: list[dict] = []
    for directory, covered in sorted(selected.items()):
        argv = [executable, "-pl", directory, "-am"]
        tests: list[str] = []
        for changed in covered:
            match = _JVM_TEST_PATH.match(changed)
            if match:
                tests.append(Path(match.group("class")).name)
        scope = "focused" if tests else "affected"
        if tests:
            argv.append("-Dtest=" + ",".join(sorted(set(tests))))
        argv.append("test")
        candidates.append({"argv": argv, "source": f"build-metadata:maven:{directory}",
                           "kind": "changed-scope", "scope": scope,
                           "affectedUnit": directory, "repairRoots": [directory],
                           "coveredPaths": covered})
    root_tests = [(path, match) for path in changed_paths
                  if (match := _JVM_TEST_PATH.match(path)) and not match.group("module")]
    if root_tests:
        candidates.append({"argv": [executable, "-Dtest=" + ",".join(sorted({Path(match.group('class')).name
                                                                              for _, match in root_tests})), "test"],
                           "source": "build-metadata:maven:root-exact-tests", "kind": "changed-scope",
                           "scope": "focused", "affectedUnit": ".",
                           "repairRoots": [],
                           "coveredPaths": [path for path, _ in root_tests]})
    return candidates


def _workspace_patterns(root: Path, package: dict) -> list[str]:
    workspaces = package.get("workspaces", [])
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages", [])
    patterns = [str(value) for value in workspaces] if isinstance(workspaces, list) else []
    pnpm = root / "pnpm-workspace.yaml"
    if pnpm.is_file():
        try:
            for line in pnpm.read_text(encoding="utf-8").splitlines():
                match = re.match(r"\s*-\s*['\"]?(?P<pattern>[^'\"#]+)", line)
                if match:
                    patterns.append(match.group("pattern").strip())
        except OSError:
            pass
    return patterns


def _targeted_javascript_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    package_path = root / "package.json"
    if not package_path.is_file():
        return []
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    directories: dict[str, str] = {}
    for pattern in _workspace_patterns(root, package):
        for path in root.glob(pattern):
            child = path / "package.json"
            if not child.is_file():
                continue
            try:
                metadata = json.loads(child.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if "test" not in metadata.get("scripts", {}):
                continue
            directory = path.relative_to(root).as_posix()
            directories[directory] = str(metadata.get("name") or directory)
    selected: dict[str, list[str]] = {}
    for changed in changed_paths:
        matches = [directory for directory in directories if changed == directory or changed.startswith(directory + "/")]
        if matches:
            selected.setdefault(max(matches, key=len), []).append(changed)
    manager = "pnpm" if (root / "pnpm-lock.yaml").is_file() or (root / "pnpm-workspace.yaml").is_file() else \
        "yarn" if (root / "yarn.lock").is_file() else "npm"
    if not directories and "test" in (package.get("scripts") or {}):
        # A single-package repository is not a workspace, and used to yield no
        # candidate at all -- the whole ecosystem silently had no targeted plan.
        directories["."] = str(package.get("name") or ".")
        selected = {".": [path for path in changed_paths
                          if Path(path).suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}]}
        selected = {key: value for key, value in selected.items() if value}
    candidates: list[dict] = []
    for directory, covered in sorted(selected.items()):
        name = directories[directory]
        specs = _js_spec_files(root, directory, covered)
        runner_filter = specs if specs and _js_runner_accepts_paths(root, directory) else []
        if directory == ".":
            argv = [manager, "test"]
        else:
            argv = (["pnpm", "--filter", name, "test"] if manager == "pnpm" else
                    ["yarn", "workspace", name, "test"] if manager == "yarn" else
                    ["npm", "--workspace", name, "test"])
        if runner_filter:
            # yarn classic forwards positionals directly; npm and pnpm need the
            # -- separator to reach the runner rather than the package manager.
            argv = argv + ([*runner_filter] if manager == "yarn"
                           else ["--", *runner_filter])
        candidates.append({"argv": argv, "source": f"build-metadata:{manager}:{directory}",
                           "kind": "changed-scope",
                           "scope": "focused" if runner_filter else "affected",
                           "affectedUnit": name,
                           "repairRoots": [] if directory == "." else [directory],
                           "coveredPaths": covered})
    return candidates


_JS_SPEC_SUFFIXES = (".test.", ".spec.")


def _js_runner_accepts_paths(root: Path, directory: str) -> bool:
    """Only pass file filters to a runner that takes them as positional test files (a browser runner
    takes projects, not files). Vitest runs once, not in watch mode, because every verification
    environment sets ``CI=1`` (check_execution.inherited_environment)."""
    manifest = (root / directory / "package.json") if directory != "." else (root / "package.json")
    try:
        script = str((json.loads(manifest.read_text(encoding="utf-8")).get("scripts") or {}).get("test", ""))
    except (OSError, json.JSONDecodeError):
        return False
    tokens = script.split()
    runner = next((token for token in tokens if Path(token).name in _JS_RUNNERS), "")
    if not runner:
        return False
    return Path(runner).name not in _BROWSER_RUNNERS


def _js_spec_files(root: Path, directory: str, covered: list[str]) -> list[str]:
    base = root if directory == "." else root / directory
    existing = {path.relative_to(root).as_posix() for path in _scan(base, "*")
                if path.is_file() and any(mark in path.name for mark in _JS_SPEC_SUFFIXES)}
    specs: set[str] = set()
    for changed in covered:
        if any(mark in Path(changed).name for mark in _JS_SPEC_SUFFIXES):
            specs.add(changed)
            continue
        stem = Path(changed).stem
        specs.update(path for path in existing
                     if Path(path).name.split(".")[0] == stem)
    return sorted(specs)


def _targeted_python_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    all_tests = _python_test_files(root)
    has_metadata = any((root / name).is_file() for name in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"))
    if not has_metadata and not all_tests:
        return []
    targets: set[str] = {path for path in changed_paths if _TEST_FILE.search(path)}
    source_paths = [path for path in changed_paths if Path(path).suffix == ".py" and path not in targets]
    covered: set[str] = set(targets)
    for source in source_paths:
        stem = Path(source).stem
        matches = [path.relative_to(root).as_posix() for path in all_tests
                   if path.name in {f"test_{stem}.py", f"{stem}_test.py", f"{stem}_tests.py"}]
        if matches:
            targets.update(matches)
            covered.add(source)
    if not targets:
        return []
    # Scope the repair agent to the directories the changed sources AND the
    # test files that will actually run both live in -- `covered` alone omits
    # the discovered test paths (it documents which *changed* paths this
    # candidate exercises, not everywhere the command touches), and a repair
    # is just as often to the test itself as to the source. This was
    # unconditionally `[]` (meaning "unrestricted, whole worktree" per
    # _worktree_guard) regardless of nesting -- fine for a single flat repo,
    # wrong once a repository accumulates independent nested projects, same as
    # the go/dotnet/cmake planners already handle. "." (repo root) is dropped
    # the same way those planners drop it -- a flat, non-nested project keeps
    # the original unrestricted behavior.
    repair_roots = sorted({str(Path(path).parent) for path in targets | covered} - {"."})
    return [{"argv": ["pytest", *sorted(targets)], "source": "build-metadata:pytest:changed-files",
             "kind": "changed-scope", "scope": "focused", "affectedUnit": "pytest-files",
             "repairRoots": repair_roots,
             "coveredPaths": sorted(covered)}]


def _targeted_ruby_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    """Map changed Ruby sources to their rspec specs or minitest files.

    ``check_execution`` already probes ruby/bundle/rake and already isolates
    GEM_SPEC_CACHE and BUNDLE_*, and AGENTS.md already promises Ruby support --
    the planner was the only missing piece, so this emits nothing new to the
    runtime.  Everything goes through ``bundle exec``: bare ``ruby -Itest`` is a
    generic interpreter and must stay refused.
    """
    if not (root / "Gemfile").is_file():
        return []
    rspec = (root / ".rspec").is_file() or (root / "spec").is_dir()
    minitest = (root / "Rakefile").is_file() and (root / "test").is_dir()
    if not rspec and not minitest:
        return []

    suffix = "_spec.rb" if rspec else "_test.rb"
    directory = "spec" if rspec else "test"
    existing = [path for path in (root / directory).rglob(f"*{suffix}")
                if not any(part in _SCAN_SKIP_PARTS or part.startswith(".")
                           for part in path.relative_to(root).parts)]

    targets = {path for path in changed_paths if path.endswith(suffix)}
    covered: set[str] = set(targets)
    for changed in changed_paths:
        if Path(changed).suffix != ".rb" or changed in targets:
            continue
        stem = Path(changed).stem
        matches = [path.relative_to(root).as_posix() for path in existing
                   if path.name == f"{stem}{suffix}"]
        if matches:
            targets.update(matches)
            covered.add(changed)
    if not targets:
        return []

    if rspec:
        argv = ["bundle", "exec", "rspec", *sorted(targets)]
        unit = "rspec-files"
    else:
        # Rake takes one file per invocation via TEST=, so a multi-file change
        # is expressed as the directory-scoped task rather than a fake list.
        only = sorted(targets)
        argv = (["bundle", "exec", "rake", "test", f"TEST={only[0]}"] if len(only) == 1
                else ["bundle", "exec", "rake", "test"])
        unit = "minitest"
    return [{"argv": argv, "source": f"build-metadata:ruby:{unit}", "kind": "changed-scope",
             "scope": "focused" if (rspec or len(targets) == 1) else "affected",
             "affectedUnit": unit, "repairRoots": [], "coveredPaths": sorted(covered)}]


def _scan(root: Path, pattern: str) -> list[Path]:
    """Find manifests without descending into vendored or generated trees."""
    return [path for path in root.rglob(pattern)
            if not any(part in _SCAN_SKIP_PARTS or part.startswith(".")
                       for part in path.relative_to(root).parts)]


def _nearest_unit(changed: str, directories: dict[str, str]) -> str | None:
    matches = [d for d in directories if changed == d or changed.startswith(d + "/") or d == "."]
    return max(matches, key=len) if matches else None


def _targeted_dotnet_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    projects = _scan(root, "*.csproj") + _scan(root, "*.fsproj")
    if not projects:
        return []
    tests: dict[str, str] = {}
    for project in projects:
        try:
            text = project.read_text(encoding="utf-8")
        except OSError:
            continue
        if "Microsoft.NET.Test.Sdk" not in text:
            continue
        tests[project.parent.relative_to(root).as_posix()] = project.relative_to(root).as_posix()
    if not tests:
        return []
    selected: dict[str, list[str]] = {}
    for changed in changed_paths:
        unit = _nearest_unit(changed, tests)
        # A changed file outside any test project still needs the suite that
        # covers it; the nearest test project is the only evidence available
        # without building the reference graph.
        target = unit or min(tests, key=len)
        selected.setdefault(target, []).append(changed)
    return [{"argv": ["dotnet", "test", tests[directory]],
             "source": f"build-metadata:dotnet:{directory}", "kind": "changed-scope",
             "scope": "affected", "affectedUnit": directory,
             "repairRoots": [] if directory == "." else [directory],
             "coveredPaths": sorted(covered)}
            for directory, covered in sorted(selected.items())]


def _targeted_php_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    if not (root / "composer.json").is_file():
        return []
    if not any((root / name).is_file() for name in ("phpunit.xml", "phpunit.xml.dist")):
        return []
    try:
        composer = json.loads((root / "composer.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if "test" not in (composer.get("scripts") or {}):
        return []
    # vendor/bin/phpunit is deliberately not used: vendor/ is gitignored, so it
    # is absent from the disposable clone, and a repo-relative binary is
    # candidate-controlled.
    existing = {path.name: path.relative_to(root).as_posix() for path in _scan(root, "*Test.php")}
    classes: list[str] = []
    covered: list[str] = []
    for changed in changed_paths:
        if not changed.endswith(".php"):
            continue
        name = Path(changed).name
        if name.endswith("Test.php"):
            classes.append(Path(name).stem)
            covered.append(changed)
        elif f"{Path(name).stem}Test.php" in existing:
            classes.append(f"{Path(name).stem}Test")
            covered.append(changed)
    if not classes:
        return []
    return [{"argv": ["composer", "test", "--", "--filter", "|".join(sorted(set(classes)))],
             "source": "build-metadata:php:phpunit", "kind": "changed-scope",
             "scope": "focused", "affectedUnit": "phpunit", "repairRoots": [],
             "coveredPaths": sorted(covered)}]


def _targeted_elixir_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    if not (root / "mix.exs").is_file():
        return []
    existing = {path.relative_to(root).as_posix() for path in _scan(root, "*_test.exs")}
    targets: set[str] = set()
    covered: set[str] = set()
    for changed in changed_paths:
        if changed in existing:
            targets.add(changed)
            covered.add(changed)
            continue
        if not changed.endswith(".ex"):
            continue
        # lib/foo.ex -> test/foo_test.exs, including the umbrella layout.
        guess = changed.replace("/lib/", "/test/", 1) if "/lib/" in changed \
            else changed.replace("lib/", "test/", 1)
        guess = guess[:-3] + "_test.exs"
        if guess in existing:
            targets.add(guess)
            covered.add(changed)
    if not targets:
        return []
    return [{"argv": ["mix", "test", *sorted(targets)],
             "source": "build-metadata:elixir:mix", "kind": "changed-scope",
             "scope": "focused", "affectedUnit": "mix-test", "repairRoots": [],
             "coveredPaths": sorted(covered)}]


_SBT_PROJECT = re.compile(
    r"lazy\s+val\s+(?P<name>\w+)\s*=\s*\(?project\s*in\s*file\(\s*['\"](?P<directory>[^'\"]+)['\"]")


def _targeted_sbt_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    build = root / "build.sbt"
    if not build.is_file():
        return []
    try:
        text = build.read_text(encoding="utf-8")
    except OSError:
        return []
    projects = {match.group("directory").strip("./"): match.group("name")
                for match in _SBT_PROJECT.finditer(text)}
    if not projects:
        return []
    selected: dict[str, list[str]] = {}
    for changed in changed_paths:
        unit = _nearest_unit(changed, projects)
        if unit:
            selected.setdefault(unit, []).append(changed)
    # One argv element containing a space is legal here: sbt parses its own
    # command line, and no shell is involved.
    return [{"argv": ["sbt", f"{projects[directory]}/test"],
             "source": f"build-metadata:sbt:{directory}", "kind": "changed-scope",
             "scope": "affected", "affectedUnit": projects[directory],
             "repairRoots": [directory], "coveredPaths": sorted(covered)}
            for directory, covered in sorted(selected.items())]


_BAZEL_TEST = re.compile(
    r"\w*_test\(\s*name\s*=\s*['\"](?P<name>[^'\"]+)['\"](?P<body>.*?)\)", re.S)


def _targeted_bazel_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    if not any((root / name).is_file()
               for name in ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel")):
        return []
    targets: dict[str, list[str]] = {}
    for build_file in _scan(root, "BUILD") + _scan(root, "BUILD.bazel"):
        package = build_file.parent.relative_to(root).as_posix()
        package = "" if package == "." else package
        try:
            text = build_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _BAZEL_TEST.finditer(text):
            sources = set(re.findall(r"['\"]([^'\"]+\.[A-Za-z0-9]+)['\"]", match.group("body")))
            label = f"//{package}:{match.group('name')}"
            for changed in changed_paths:
                relative = changed[len(package) + 1:] if package and changed.startswith(package + "/") \
                    else (changed if not package else None)
                if relative in sources:
                    targets.setdefault(label, []).append(changed)
    return [{"argv": ["bazel", "test", label], "source": f"build-metadata:bazel:{label}",
             "kind": "changed-scope", "scope": "focused", "affectedUnit": label,
             "repairRoots": [], "coveredPaths": sorted(covered)}
            for label, covered in sorted(targets.items())]


_MAKE_TARGET = re.compile(r"^(?P<name>[A-Za-z0-9_.\-/]+)\s*:(?!=)", re.M)
_TEST_TARGET = re.compile(r"(^|[-_./])(test|check|spec)")
# `make test` runs everything, so it proves nothing about the changed files and
# is exactly the repository-wide fallback targeted mode refuses.
_GENERIC_MAKE_TARGETS = {"test", "check", "spec", "tests", "checks"}


def _targeted_make_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    """Bind changed paths to a Makefile target only on literal recipe evidence.

    This is the weakest evidence of any planner, so the bar is deliberately
    high: the target must be test-shaped *and* its recipe must literally name a
    directory that contains the changed file. Scope is never "focused" -- a make
    target is an opaque script, not an exact test.
    """
    makefile = next((root / name for name in ("Makefile", "GNUmakefile")
                     if (root / name).is_file()), None)
    if makefile is None:
        return []
    try:
        text = makefile.read_text(encoding="utf-8")
    except OSError:
        return []
    blocks: dict[str, str] = {}
    matches = list(_MAKE_TARGET.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        name = match.group("name")
        if name.startswith(".") or not _TEST_TARGET.search(name) \
                or name in _GENERIC_MAKE_TARGETS:
            continue
        blocks[name] = text[match.end():end]
    selected: dict[str, list[str]] = {}
    for name, recipe in blocks.items():
        for changed in changed_paths:
            directory = str(Path(changed).parent)
            if directory != "." and directory in recipe:
                selected.setdefault(name, []).append(changed)
    return [{"argv": ["make", name], "source": f"build-metadata:make:{name}",
             "kind": "changed-scope", "scope": "affected", "affectedUnit": name,
             "repairRoots": [], "coveredPaths": sorted(covered)}
            for name, covered in sorted(selected.items())]


def _targeted_go_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    module_dirs = sorted(({_relative(root, path.parent) or "." for path in _scan(root, "go.mod")}),
                         key=len, reverse=True)
    if not module_dirs:
        return []
    # Group by (module, package-within-module): the module a changed file
    # belongs to decides whether `go` needs `-C <module>` at all, and the
    # package pattern is always relative to that module's own root, not the
    # repository root.
    by_unit: dict[tuple[str, str], list[str]] = {}
    for changed in changed_paths:
        if not changed.endswith(".go"):
            continue
        module = next((directory for directory in module_dirs
                       if directory == "." or changed == directory or changed.startswith(directory + "/")), None)
        if module is None:
            continue
        within = changed[len(module) + 1:] if module != "." else changed
        package_dir = Path(within).parent.as_posix()
        by_unit.setdefault((module, package_dir), []).append(changed)

    candidates = []
    for (module, package_dir), covered in sorted(by_unit.items()):
        pattern = "." if package_dir == "." else f"./{package_dir}"
        argv = ["go", "test", pattern] if module == "." else ["go", "-C", module, "test", pattern]
        if module == ".":
            unit = package_dir
        elif package_dir == ".":
            unit = module
        else:
            unit = f"{module}/{package_dir}"
        candidates.append({"argv": argv, "source": f"build-metadata:go:{unit}",
                           "kind": "changed-scope", "scope": "affected", "affectedUnit": unit,
                           "repairRoots": [] if unit == "." else [unit],
                           "coveredPaths": covered})
    return candidates


def _cargo_packages(root: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for manifest in root.rglob("Cargo.toml"):
        relative = manifest.relative_to(root)
        if any(part in _SCAN_SKIP_PARTS or part.startswith(".") for part in relative.parts):
            continue
        try:
            package = tomllib.loads(manifest.read_text(encoding="utf-8")).get("package", {})
        except (OSError, tomllib.TOMLDecodeError):
            continue
        name = package.get("name")
        if name:
            packages[manifest.parent.relative_to(root).as_posix()] = str(name)
    return packages


def _targeted_cargo_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    if not (root / "Cargo.toml").is_file():
        return []
    packages = _cargo_packages(root)
    selected: dict[str, list[str]] = {}
    for changed in changed_paths:
        if not changed.endswith(".rs"):
            continue
        matches = [directory for directory in packages if directory == "." or changed.startswith(directory + "/")]
        if matches:
            selected.setdefault(max(matches, key=len), []).append(changed)
    return [{"argv": ["cargo", "test", "-p", packages[directory]],
             "source": f"build-metadata:cargo:{directory}", "kind": "changed-scope",
             "scope": "affected", "affectedUnit": packages[directory],
             "repairRoots": [] if directory == "." else [directory], "coveredPaths": covered}
            for directory, covered in sorted(selected.items())]


def _cmake_calls(root: Path) -> tuple[dict[str, set[str]], dict[str, str], dict[str, set[str]]]:
    """Read literal CMake target/source/test relationships without executing CMake."""
    sources: dict[str, set[str]] = {}
    tests: dict[str, str] = {}
    links: dict[str, set[str]] = {}
    for definition in root.rglob("CMakeLists.txt"):
        relative = definition.relative_to(root)
        if any(part in _SCAN_SKIP_PARTS or part.startswith(".") for part in relative.parts):
            continue
        try:
            text = re.sub(r"#.*", "", definition.read_text(encoding="utf-8"))
        except OSError:
            continue
        base = definition.parent.relative_to(root)
        for match in re.finditer(r"\badd_(?:executable|library)\s*\((?P<body>.*?)\)", text, re.I | re.S):
            tokens = re.findall(r"[^\s;]+", match.group("body"))
            if not tokens:
                continue
            target = tokens[0].strip('"')
            for token in tokens[1:]:
                value = token.strip('"')
                if not value or value.startswith("$") or value.upper() in {"STATIC", "SHARED", "MODULE", "OBJECT", "EXCLUDE_FROM_ALL"}:
                    continue
                path = (base / value).as_posix()
                sources.setdefault(target, set()).add(path)
        for match in re.finditer(r"\btarget_link_libraries\s*\((?P<body>.*?)\)", text, re.I | re.S):
            tokens = [token.strip('"') for token in re.findall(r"[^\s;]+", match.group("body"))]
            if tokens:
                links.setdefault(tokens[0], set()).update(token for token in tokens[1:]
                                                        if token.upper() not in {"PRIVATE", "PUBLIC", "INTERFACE"})
        for match in re.finditer(r"\badd_test\s*\((?P<body>.*?)\)", text, re.I | re.S):
            tokens = [token.strip('"') for token in re.findall(r"[^\s;]+", match.group("body"))]
            if not tokens:
                continue
            if tokens[0].upper() == "NAME" and len(tokens) >= 4 and tokens[2].upper() == "COMMAND":
                tests[tokens[1]] = tokens[3]
            elif len(tokens) >= 2:
                tests[tokens[0]] = tokens[1]
    return sources, tests, links


def _cmake_project_dir(cmake_dirs: list[str], paths: set[str]) -> str:
    """Nearest ``cmake_dirs`` entry that is an ancestor of every given path.

    ``_cmake_calls`` already prefixes a nested ``CMakeLists.txt``'s own targets
    with its directory, so this just picks out which project that directory
    belongs to; ``cmake_dirs`` is pre-sorted deepest-first so the first (and
    only, for a well-formed target) match wins.
    """
    for directory in cmake_dirs:
        prefix = "" if directory == "." else directory + "/"
        if all(path == directory or path.startswith(prefix) for path in paths):
            return directory
    return "."


def _targeted_cmake_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    cmake_dirs = sorted(({_relative(root, path.parent) or "." for path in _scan(root, "CMakeLists.txt")}),
                        key=len, reverse=True)
    if not cmake_dirs:
        return []
    sources, tests, links = _cmake_calls(root)
    test_targets: dict[str, str] = {target: name for name, target in tests.items()}
    selected: dict[str, list[str]] = {}
    for changed in changed_paths:
        direct = {target for target, paths in sources.items() if changed in paths}
        executable_tests = {target for target in direct if target in test_targets}
        for library in direct:
            executable_tests.update(target for target, dependencies in links.items()
                                    if library in dependencies and target in test_targets)
        for target in executable_tests:
            selected.setdefault(target, []).append(changed)
    if not selected:
        return []
    # Group by which nested (or root) CMake project each selected target's own
    # sources belong to, so a repository with more than one independent CMake
    # project only configures/builds the ones the change actually touched --
    # and, critically, so a project that is only nested still gets a real
    # -S/-B pair pointed at its own directory instead of a non-existent root.
    by_project: dict[str, dict[str, list[str]]] = {}
    for target, paths in selected.items():
        project = _cmake_project_dir(cmake_dirs, set(sources.get(target, ())))
        by_project.setdefault(project, {})[target] = paths

    candidates: list[dict] = []
    for project, targets in sorted(by_project.items()):
        build_dir = f"{project}/.conductor-verify-build" if project != "." else ".conductor-verify-build"
        covered = sorted({path for paths in targets.values() for path in paths})
        candidates.append({"argv": ["cmake", "-S", project, "-B", build_dir],
                           "source": f"build-metadata:cmake:configure:{project}", "kind": "changed-scope",
                           "scope": "affected", "affectedUnit": f"cmake-configure:{project}", "repairRoots": [],
                           "coveredPaths": covered})
        for target, paths in sorted(targets.items()):
            candidates.extend([
                {"argv": ["cmake", "--build", build_dir, "--target", target],
                 "source": f"build-metadata:cmake:{target}", "kind": "changed-scope", "scope": "affected",
                 "affectedUnit": target, "repairRoots": [], "coveredPaths": paths},
                {"argv": ["ctest", "--test-dir", build_dir, "-R",
                          f"^{re.escape(test_targets[target])}$", "--output-on-failure"],
                 "source": f"build-metadata:ctest:{test_targets[target]}", "kind": "changed-scope",
                 "scope": "focused", "affectedUnit": test_targets[target], "repairRoots": [],
                 "coveredPaths": paths},
            ])
    return candidates


def _swift_targets(manifest: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return {}, {}
    targets: dict[str, str] = {}
    dependencies: dict[str, set[str]] = {}
    for match in re.finditer(r"\.(?P<kind>testTarget|target)\s*\(\s*name\s*:\s*['\"](?P<name>[^'\"]+)['\"](?P<body>.*?)\)\s*[,)]",
                             text, re.S):
        name = match.group("name")
        targets[name] = match.group("kind")
        dep = re.search(r"dependencies\s*:\s*\[(?P<values>.*?)\]", match.group("body"), re.S)
        dependencies[name] = set(_QUOTED_VALUE.findall(dep.group("values"))) if dep else set()
    return targets, dependencies


def _targeted_swift_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    candidates: list[dict] = []
    manifests = [path for path in root.rglob("Package.swift")
                 if not any(part in _SCAN_SKIP_PARTS or part.startswith(".")
                            for part in path.relative_to(root).parts)]
    for manifest in manifests:
        package_dir = manifest.parent.relative_to(root).as_posix()
        prefix = "" if package_dir == "." else package_dir + "/"
        targets, dependencies = _swift_targets(manifest)
        selected: dict[str, list[str]] = {}
        for changed in changed_paths:
            local = changed[len(prefix):] if prefix and changed.startswith(prefix) else changed if not prefix else ""
            match = re.match(r"(?:Sources|Tests)/(?P<target>[^/]+)/", local)
            if not match or match.group("target") not in targets:
                continue
            target = match.group("target")
            test_targets = ({target} if targets[target] == "testTarget" else
                            {name for name, deps in dependencies.items()
                             if targets.get(name) == "testTarget" and target in deps})
            for test_target in test_targets:
                selected.setdefault(test_target, []).append(changed)
        for test_target, covered in sorted(selected.items()):
            argv = ["swift", "test"]
            if package_dir != ".":
                argv.extend(["--package-path", package_dir])
            argv.extend(["--filter", test_target])
            candidates.append({"argv": argv, "source": f"build-metadata:swiftpm:{test_target}",
                               "kind": "changed-scope", "scope": "affected",
                               "affectedUnit": test_target,
                               "repairRoots": [] if package_dir == "." else [package_dir],
                               "coveredPaths": covered})
    return candidates


def _configured_scope_candidates(root: Path, changed_paths: list[str]) -> list[dict]:
    """Load an explicit, language-neutral mapping for non-inferable build graphs."""
    config = root / ".conductor-code" / "verification.json"
    if not config.is_file():
        return []
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationBlocked(f"invalid .conductor-code/verification.json: {exc}") from exc
    if document.get("version") != 1 or not isinstance(document.get("changedScopeRules"), list):
        raise VerificationBlocked("verification.json requires version=1 and changedScopeRules")
    candidates: list[dict] = []
    for rule in document["changedScopeRules"]:
        patterns = rule.get("paths", []) if isinstance(rule, dict) else []
        unit = str(rule.get("affectedUnit") or "") if isinstance(rule, dict) else ""
        token = str(rule.get("scopeToken") or "") if isinstance(rule, dict) else ""
        scope = str(rule.get("scope") or "") if isinstance(rule, dict) else ""
        if not patterns or not unit or not token or scope not in {"focused", "affected"}:
            raise VerificationBlocked(
                "each verification.json rule requires paths, affectedUnit, scopeToken, and scope=focused|affected"
            )
        covered = [path for path in changed_paths if any(fnmatch.fnmatch(path, str(pattern)) for pattern in patterns)]
        if not covered:
            continue
        for argv in rule.get("commands", []):
            validated = validate_configured_argv(argv)
            if token not in validated:
                raise VerificationBlocked(f"configured command does not contain its declared scopeToken: {token}")
            candidates.append({"argv": validated, "adapter": "configured",
                               "source": "repository-config:.conductor-code/verification.json",
                               "kind": "changed-scope", "scope": scope,
                               "affectedUnit": unit,
                               "repairRoots": [str(value) for value in rule.get("repairRoots", [])
                                               if str(value).strip()],
                               "coveredPaths": covered})
    return candidates


def _applicable_guides(root: Path, changed_paths: list[str]) -> list[Path]:
    """Find repository guidance relevant to the candidate's changed paths."""
    found: list[Path] = []

    def add(directory: Path) -> None:
        for name in _GUIDES:
            path = directory / name
            if path.is_file() and path not in found:
                found.append(path)

    add(root)
    for changed in changed_paths:
        parent = (root / changed).parent
        lineage: list[Path] = []
        while parent != root and root in parent.parents:
            lineage.append(parent)
            parent = parent.parent
        for directory in reversed(lineage):
            add(directory)
    return found


def _candidate_jdk_homes(version: str) -> list[str]:
    """Compatibility seam; JDK discovery is owned by check_execution."""
    return check_execution.candidate_jdk_homes(version)


def _java_home(version: object) -> str | None:
    """Resolve a CI-pinned JDK from explicit worker environment or known local installs.

    Verification must use the repository's declared runtime. Falling back to a
    newer host JVM turns an environment mismatch into a false PR failure.
    """
    value = str(version or "").strip()
    for candidate in _candidate_jdk_homes(value):
        if Path(candidate, "bin", "java").is_file():
            return candidate
    if not value:
        return None
    raise VerificationBlocked(
        f"CI requires Java {value}, but no matching JDK is installed; set JAVA{value}_HOME on the verification worker"
    )


def _isolated_environment(
    temp: str,
    java_home: str | None = None,
    dependency_cache_dir: str | Path | None = None,
) -> dict[str, str]:
    """Return the worker's real environment for a candidate-controlled build.

    Inherits the worker's full environment (see
    ``check_execution.isolated_environment``): a repository that needs an
    authenticated package registry or other machine-level configuration
    resolves it the same way a human running the same command by hand would.
    Deployment (Docker env, local shell config, sandbox provisioning) decides
    what is actually present -- this function does not filter it.
    """
    # Kept as a stable internal API for callers/tests; campaign checks use the
    # exact same underlying environment builder.
    return check_execution.isolated_environment(
        temp, java_home=java_home, resolve_default_java=False,
        dependency_cache_dir=dependency_cache_dir
    )


def _with_checkout_importable(env: dict[str, str], argv: list[str], checkout: str) -> dict[str, str]:
    """Bare ``pytest`` puts the test file's own directory on the import path, not the checkout root as
    ``python -m pytest`` does. A correct acceptance test in tests/acceptance/ importing a root module then
    died at collection, a repair agent patched sys.path into the frozen test, the reviewers refused the
    amendment, and the item looped until the budget ran out (run 5e510556)."""
    if not argv or os.path.basename(str(argv[0])) != "pytest":
        return env
    existing = env.get("PYTHONPATH") or ""
    return {**env, "PYTHONPATH": os.pathsep.join(p for p in (checkout, existing) if p)}


def _java_runtime_reason(env: dict[str, str]) -> str | None:
    """Return a diagnostic when the isolated environment cannot start Java."""
    java_home = env.get("JAVA_HOME", "")
    java = str(Path(java_home, "bin", "java")) if java_home else shutil.which("java", path=env.get("PATH"))
    if not java:
        return ("verification worker has no Java runtime on PATH; install/configure the required JDK "
                "before running Gradle or Maven verification")
    probe = check_execution.probe([java, "-version"], cwd=os.getcwd(), env=env)
    if probe.get("available"):
        return None
    detail = str(probe.get("reason") or "unknown Java startup failure").strip().replace("\n", " ")
    return f"verification worker found Java at {java}, but it could not start: {detail[:500]}"


def runtime_capabilities() -> dict[str, dict]:
    """Probe harness build runtimes in the same isolated environment as checks."""
    return check_execution.runtime_capabilities(isolated=True)


def _runtime_block_reason(argv: list[str], env: dict[str, str], cwd: str | None = None) -> str | None:
    """Return an infrastructure reason before executing a runtime-dependent check.

    A missing host runtime is neither a candidate-code failure nor something a
    remediation agent may safely repair.  Surface it as blocked evidence so the
    candidate remains available for handoff without wasting repair attempts.
    """
    directory = cwd or os.getcwd()
    if not check_execution.executable_for(argv, cwd=directory, env=env):
        return f"verification worker cannot start {argv[0]}: executable not found or not executable"
    if argv[0] in _JAVA_ENTRYPOINTS:
        return _java_runtime_reason(env)
    probes = {
        "npm": ["npm", "--version"], "pnpm": ["pnpm", "--version"],
        "yarn": ["yarn", "--version"], "pytest": ["pytest", "--version"],
        "go": ["go", "version"], "cargo": ["cargo", "--version"],
        "make": ["make", "--version"], "cmake": ["cmake", "--version"],
        "ctest": ["ctest", "--version"], "swift": ["swift", "--version"],
    }
    probe_argv = probes.get(argv[0])
    if not probe_argv:
        return None
    if argv[0] in {"npm", "pnpm", "yarn"}:
        node = check_execution.probe(["node", "--version"], cwd=directory, env=env)
        if not node.get("available"):
            return f"verification worker cannot run node: {node.get('reason') or 'unknown runtime failure'}"
    runtime = check_execution.probe(probe_argv, cwd=directory, env=env)
    if runtime.get("available"):
        return None
    return f"verification worker cannot run {argv[0]}: {runtime.get('reason') or 'unknown runtime failure'}"


def _runtime_evidence(argv: list[str], env: dict[str, str], cwd: str) -> dict:
    """Capture the executable/version that was validated for a command."""
    if argv[0] in _JAVA_ENTRYPOINTS:
        java_home = env.get("JAVA_HOME", "")
        java = str(Path(java_home, "bin", "java")) if java_home else shutil.which("java", path=env.get("PATH"))
        evidence = check_execution.probe([java or "java", "-version"], cwd=cwd, env=env)
        return {"runtime": "java", "executable": java or "", **evidence}
    probes = {
        "npm": ("node", ["node", "--version"]), "pnpm": ("node", ["node", "--version"]),
        "yarn": ("node", ["node", "--version"]), "pytest": ("python", ["python3", "--version"]),
        "go": ["go", "version"], "cargo": ["cargo", "--version"],
        "make": ["make", "--version"], "cmake": ["cmake", "--version"],
        "ctest": ["ctest", "--version"], "swift": ["swift", "--version"],
    }
    probe_spec = probes.get(argv[0])
    if not probe_spec:
        return {"runtime": argv[0], "available": True}
    runtime_name, probe_argv = probe_spec if isinstance(probe_spec, tuple) else (argv[0], probe_spec)
    runtime = check_execution.probe(probe_argv, cwd=cwd, env=env)
    return {"runtime": runtime_name, **runtime}


def _prepare_entrypoint(argv: list[str], cwd: str) -> None:
    """Make a checked-in wrapper runnable inside the disposable clone only."""
    if argv[0] not in {"./gradlew", "./mvnw"}:
        return
    wrapper = Path(cwd) / argv[0]
    if wrapper.is_file() and not os.access(wrapper, os.X_OK):
        wrapper.chmod(wrapper.stat().st_mode | 0o111)


def _dependency_cache(repo: str) -> Path:
    base = Path(os.environ.get("CONDUCTOR_DEPENDENCY_CACHE_ROOT") or
                (Path(tempfile.gettempdir()) / "conductor-dependency-cache"))
    remote = git.git(repo, "remote", "get-url", "origin", check=False)
    # Hash the identity before using it so credentials embedded in a remote URL
    # can never appear in a filesystem path or task output. Local worktrees
    # share their common Git directory and therefore their dependency cache.
    identity = remote.stdout.strip() if remote.code == 0 and remote.stdout.strip() else git.common_gitdir(repo)
    cache = base / sha256(identity.encode("utf-8")).hexdigest()[:20]
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _runtime_requirement_reason(argv: list[str], runtime: dict, requirements: dict[str, str]) -> str | None:
    names = {
        "./gradlew": "java", "gradle": "java", "./mvnw": "java", "mvn": "java",
        "npm": "node", "pnpm": "node", "yarn": "node", "pytest": "python",
        "go": "go", "cargo": "rust",
    }
    name = names.get(argv[0])
    expected = requirements.get(name or "")
    if not expected:
        return None
    actual = str(runtime.get("version") or "")
    if actual and not _version_constraint_matches(actual, expected):
        return f"repository requires {name} {expected}, but resolved runtime reports: {actual[:200]}"
    return None


def _version_tuple(value: str) -> tuple[int, ...] | None:
    match = re.search(r"\d+(?:\.\d+){0,2}", value)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right), 3)
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)


def _version_constraint_matches(actual_banner: str, constraint: str) -> bool:
    """Evaluate the ordinary package-engine forms without requiring Node/npm.

    A pinned file such as ``.nvmrc`` uses a bare version and therefore remains
    a same-major requirement. ``package.json`` engines commonly use ranges;
    treating ``>=20`` as exactly major 20 incorrectly blocks a newer valid
    runtime such as Node 22 or 24.
    """
    actual = _version_tuple(actual_banner)
    if not actual:
        return True
    for alternative in constraint.split("||"):
        terms = alternative.strip().split()
        if terms and all(_version_term_matches(actual, term) for term in terms):
            return True
    return False


def _version_term_matches(actual: tuple[int, ...], raw_term: str) -> bool:
    term = raw_term.strip()
    if not term or term in {"*", "latest"}:
        return True
    match = re.match(r"(?P<operator>>=|<=|>|<|=|\^|~)?\s*(?P<version>\d+(?:\.\d+){0,2}|\d+\.x|\d+\.\*)$", term)
    if not match:
        # We cannot safely parse an exotic range. Availability was already
        # established, so preserve a code-check result rather than fabricate a
        # false infrastructure block.
        return True
    expected_text = match.group("version")
    if expected_text.endswith((".x", ".*")):
        return actual[0] == int(expected_text.split(".", 1)[0])
    expected = _version_tuple(expected_text)
    if expected is None:
        return True
    comparison = _compare_versions(actual, expected)
    operator = match.group("operator") or "="
    if operator == ">=":
        return comparison >= 0
    if operator == "<=":
        return comparison <= 0
    if operator == ">":
        return comparison > 0
    if operator == "<":
        return comparison < 0
    if operator == "^":
        return actual[0] == expected[0] and comparison >= 0
    if operator == "~":
        return actual[:max(1, len(expected) - 1)] == expected[:max(1, len(expected) - 1)] and comparison >= 0
    # Bare versions in .nvmrc/.java-version are intentionally major-pinned
    # unless the repository supplied a minor/patch pin.
    return actual[:len(expected)] == expected


def _safe_output(value: str, limit: int = 12_000) -> str:
    """Keep verifier diagnostics readable and inert in task logs/prompts."""
    # Build output is untrusted candidate-controlled text. Preserve line breaks
    # and tabs for diagnostics, but remove terminal controls (including ANSI
    # escape sequences) before it reaches the Conductor UI or repair prompt.
    cleaned = "".join(char for char in str(value or "")
                      if char in {"\n", "\r", "\t"} or ord(char) >= 32)
    return cleaned[-limit:]


def _verification_artifact_dir(candidate_commit: str) -> Path:
    root = Path(os.environ.get("CONDUCTOR_ARTIFACT_ROOT") or
                (Path(tempfile.gettempdir()) / "conductor-check-artifacts"))
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"verification-{candidate_commit[:12]}-", dir=root))


def _write_command_log(directory: Path, index: int, output: str) -> str:
    path = directory / f"command-{index:02d}.log"
    path.write_text(_safe_output(output, 1_000_000), encoding="utf-8")
    return str(path)


def _requires_verification_scope(path: str) -> bool:
    candidate = Path(path)
    if candidate.suffix.lower() in _NO_TEST_EXTENSIONS:
        return False
    if candidate.name in _BUILD_METADATA_NAMES:
        return True
    # `.cc/` is the harness's own metadata on the branch (the program's definition of done and its
    # external checks), never code under test: a team whose branch received the Architect's contract
    # commit while it built had every changed path unmappable and its final cycle blocked (run 884f7eec).
    return not any(part in {"docs", ".github", ".cc"} for part in candidate.parts)


def repository_suite_available(repo_path: str) -> bool:
    """Whether the repository has a whole-suite command: documented, or inferred from its build."""
    try:
        return bool(discover_commands(repo_path, None, mode="full").get("candidates"))
    except Exception:  # noqa: BLE001 - "cannot tell" must not change what a caller would otherwise do
        return False


def integration_test_mode(repo_path: str, candidate_commit: str) -> dict:
    """How to test a merged change: targeted when its files map to exact tests, so a large repository
    stays fast, otherwise the repository's own suite (after the merge the whole suite is the right
    proof; a repository with only ``make test`` was never tested at all before this, run 1117790e)."""
    try:
        discovered = discover_commands(repo_path, candidate_commit, mode="targeted")
    except Exception as exc:  # noqa: BLE001 - the test cycle reports the real problem
        return {"testMode": "targeted", "reason": f"mode selection failed: {exc}"}
    if discovered["coverage"] == "repository":
        return {"testMode": "full", "reason": "the changed files have no exact tests: running the repository suite"}
    if discovered["executionOutcome"] == "discovered":
        return {"testMode": "targeted", "reason": "the changed files map to exact tests"}
    return {"testMode": "targeted", "reason": str(discovered.get("reason") or discovered["executionOutcome"])}


def validator_for(candidate: dict, *, mode: str = "targeted"):
    """Resolve the one validator that governs a candidate, for every caller.

    Discovery and execution both validate, and they used to duplicate this
    dispatch.  A new adapter taught to only one of them passed discovery and
    then failed at execution, so the mapping lives here and both call it.
    """
    if candidate.get("phase") == "prepare":
        return validate_prepare_argv
    if candidate.get("adapter") == "browser-suite":
        return validate_browser_argv
    if mode == "repository":
        return validate_repository_argv
    if candidate.get("adapter") == "configured":
        return validate_configured_argv
    return validate_argv


# Toolchains whose test runner is installed by the project, not the host, so a
# fresh clone cannot run them until dependencies are fetched.
_NEEDS_PREPARE = {"npm", "pnpm", "yarn", "bundle", "rspec", "rake", "composer", "mix", "dotnet"}
# Toolchains that fetch their own dependencies as part of the build: no install step precedes them.
_SELF_RESOLVING = {"go", "cargo", "gradle", "./gradlew", "mvn", "./mvnw", "swift", "zig"}
_PREPARE_SCOPE_FLAG = {"npm": "--prefix", "pnpm": "--dir", "yarn": "--cwd"}


def _candidate_directory(argv: list[str]) -> str:
    """The directory a discovered test candidate targets, "." if unscoped."""
    if not argv:
        return "."
    flag = _PREPARE_SCOPE_FLAG.get(argv[0])
    if flag and len(argv) >= 3 and argv[1] == flag:
        return argv[2]
    return "."


def _with_prepare(root: Path, candidates: list[dict], *, broad: bool = False) -> list[dict]:
    """Prefix each command needing a dependency install with that install, scoped to the directory it
    targets -- a repository with more than one nested project must never install into the wrong one.
    ``broad`` (the commands a caller hands in, such as a team's frozen acceptance tests) also gives any
    runner that is not the project's own (pytest, a script) every manifest's install, each once, since
    it may drive the project's documented commands: a frozen pytest test drove ``npm test``, ``npm run
    build`` and the scenario script in a clone with no node_modules, failed with "vitest: command not
    found", and a test repairer spent three minutes on an environment it could not fix (run f83293f5)."""
    prepares = _prepare_commands(root)
    prepare_by_directory = {command["affectedUnit"].split(":", 1)[1]: command for command in prepares}
    result: list[dict] = []
    added: set[str] = set()
    for candidate in candidates:
        argv = [str(part) for part in candidate.get("argv") or []]
        entrypoint = argv[0] if argv else ""
        if entrypoint in _NEEDS_PREPARE:
            needed = [prepare_by_directory.get(_candidate_directory(argv))]
        elif broad and candidate.get("phase") != "prepare" and entrypoint not in _SELF_RESOLVING:
            needed = prepares
        else:
            needed = []
        for prepare in needed:
            if prepare is None:
                continue
            directory = prepare["affectedUnit"].split(":", 1)[1]
            if directory not in added:
                result.append(prepare)
                added.add(directory)
        result.append(candidate)
    return result


def _discovery_result(*, candidates: list[dict], source_paths: list[str], requirements: dict,
                      changed_paths: list[str], candidate_commit: str | None, mode: str,
                      selection: str, coverage: str, affected_units: list,
                      repair_roots: list[str], repair_scope_resolved: bool,
                      execution_outcome: str, rejected: list[dict],
                      heavy_allowed: bool, reason: str = "") -> dict:
    """One output shape for every discovery path.

    Full mode used to omit ``executionOutcome``, ``coverage``, ``affectedUnits``
    and ``repairRoots``, which the remediation loop reads — so a full-mode
    discovery silently handed the repair agent an empty write scope.
    """
    return {
        "reason": reason,
        "candidates": candidates,
        "sourcePaths": source_paths,
        "runtimeRequirements": requirements,
        "changedPaths": changed_paths,
        "candidateCommit": candidate_commit,
        "mode": mode,
        "selection": selection,
        "coverage": coverage,
        "affectedUnits": affected_units,
        "repairRoots": repair_roots,
        "repairScopeResolved": repair_scope_resolved,
        "executionOutcome": execution_outcome,
        "rejectedCandidates": rejected,
        "heavyAllowed": heavy_allowed,
    }


def commands_for_test_paths(repo_path: str, paths: list[str]) -> list[dict]:
    """The repository's own commands that run exactly these test files (``[{"argv": [...]}]``), from the
    same per-language planners targeted discovery uses; empty when no tooling of the repository maps
    them. Used for acceptance tests that already exist on the branch (an earlier team froze them)."""
    root = Path(repo_path)
    out, seen = [], set()
    for candidate in _changed_scope_candidates(root, [str(p) for p in paths]):
        argv = [str(a) for a in candidate.get("argv") or []]
        if argv and tuple(argv) not in seen:
            seen.add(tuple(argv))
            out.append({"argv": argv})
    return out


def _changed_scope_candidates(root: Path, changed_paths: list[str],
                              rejected: list[dict] | None = None) -> list[dict]:
    planners = (
        _configured_scope_candidates,
        _targeted_gradle_candidates,
        _targeted_maven_candidates,
        _targeted_javascript_candidates,
        _targeted_python_candidates,
        _targeted_go_candidates,
        _targeted_cargo_candidates,
        _targeted_cmake_candidates,
        _targeted_swift_candidates,
        _targeted_ruby_candidates,
        _targeted_dotnet_candidates,
        _targeted_php_candidates,
        _targeted_elixir_candidates,
        _targeted_sbt_candidates,
        _targeted_bazel_candidates,
        _targeted_make_candidates,
    )
    candidates: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for planner in planners:
        try:
            planned = planner(root, changed_paths)
        except VerificationBlocked as exc:
            # A repository verification.json the configured planner cannot read is that planner's
            # problem alone: the other planners and the repository suite still cover the change.
            if rejected is not None:
                rejected.append({"argv": [], "source": planner.__name__, "reason": str(exc)})
            continue
        for candidate in planned:
            # One unmappable planner output must not abort the whole discovery.
            try:
                argv = validator_for(candidate)(candidate["argv"], root=root)
            except VerificationBlocked as exc:
                if rejected is not None:
                    rejected.append({"argv": [str(value) for value in candidate.get("argv") or []],
                                     "source": candidate.get("source", ""), "reason": str(exc)})
                continue
            key = tuple(argv)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({**candidate, "argv": argv})
    return candidates


def discover_commands(repo_path: str, candidate_commit: str | None = None, *,
                      mode: str | None = None, allow_heavy: bool = False,
                      include_browser_tests: bool = False) -> dict:
    """Return the verification commands for one candidate commit. Discovery never blocks.

    ``mode="targeted"`` climbs a ladder from the smallest honest scope: the exact tests the changed
    files map to, then the affected build unit's own test command, then the repository's whole suite
    (documented guidance first, build-system inference second, a heavy suite last) for any changed
    file no per-language planner maps. ``mode="full"`` starts at the suite. A candidate that changes
    only documentation ends ``no_tests_required``; a repository with no test command at all ends
    ``no_tests_found`` with the reason named. Neither is a block: the caller reports it and goes on.

    ``mode=None`` infers the historical behaviour — targeted when a candidate commit is supplied,
    full otherwise. ``include_browser_tests`` only ever adds a candidate in full mode, and only when
    the host already has browser binaries provisioned -- this never triggers an install.
    """
    root = Path(repo_path).resolve()
    _git_root(root)
    resolved_mode = mode or ("targeted" if candidate_commit else "full")
    if resolved_mode not in {"targeted", "full"}:
        raise VerificationBlocked(f"unsupported discovery mode: {resolved_mode}")
    if resolved_mode == "targeted" and not candidate_commit:
        raise VerificationBlocked("targeted discovery requires a candidate commit")
    changed_paths = _changed_paths(root, candidate_commit)
    requirements = runtime_requirements(root)
    rejected: list[dict] = []
    to_verify = [path for path in changed_paths if _requires_verification_scope(path)]
    scoped = _changed_scope_candidates(root, changed_paths, rejected) if changed_paths else []
    repair_roots = sorted({path for candidate in scoped for path in candidate.get("repairRoots", [])})
    common = dict(requirements=requirements, changed_paths=changed_paths, candidate_commit=candidate_commit,
                  mode=resolved_mode, rejected=rejected)
    if resolved_mode == "targeted":
        if not to_verify:
            return _discovery_result(
                candidates=[], source_paths=[], selection="no-tests-required", coverage="not-applicable",
                affected_units=[], repair_roots=[], repair_scope_resolved=False,
                execution_outcome="no_tests_required", heavy_allowed=False,
                reason="the candidate changes only documentation or non-executable files", **common)
        covered = {path for candidate in scoped for path in candidate.get("coveredPaths", [])}
        if scoped and all(path in covered for path in to_verify):
            scopes = {candidate["scope"] for candidate in scoped}
            return _discovery_result(
                candidates=_with_prepare(root, scoped),
                source_paths=sorted({candidate["source"] for candidate in scoped}),
                selection="changed-scope", coverage="focused" if scopes == {"focused"} else "affected",
                affected_units=[candidate["affectedUnit"] for candidate in scoped],
                repair_roots=repair_roots, repair_scope_resolved=True, execution_outcome="discovered",
                heavy_allowed=False, **common)
    # The repository's own suite: full mode, or changed files no per-language planner maps.
    suite, sources, documented, heavy_allowed = _repository_candidates(
        root, changed_paths, allow_heavy=allow_heavy,
        include_browser_tests=resolved_mode == "full" and include_browser_tests, rejected=rejected)
    # The suite subsumes the focused commands, so they are not run twice.
    candidates = suite or scoped
    if not candidates:
        detail = ""
        if sources:
            detail += f" (guides inspected: {', '.join(sources)})"
        if rejected:
            detail += "; rejected: " + ", ".join(
                f"{' '.join(item['argv']) or item['source']} [{item['reason']}]" for item in rejected[:10])
        return _discovery_result(
            candidates=[], source_paths=sources, selection="no-test-command", coverage="not-applicable",
            affected_units=[], repair_roots=repair_roots, repair_scope_resolved=bool(repair_roots),
            execution_outcome="no_tests_found", heavy_allowed=heavy_allowed,
            reason="no test command found in the repository: nothing documented in AGENTS.md/README and "
                   "no supported build file declares one" + detail, **common)
    return _discovery_result(
        candidates=_with_prepare(root, candidates),
        source_paths=sorted(sources) if suite else sorted({candidate["source"] for candidate in scoped}),
        selection=("documented-guide" if documented else "build-system-inference") if suite else "changed-scope",
        coverage="repository" if suite else "affected",
        affected_units=[] if suite else [candidate["affectedUnit"] for candidate in scoped],
        repair_roots=repair_roots, repair_scope_resolved=bool(repair_roots), execution_outcome="discovered",
        heavy_allowed=heavy_allowed, **common)


def _dedupe_argv(candidates: list[dict]) -> list[dict]:
    out, seen = [], set()
    for candidate in candidates:
        key = tuple(candidate["argv"])
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _repository_candidates(root: Path, changed_paths: list[str], *, allow_heavy: bool,
                           include_browser_tests: bool, rejected: list[dict]) -> tuple[list[dict], list[str], bool, bool]:
    """The repository's whole-suite commands: ``(candidates, guide sources, documented?, heavy allowed?)``.

    Documented guidance first, build-system inference second. A heavy suite (end-to-end, integration
    markers) is admitted only when nothing lighter exists: running the repository's one suite beats
    running nothing. Candidate-authored instructions cannot grant command-execution authority: a guide
    the candidate itself edits is skipped in favour of build-system inference.
    """
    changed = set(changed_paths)
    guides = [path for path in _applicable_guides(root, changed_paths) if _relative(root, path) not in changed]
    sources = [_relative(root, path) for path in guides]

    def from_guides(heavy: bool) -> list[dict]:
        found: list[dict] = []
        for path in guides:
            try:
                found.extend(_candidate_from_guide(path.read_text(encoding="utf-8"), _relative(root, path),
                                                   allow_heavy=heavy, root=root))
            except OSError:
                continue
        return _dedupe_argv(found)

    heavy_allowed = allow_heavy
    unique = from_guides(allow_heavy)
    documented = bool(unique)
    if not unique:
        unique = _inferred_candidates(root, rejected)
    if not unique and not allow_heavy:
        # A heavy suite is still the repository's suite: it runs when it is the only one.
        unique = from_guides(True)
        documented = bool(unique)
        if not unique:
            unique = _inferred_candidates(root, rejected, allow_heavy=True)
        heavy_allowed = bool(unique)
    if include_browser_tests and _browser_suite_configured(root):
        reason = browser_suite_reason(root)
        if reason is not None:
            # Declared but not runnable here: diagnostic, never a hard block --
            # opting into browser tests must not fail a run that has no other
            # problem, and it must never trigger an install.
            rejected.append({"argv": [], "source": "browser-suite", "reason": reason})
        else:
            browser_candidate = _browser_suite_candidate(root)
            if browser_candidate is not None:
                unique.append({**browser_candidate, "argv": validate_browser_argv(browser_candidate["argv"])})
            else:
                rejected.append({"argv": [], "source": "browser-suite",
                                 "reason": "browser config present but no dedicated e2e script found"})
    return unique, sources, documented, heavy_allowed


def is_generated_path(path: str) -> bool:
    parts = Path(path).parts
    return any(part in _GENERATED_PARTS for part in parts)


def _disposable_checkout(repo: str, sha: str) -> tuple[str, str]:
    """Create an independent checkout, not a linked Git worktree.

    A linked worktree has a `.git` file pointing at the source repository's
    common Git directory.  Candidate build code can run Git commands, so that
    arrangement lets a verifier mutate source refs/config despite the files
    being disposable.  A no-local clone copies the Git database first and
    isolates all metadata mutations to the temporary checkout.
    """
    commit = git.git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}", check=False)
    if commit.code != 0:
        raise VerificationBlocked(f"candidate commit does not exist: {sha}")
    temp = tempfile.mkdtemp(prefix="conductor-verify-")
    checkout = os.path.join(temp, "candidate")
    env = _isolated_environment(temp)
    try:
        git.clone(repo, checkout, no_local=True, env=env, clean_env=True)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(temp, ignore_errors=True)
        raise RuntimeError(f"unable to clone verification checkout: {exc}") from exc
    # `git clone <local-source>` records the absolute source path as `origin`.
    # A candidate build could read that URL and deliberately operate on the
    # source checkout, bypassing the metadata isolation above. Verification
    # never fetches or pushes, so remove the remote before any candidate code
    # gets control.
    git.git(checkout, "remote", "remove", "origin", check=False, env=env, clean_env=True)
    detached = git.git(checkout, "checkout", "--detach", commit.stdout.strip(), check=False,
                       env=env, clean_env=True)
    if detached.code != 0:
        shutil.rmtree(temp, ignore_errors=True)
        raise RuntimeError((detached.stderr or detached.stdout or "unable to check out verification candidate").strip())
    # The source's tool cache (git-excluded: a scout's Playwright and Chromium under .cc-cache, the npm
    # and pip caches) comes along by hard link, so a check's setup is already satisfied here and a
    # frozen test that drives the scenario script finds what the script needs (run f83293f5).
    git.seed_cache(os.path.join(repo, git.CACHE_DIR), os.path.join(checkout, git.CACHE_DIR))
    return temp, checkout


# ─── check memo: one run per (code, command) ───
# A frozen acceptance test ran at the same code four times in one small program (its red run, the
# team's final cycle, the program's final cycle, the Architect's evaluation after an amendment that
# changed only `.cc/`), and every wave re-ran every integrated item's tests: test time grew with the
# square of the item count. A completed run (passed or code_failed, never an infrastructure block) is
# remembered per (code tree without `.cc/`, argv, sandbox, scope) in the repository's dependency cache
# and reused by any later caller on the same code. CONDUCTOR_CHECK_MEMO=0 turns it off.
_MEMO_ENV = "CONDUCTOR_CHECK_MEMO"
_MEMO_FILE = "check-memo.json"
_MEMO_MAX_ENTRIES = 500


def _memo_enabled() -> bool:
    return str(os.environ.get(_MEMO_ENV, "1")).strip().lower() not in {"0", "false", "no", "off"}


def code_tree_id(git_root: str, commit: str) -> str:
    """A digest of every blob at ``commit`` except the harness's `.cc/` directory: two commits with
    the same id hold the same code, whatever the contract commits between them."""
    listing = git.git(git_root, "ls-tree", "-r", commit, check=False)
    if listing.code != 0:
        return ""
    lines = [ln for ln in listing.stdout.splitlines() if "\t" in ln and not ln.split("\t", 1)[1].startswith(".cc/")]
    return sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def _memo_key(tree: str, argv: list[str], sandbox: bool, scope: str) -> str:
    return sha256(json.dumps([tree, list(argv), bool(sandbox), scope]).encode("utf-8")).hexdigest()


def _memo_load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _memo_store(path: Path, entries: dict) -> None:
    """Merge ``entries`` into the memo on disk (atomic replace; the newest 500 entries are kept)."""
    if not entries:
        return
    data = {**_memo_load(path), **entries}
    if len(data) > _MEMO_MAX_ENTRIES:
        data = dict(sorted(data.items(), key=lambda kv: float(kv[1].get("ts") or 0))[-_MEMO_MAX_ENTRIES:])
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _aggregate_outcome(results: list[dict], selected: list[dict]) -> tuple[bool, str]:
    blocked = any(item.get("blocked") for item in results)
    passed = bool(results) and not blocked and all(item.get("exitCode") == 0 for item in results) and len(results) == len(selected)
    outcome = "passed" if passed else "infra_blocked" if blocked else next(
        (item["outcome"] for item in results if item.get("outcome") == "cancelled"), "code_failed")
    return passed, outcome


def verify_candidate(repo_path: str, candidate_commit: str, commands: object | None = None, *,
                     scope: str = "changed", allow_heavy: bool = False, on_progress=None,
                     sandbox: bool = False, in_place: bool = False) -> dict:
    """Run validated checks in a disposable detached worktree at exactly ``candidate_commit``.

    ``in_place`` runs them in ``repo_path`` itself instead: ONE tree per team, where teammates' files
    and the installed dependencies live beside the item's own and a clone of one item's commit has
    neither (calc27: three green items failed their cycles in clones with no package.json or lock
    file, and the repair step reset the shared tree). No install step runs; the agents already did.

    ``scope="changed"`` (the default) validates each command for safety (argv shape, entrypoint
    allowlist, no shell); ``scope="repository"`` is the deliberate full-suite path that also admits
    heavy suites when asked. An empty plan is a passed ``no_tests_found`` outcome, never a block.

    ``on_progress``, when given, is called once before each command starts
    with ``{"commandIndex", "totalCommands", "currentCommand", "completedCommands"}``
    -- a real build/test command can run for a very long time as a single
    blocking call, so this is the only signal available while it runs. The
    caller (test_run) pairs it with a ProgressReporter so the task pushes
    periodic IN_PROGRESS updates instead of looking frozen for the whole
    duration, the same mechanism coding_agent already uses.
    """
    repo = str(Path(repo_path).resolve())
    project_root = Path(repo)
    git_root = _git_root(project_root)
    relative_project = project_root.relative_to(git_root)
    requirements = runtime_requirements(repo)
    if scope not in {"changed", "repository"}:
        raise VerificationBlocked(f"unsupported verification scope: {scope}")
    given = commands
    outcome, reason = "no_tests_found", "no verification command was given"
    if commands is None:
        discovered = discover_commands(repo, candidate_commit,
                                       mode="full" if scope == "repository" else "targeted",
                                       allow_heavy=allow_heavy)
        commands = discovered["candidates"]
        requirements = discovered["runtimeRequirements"]
        outcome, reason = discovered["executionOutcome"], str(discovered.get("reason") or "")
    if not isinstance(commands, list) or not commands:
        return {"verificationState": "passed", "executionOutcome": outcome, "reason": reason,
                "candidateCommit": git.git(str(git_root), "rev-parse", candidate_commit).stdout.strip(),
                "runtimeRequirements": requirements, "coverage": "not-applicable", "affectedUnits": [],
                "commands": [], "reusedCommands": 0}
    selected: list[dict] = []
    for command in commands:
        if isinstance(command, dict):
            validator = validator_for(command, mode="repository" if scope == "repository" else "targeted")
            argv = (validator(command.get("argv"), allow_heavy=allow_heavy, root=project_root)
                    if scope == "repository" or command.get("adapter") == "configured"
                    else validator(command.get("argv"), root=project_root))
            java_version = ((command.get("javaVersion") or requirements.get("java"))
                            if argv[0] in _JAVA_ENTRYPOINTS else None)
            selected.append({"argv": argv, "source": command.get("source", "provided"),
                             "javaVersion": java_version, "phase": command.get("phase"),
                             "scope": command.get("scope"), "affectedUnit": command.get("affectedUnit"),
                             "coveredPaths": command.get("coveredPaths", []),
                             "adapter": command.get("adapter")})
        else:
            # Unstructured commands have no proof of changed-file scope. They
            # are deliberately refused in automated remediation.
            raise VerificationBlocked("verification commands must include changed-scope discovery evidence")
    if given is not None and not any(item.get("phase") == "prepare" for item in selected):
        # Commands handed in (a team's frozen acceptance tests) carry no install step of their own:
        # the checkout's manifests decide it (see _with_prepare).
        selected = [{"javaVersion": None, "adapter": None, **item} for item in _with_prepare(project_root, selected, broad=True)]
    dependency_cache = _dependency_cache(str(git_root))
    if in_place:
        selected = [item for item in selected if item.get("phase") != "prepare"]
    resolved = git.head(str(project_root)) if in_place else git.git(str(git_root), "rev-parse", candidate_commit).stdout.strip()
    tree = code_tree_id(str(git_root), resolved) if _memo_enabled() and not in_place else ""
    memo_path = dependency_cache / _MEMO_FILE
    memo = _memo_load(memo_path) if tree else {}
    # A dependency install is never reused from the memo: it must run in every fresh clone.
    keys = [_memo_key(tree, item["argv"], sandbox, scope) if tree and item.get("phase") != "prepare" else ""
            for item in selected]
    reused: dict[int, dict] = {}
    for n, key in enumerate(keys):
        hit = memo.get(key) if key else None
        if isinstance(hit, dict) and isinstance(hit.get("result"), dict):
            reused[n] = {**hit["result"], "argv": list(selected[n]["argv"]), "reused": True,
                         "reusedFrom": str(hit.get("candidateCommit") or "")}
    to_run = [n for n in range(len(selected)) if n not in reused]
    artifact_dir = _verification_artifact_dir(candidate_commit)
    results_by_index: dict[int, dict] = dict(reused)
    temp = ""
    try:
        actual = resolved
        if to_run:
            if in_place:
                worktree = str(project_root)
                scratch = str(dependency_cache / "in-place-state")
                Path(scratch).mkdir(parents=True, exist_ok=True)
            else:
                temp, checkout_root = _disposable_checkout(str(git_root), candidate_commit)
                worktree = str(Path(checkout_root) / relative_project)
                scratch = temp
                actual = git.head(worktree)
                if actual != resolved:
                    raise VerificationBlocked("disposable worktree did not resolve the candidate commit exactly")
            ran, _passed, _outcome = _execute_selected_commands(
                worktree, scratch, [selected[n] for n in to_run], dependency_cache, requirements, artifact_dir,
                on_progress=on_progress, sandbox=sandbox)
            remembered = {}
            for n, item in zip(to_run, ran):
                results_by_index[n] = item
                if keys[n] and item.get("exitCode") is not None and item.get("outcome") in {"passed", "code_failed"}:
                    remembered[keys[n]] = {"candidateCommit": actual, "ts": time.time(),
                                           "result": {k: v for k, v in item.items() if k != "logPath"}}
            _memo_store(memo_path, remembered)
        results = [results_by_index[n] for n in sorted(results_by_index)]
        passed, execution_outcome = _aggregate_outcome(results, selected)
        non_code_block = execution_outcome in {"infra_blocked", "cancelled"}
        return {"verificationState": "passed" if passed else "blocked" if non_code_block else "failed",
                "executionOutcome": execution_outcome, "candidateCommit": actual,
                "runtimeRequirements": requirements,
                "coverage": "focused" if selected and all(item.get("scope") == "focused" for item in selected) else "affected",
                "affectedUnits": [item.get("affectedUnit") for item in selected],
                "artifactDir": str(artifact_dir),
                "projectRelativePath": relative_project.as_posix() if relative_project.parts else ".",
                "commands": results, "reusedCommands": len(reused), "worktreeIsDisposable": not in_place,
                "gitMetadataIsIsolated": True,
                "sourceRemoteDetached": True}
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def _execute_selected_commands(worktree: str, temp: str, selected: list[dict],
                               dependency_cache: Path, requirements: dict,
                               artifact_dir: Path, *, on_progress=None,
                               sandbox: bool = False) -> tuple[list[dict], bool, str]:
    """Run a pre-validated, pre-selected command list inside an already-prepared
    disposable worktree, returning (results, passed, executionOutcome).

    Extracted from verify_candidate's own inline loop so a caller with its own
    checkout can run the identical execution machinery.
    """
    results: list[dict] = []
    for command in selected:
        if on_progress is not None:
            try:
                on_progress({"commandIndex": len(results) + 1, "totalCommands": len(selected),
                            "currentCommand": command["argv"],
                            "completedCommands": [item["argv"] for item in results]})
            except Exception:  # noqa: BLE001 -- progress must never break the run
                pass
        try:
            java_home = (_java_home(command.get("javaVersion"))
                         if command["argv"][0] in _JAVA_ENTRYPOINTS else None)
        except VerificationBlocked as exc:
            results.append({"argv": command["argv"], "source": command["source"], "exitCode": 127,
                            "scope": command.get("scope"), "affectedUnit": command.get("affectedUnit"),
                            "runtime": {"available": False}, "blocked": True,
                            "outcome": "infra_blocked", "output": str(exc)})
            break
        env = _with_checkout_importable(_isolated_environment(temp, java_home, dependency_cache),
                                        command["argv"], worktree)
        _prepare_entrypoint(command["argv"], worktree)
        runtime = _runtime_evidence(command["argv"], env, worktree)
        blocked_reason = _runtime_block_reason(command["argv"], env, worktree) or _runtime_requirement_reason(
            command["argv"], runtime, requirements
        )
        if blocked_reason:
            results.append({"argv": command["argv"], "source": command["source"], "exitCode": 127,
                            "javaVersion": command.get("javaVersion"),
                            "scope": command.get("scope"), "affectedUnit": command.get("affectedUnit"),
                            "dependencyCache": str(dependency_cache),
                            "runtime": runtime, "blocked": True, "outcome": "infra_blocked",
                            "output": blocked_reason})
            break
        run_options = {"cwd": worktree, "check": False, "env": env, "clean_env": True}
        argv = command["argv"]
        if sandbox:
            # Agent-written code under test runs OS-sandboxed (H17): writes confined to this disposable
            # checkout + scratch/caches, no network but loopback. No sandbox tool = a named block.
            # The commands that fetch from the registry run with the network on (`_needs_network`).
            try:
                argv = check_execution.sandboxed_argv(argv, cwd=worktree,
                                                      writable=[temp, str(dependency_cache), str(artifact_dir)],
                                                      allow_network=_needs_network(command))
            except check_execution.SandboxUnavailable as exc:
                results.append({"argv": command["argv"], "source": command["source"], "exitCode": 127,
                                "scope": command.get("scope"), "affectedUnit": command.get("affectedUnit"),
                                "runtime": runtime, "blocked": True, "outcome": "infra_blocked",
                                "sandbox": "sandbox_unavailable", "output": f"sandbox_unavailable: {exc}"})
                break
        try:
            result = run(argv, **run_options)
        except OSError as exc:
            results.append({"argv": command["argv"], "source": command["source"], "exitCode": 127,
                            "scope": command.get("scope"), "affectedUnit": command.get("affectedUnit"),
                            "dependencyCache": str(dependency_cache), "runtime": runtime,
                            "blocked": True, "outcome": "infra_blocked",
                            "output": f"verification worker could not spawn {command['argv'][0]}: {exc}"})
            break
        full_output = (result.stdout or "") + ("\n" if result.stdout and result.stderr else "") + (result.stderr or "")
        output = _safe_output(full_output)
        results.append({"argv": command["argv"], "source": command["source"], "exitCode": result.code,
                        "sandboxed": bool(sandbox),
                        "javaVersion": command.get("javaVersion"),
                        "scope": command.get("scope"), "affectedUnit": command.get("affectedUnit"),
                        "dependencyCache": str(dependency_cache),
                        "runtime": runtime,
                        "logPath": _write_command_log(artifact_dir, len(results) + 1, full_output),
                        "outcome": "passed" if result.code == 0 else "code_failed",
                        "output": output})
        # A failing command does not end the run: every selected command leaves evidence. Stopping at
        # the first failure meant a team's required check commands never ran after an unrelated
        # repository-wide command failed, and its head was refused as unproven (run 037e1cbe); repair
        # agents also saw one failure at a time. Only an infrastructure block (above) stops the run.
    blocked = any(item.get("blocked") for item in results)
    passed = bool(results) and not blocked and all(item["exitCode"] == 0 for item in results) and len(results) == len(selected)
    execution_outcome = "passed" if passed else "infra_blocked" if blocked else next(
        (item["outcome"] for item in results if item.get("outcome") == "cancelled"),
        "code_failed",
    )
    return results, passed, execution_outcome
