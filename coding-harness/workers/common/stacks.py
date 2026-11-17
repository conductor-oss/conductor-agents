"""Stack packs: what the harness knows about each kind of system it builds.

A swarm is generic: the lead picks the stack and agents use the host's tools. Each stack still needs the same few
things from the harness, and a missing one fails a run late and confusingly (no Xcode: half an hour in; a cache
outside the sandbox: every build "operation not permitted"; a package host the sandbox does not reach: every
install hangs). A pack declares them once:

* ``markers``  files that show a repository uses the stack (detection);
* ``tools``    the host commands it needs, each with the probe that proves it works (checked right after planning:
               a missing toolchain stops the run at once, with what to install);
* ``caches``   environment variables pointing its caches inside the tree (the sandbox writes only there);
* ``hosts``    the package hosts its installs reach (added to the sandbox's allowed hosts for this run only);
* ``ui``       the helper an acceptance check uses to drive its user interface, if any;
* ``smoke``    the default smoke check (run as each item lands);
* ``split``    how to split it so parallel agents never edit the same file;
* ``tests``    its test runners: ``(tool, first-run seconds, test words, match)`` where match is ``first`` (the first
               word is a test word: `dotnet test`), ``any`` (any word is: `xcodebuild ... test`) or ``all`` (every run
               of the tool is a test run: `rspec`). They get the wrappers of common/test_budget.py.

The packs a run uses are the lead's declaration (``stacks`` in the kickoff or plan) plus what the tree shows; the
harness records them in ``.cc/stacks.json``. Test commands of every pack run under the expected-duration rule
(common/test_budget.py).
"""
from __future__ import annotations

import glob
import json
import os
import shutil
from dataclasses import dataclass, field

FILE = os.path.join(".cc", "stacks.json")


@dataclass(frozen=True)
class Pack:
    id: str
    title: str
    markers: tuple[str, ...] = ()
    tools: tuple[tuple[str, tuple[str, ...], str], ...] = ()  # (name, probe argv, how to get it)
    caches: dict = field(default_factory=dict)              # ENV -> sub-folder of .cc-cache
    hosts: tuple[str, ...] = ()
    ui: str = ""
    smoke: str = ""
    split: str = ""
    tests: tuple[tuple[str, int, tuple[str, ...], str], ...] = ()


PACKS: dict[str, Pack] = {p.id: p for p in [
    Pack("web", "Web front end (React, Vue, Svelte; Vite)", ("vite.config.*", "next.config.*", "svelte.config.*", "angular.json"),
         (("node", ("node", "--version"), "Node.js"),), {"npm_config_cache": "npm", "PNPM_HOME": "pnpm", "YARN_CACHE_FOLDER": "yarn"},
         ("registry.npmjs.org", "registry.yarnpkg.com", "nodejs.org"),
         "the browser: PLAYWRIGHT_SCENARIO (a shared Playwright browser)", "start the dev server, load the page, see one element",
         "one owner for package.json and the lockfile; pages and components each owned by one item"),
    Pack("node", "Node.js / TypeScript back end", ("package.json",), (("node", ("node", "--version"), "Node.js"),),
         {"npm_config_cache": "npm"}, ("registry.npmjs.org", "registry.yarnpkg.com", "nodejs.org"), "",
         "start the server, one request, one expected answer", "one owner for package.json, tsconfig and the lockfile"),
    Pack("jvm", "Java / Kotlin (Spring; Maven or Gradle)", ("pom.xml", "build.gradle", "build.gradle.kts"),
         (("java", ("java", "-version"), "a JDK"),), {"GRADLE_USER_HOME": "gradle"},
         ("repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "services.gradle.org"), "",
         "start the service, one request", "one owner for pom.xml / build.gradle and settings.gradle; one package per subsystem"),
    Pack("go", "Go services and CLIs", ("go.mod",), (("go", ("go", "version"), "Go"),),
         {"GOCACHE": "go-build"}, ("proxy.golang.org", "sum.golang.org", "storage.googleapis.com"), "",
         "build and start the binary, one request", "one owner for go.mod; one package per subsystem; a registry of "
         "commands or routes is appended to through one function per package, never edited by two items"),
    Pack("python", "Python (FastAPI, Django, CLIs, data)", ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
         (("python3", ("python3", "--version"), "Python 3"),),
         {"PIP_CACHE_DIR": "pip", "UV_CACHE_DIR": "uv", "POETRY_CACHE_DIR": "poetry"}, ("pypi.org", "files.pythonhosted.org"),
         "the browser, for a web UI", "start the app, one request", "one owner for pyproject.toml and the lockfile"),
    Pack("rust", "Rust", ("Cargo.toml",), (("cargo", ("cargo", "--version"), "Rust (rustup)"),),
         {"CARGO_HOME": "cargo"}, ("crates.io", "static.crates.io", "index.crates.io"), "",
         "build and start the binary, one request", "one crate per subsystem in a workspace; one owner for the root Cargo.toml"),
    Pack("dotnet", ".NET (C#, ASP.NET)", ("*.csproj", "*.sln", "*.fsproj"), (("dotnet", ("dotnet", "--version"), "the .NET SDK"),),
         {"NUGET_PACKAGES": "nuget", "DOTNET_CLI_HOME": "dotnet"}, ("api.nuget.org", "www.nuget.org", "nuget.org"),
         "the browser, for an ASP.NET UI", "dotnet run, one request", "one project per subsystem; one owner for the .sln"),
    Pack("cpp", "C / C++ (CMake)", ("CMakeLists.txt",), (("cmake", ("cmake", "--version"), "CMake and a C/C++ compiler"),),
         {"CCACHE_DIR": "ccache"}, (), "", "build, run the binary once", "one owner for the top CMakeLists.txt; one target per subsystem"),
    Pack("ruby", "Ruby / Rails", ("Gemfile",), (("ruby", ("ruby", "-e", "exit(RUBY_VERSION >= '3.0' ? 0 : 1)"), "Ruby 3+"),),
         {"BUNDLE_PATH": "bundle"}, ("rubygems.org", "index.rubygems.org"),
         "the browser, for a Rails UI", "start the server, one request", "one owner for the Gemfile, routes.rb and db/migrate"),
    Pack("php", "PHP / Laravel", ("composer.json",), (("php", ("php", "--version"), "PHP"), ("composer", ("composer", "--version"), "Composer")),
         {"COMPOSER_HOME": "composer"}, ("repo.packagist.org", "packagist.org"), "the browser",
         "start the server, one request", "one owner for composer.json, routes and migrations"),
    Pack("elixir", "Elixir / Phoenix", ("mix.exs",), (("mix", ("mix", "--version"), "Elixir"),),
         {"MIX_HOME": "mix", "HEX_HOME": "hex"}, ("hex.pm", "repo.hex.pm", "builds.hex.pm"), "the browser",
         "start the endpoint, one request", "one owner for mix.exs and the router"),
    Pack("ios", "iOS / macOS apps (Swift, SwiftUI)", ("*.xcodeproj", "*.xcworkspace", "Package.swift", "project.yml", "Project.swift"),
         (("xcodebuild", ("xcodebuild", "-version"), "Xcode (the full app, not the Command Line Tools)"),
          ("simctl", ("xcrun", "simctl", "list", "runtimes"), "an iOS Simulator runtime (Xcode > Settings > Components)")),
         {"CP_HOME_DIR": "cocoapods"}, ("cdn.cocoapods.org", "github.com", "codeload.github.com", "objects.githubusercontent.com"),
         "`.cc-cache/bin/device ios --scheme <S> [--project <P>|--workspace <W>] [--only-testing <T>]`: the harness "
         "boots a simulator of the check's own on the host, runs the XCUITests, deletes it",
         "build for the simulator and launch the app once",
         "SwiftPM modules (or XcodeGen/Tuist project.yml), one module per subsystem: NEVER several items editing one "
         ".xcodeproj/project.pbxproj; build with -derivedDataPath .cc-cache/DerivedData"),
    Pack("android", "Android (Kotlin, Jetpack Compose)", ("AndroidManifest.xml", "app/build.gradle", "app/build.gradle.kts"),
         (("java", ("java", "-version"), "a JDK"), ("adb", ("adb", "version"), "the Android SDK platform-tools"),
          ("emulator", ("emulator", "-version"), "the Android emulator and a system image")),
         {"GRADLE_USER_HOME": "gradle", "ANDROID_USER_HOME": "android"}, ("dl.google.com", "maven.google.com",
                                                                          "repo.maven.apache.org", "plugins.gradle.org"),
         "`.cc-cache/bin/device android [--module app] [--avd <name>]`: the harness starts an emulator of the "
         "check's own on the host and runs the instrumented tests against it",
         "assemble the debug APK and install it once", "one Gradle module per feature; one owner for settings.gradle and the manifest"),
    Pack("flutter", "Flutter", ("pubspec.yaml",), (("flutter", ("flutter", "--version"), "the Flutter SDK"),),
         {"PUB_CACHE": "pub"}, ("pub.dev", "storage.googleapis.com"),
         "`device ios` / `device android` running integration_test", "flutter build, launch once",
         "one package per feature; one owner for pubspec.yaml"),
    Pack("react-native", "React Native", ("app.json", "metro.config.js"), (("node", ("node", "--version"), "Node.js"),),
         {"npm_config_cache": "npm"}, ("registry.npmjs.org",), "`device ios` / `device android` (instrumented tests)",
         "bundle the JS, launch once", "the ios and android packs' rules, plus one owner for package.json"),
    Pack("electron", "Desktop: Electron", (), (("node", ("node", "--version"), "Node.js"),), {"npm_config_cache": "npm",
                                                                                           "ELECTRON_CACHE": "electron"},
         ("registry.npmjs.org", "github.com", "objects.githubusercontent.com"),
         "`.cc-cache/bin/device electron .cc/checks/<name>.mjs [--main <entry>]`: the scenario's default export gets "
         "{app, page} (Playwright's Electron driver, first window)", "launch the app, see the first window",
         "one owner for the main-process entry and package.json"),
    Pack("tauri", "Desktop: Tauri", ("tauri.conf.json", "src-tauri/tauri.conf.json"),
         (("cargo", ("cargo", "--version"), "Rust"), ("node", ("node", "--version"), "Node.js")), {"CARGO_HOME": "cargo"},
         ("crates.io", "static.crates.io", "index.crates.io", "registry.npmjs.org"), "", "build and launch once",
         "the Rust and web packs' rules; one owner for tauri.conf.json"),
    Pack("iac", "Infrastructure as code (Terraform, Kubernetes, Docker)", ("*.tf", "Dockerfile", "docker-compose.yml", "Chart.yaml"),
         (("terraform", ("terraform", "version"), "Terraform"),), {"TF_PLUGIN_CACHE_DIR": "terraform"},
         ("registry.terraform.io", "releases.hashicorp.com", "registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com"),
         "", "terraform validate (and plan against no real account); never apply to a real cloud account",
         "one module per subsystem; one owner for the backend and provider configuration"),
    Pack("data-ml", "Data / ML pipelines", ("dvc.yaml", "MLproject"), (("python3", ("python3", "--version"), "Python 3"),),
         {"HF_HOME": "huggingface", "TORCH_HOME": "torch", "PIP_CACHE_DIR": "pip"},
         ("pypi.org", "files.pythonhosted.org", "download.pytorch.org", "huggingface.co", "cdn-lfs.huggingface.co"), "",
         "run the pipeline on a small sample", "one owner for the data schema; one stage per item"),
]}


_TESTS = {
    "dotnet": (("dotnet", 300, ("test",), "first"),),
    "cpp": (("ctest", 300, (), "all"),),
    "ruby": (("rspec", 300, (), "all"), ("rails", 300, ("test",), "first"), ("rake", 300, ("test", "spec"), "first")),
    "php": (("phpunit", 300, (), "all"), ("pest", 300, (), "all")),
    "elixir": (("mix", 300, ("test",), "first"),),
    "ios": (("xcodebuild", 1200, ("test", "test-without-building"), "any"), ("swift", 600, ("test",), "first")),
    "android": (("adb", 900, ("instrument",), "any"),),
    "flutter": (("flutter", 600, ("test",), "first"),),
    "iac": (("terraform", 300, ("test", "validate"), "first"),),
}
PACKS = {pid: (Pack(**{**p.__dict__, "tests": _TESTS[pid]}) if pid in _TESTS else p) for pid, p in PACKS.items()}


def test_runners() -> dict[str, tuple[int, tuple[str, ...], str]]:
    """Every pack's test runners: ``{tool: (first-run seconds, test words, match)}``."""
    out = {}
    for p in PACKS.values():
        for tool, seconds, words, match in p.tests:
            out.setdefault(tool, (seconds, words, match))
    return out


def ids() -> list[str]:
    return list(PACKS)


def detect(work: str) -> list[str]:
    """Packs the tree shows, by their marker files (top two levels; never inside dependencies or caches)."""
    found = []
    for p in PACKS.values():
        for marker in p.markers:
            hits = glob.glob(os.path.join(work, marker)) + glob.glob(os.path.join(work, "*", marker))
            if any(".cc-cache" not in h and "node_modules" not in h for h in hits):
                found.append(p.id)
                break
    try:
        with open(os.path.join(work, "package.json"), encoding="utf-8") as fh:
            manifest = fh.read()
        for pid, dep in (("electron", '"electron"'), ("react-native", '"react-native"')):
            if dep in manifest and pid not in found:
                found.append(pid)
    except OSError:
        pass
    # an Electron or React Native app is that pack, not a Node back end too
    if "node" in found and {"electron", "react-native"} & set(found):
        found.remove("node")
    # a package.json next to a front-end config is the web pack, not a Node back end too
    if "web" in found and "node" in found and not glob.glob(os.path.join(work, "server*")):
        found.remove("node")
    return found


# Packs whose code does not build at all until the module file and the shared core types exist (go.mod and the core
# package, Cargo.toml and the core crate, ...). A new project on one of them starts with a `core` item (swarm/team.py).
COMPILED = frozenset({"jvm", "go", "rust", "dotnet", "cpp", "ios", "android", "flutter", "tauri"})


def needs_core(work: str, declared: list[str]) -> list[str]:
    """The compiled packs this plan declares whose module file the tree does not have yet."""
    present = set(detect(work)) if work and os.path.isdir(work) else set()
    return [p for p in dict.fromkeys(declared or []) if p in COMPILED and p not in present]


def chosen(work: str) -> list[str]:
    try:
        with open(os.path.join(work, FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        return [s for s in data.get("stacks") or [] if s in PACKS]
    except (OSError, ValueError):
        return []


def record(work: str, declared: list[str]) -> list[str]:
    """The run's packs: the lead's declaration plus what the tree shows, written to ``.cc/stacks.json``."""
    packs = list(dict.fromkeys([s for s in (declared or []) if s in PACKS] + detect(work)))
    os.makedirs(os.path.dirname(os.path.join(work, FILE)), exist_ok=True)
    with open(os.path.join(work, FILE), "w", encoding="utf-8") as fh:
        json.dump({"stacks": packs}, fh, indent=1)
    return packs


def missing_tools(packs: list[str], env: dict | None = None) -> list[str]:
    """What the host lacks for these packs, each as "<pack>: <what to install> (<probe> failed)"."""
    out = []
    for pid in packs:
        for name, probe, how in PACKS[pid].tools if pid in PACKS else ():
            ok = shutil.which(probe[0], path=(env or os.environ).get("PATH")) is not None
            if ok:
                from .exec import run
                try:
                    ok = run(list(probe), check=False, env=env).code == 0
                except OSError:
                    ok = False
            if not ok:
                out.append(f"{PACKS[pid].title}: needs {how} (`{' '.join(probe)}` did not work on this host)")
    return out


def cache_env(cache_root: str, packs: list[str]) -> dict[str, str]:
    """These packs' caches inside the tree (variables and empty folders), for every session and check run."""
    env = {}
    for p in (PACKS[x] for x in packs if x in PACKS):
        for var, sub in p.caches.items():
            env.setdefault(var, os.path.join(cache_root, sub))
    if "dotnet" in packs:
        env.update(DOTNET_NOLOGO="1", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1", DOTNET_CLI_TELEMETRY_OPTOUT="1")
    for var, path in env.items():
        if var in {v for p in PACKS.values() for v in p.caches}:
            os.makedirs(path, exist_ok=True)
    return env


def hosts(packs: list[str]) -> list[str]:
    return sorted({h for pid in packs if pid in PACKS for h in PACKS[pid].hosts})


def guide(detected: list[str] | None = None) -> str:
    """The packs, as the lead reads them when it splits and plans."""
    lines = []
    for p in PACKS.values():
        lines.append(f"- `{p.id}` ({p.title}): split: {p.split}. Smoke check: {p.smoke}."
                     + (f" UI checks: {p.ui}." if p.ui else ""))
    return ("STACKS. Declare in `stacks` the packs this request uses (the harness adds what the tree already shows): it "
            "sets their caches, allows their package hosts, checks their tools on this host before anyone builds, and "
            "limits their test runs. Follow each declared pack's split rule, and name its smoke check."
            + (f" The tree already shows: {', '.join(detected)}." if detected else "") + "\n" + "\n".join(lines))


def setup_request(missing: list[str]) -> dict:
    """The approver's request for a host that lacks a pack's toolchain: someone installs it and grants."""
    return {"kind": "decision", "target": "host setup: " + "; ".join(missing),
            "why": "This run's platform needs tools this host does not have. Install them on the worker host, then "
                   "grant this request (the harness checks again); deny it to build without them."}
