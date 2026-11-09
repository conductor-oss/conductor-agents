"""Import-light tool policy shared by the coding worker and its unit tests."""

# The coding surface an unattended worker gets. Scoped Bash rules approve only matching
# commands; anything else falls through to dontAsk and is denied.
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(python *)", "Bash(python3 *)", "Bash(node *)", "Bash(npm *)",
    "Bash(npx *)", "Bash(cat *)", "Bash(ls *)", "Bash(pytest *)",
    "Bash(go *)", "Bash(cargo *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
    # Scoped language build, test, format, lint, and package entrypoints.  Do not
    # add generic shells or environment launchers: each rule admits one executable.
    "Bash(./gradlew *)", "Bash(gradle *)", "Bash(./mvnw *)", "Bash(mvn *)",
    "Bash(ant *)", "Bash(sbt *)", "Bash(mill *)", "Bash(bazel *)", "Bash(bazelisk *)", "Bash(java *)",
    "Bash(pip *)", "Bash(pip3 *)", "Bash(uv *)", "Bash(poetry *)", "Bash(pdm *)", "Bash(tox *)", "Bash(nox *)", "Bash(hatch *)", "Bash(coverage *)", "Bash(ruff *)",
    "Bash(yarn *)", "Bash(pnpm *)", "Bash(bun *)", "Bash(deno *)", "Bash(corepack *)", "Bash(tsc *)",
    "Bash(gofmt *)", "Bash(golangci-lint *)", "Bash(rustc *)", "Bash(rustfmt *)", "Bash(clippy-driver *)",
    "Bash(ruby *)", "Bash(bundle *)", "Bash(rake *)", "Bash(rspec *)", "Bash(dotnet *)", "Bash(nuget *)",
    "Bash(make *)", "Bash(cmake *)", "Bash(ctest *)", "Bash(ninja *)", "Bash(meson *)", "Bash(./configure *)",
    "Bash(swift *)", "Bash(xcodebuild *)", "Bash(php *)", "Bash(composer *)", "Bash(mix *)", "Bash(rebar3 *)",
    "Bash(cabal *)", "Bash(stack *)", "Bash(dart *)", "Bash(flutter *)", "Bash(clojure *)", "Bash(lein *)",
    "Bash(lua *)", "Bash(luarocks *)", "Bash(perl *)", "Bash(prove *)", "Bash(cpanm *)", "Bash(Rscript *)", "Bash(zig *)",
    # Claude Code has no native move/delete tool. These remain bounded by the OS sandbox,
    # while the deny list below still wins for destructive/global variants.
    "Bash(git mv *)", "Bash(git rm *)", "Bash(mv *)", "Bash(rm *)",
    "Bash(mkdir *)", "Bash(cp *)", "Bash(touch *)",
]

DEFAULT_DISALLOWED_TOOLS = [
    "WebSearch", "WebFetch",
    "Bash(git push*)", "Bash(git commit*)", "Bash(git reset*)",
    "Bash(rm -rf *)", "Bash(sudo *)",
]


def denied_without_changes(changed, denials) -> bool:
    """True when an unattended agent was blocked and produced no repository change."""
    return not changed and bool(denials)


