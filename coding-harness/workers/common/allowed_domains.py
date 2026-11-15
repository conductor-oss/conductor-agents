"""The hosts every agent session may reach: package registries, toolchain downloads and source hosts
(their builds and installs need them). Kept apart from the session runner so the console can read it
without the agent SDKs."""

DEFAULT_ALLOWED_DOMAINS = [
    "registry.npmjs.org", "registry.yarnpkg.com", "nodejs.org",
    "pypi.org", "files.pythonhosted.org",
    "proxy.golang.org", "sum.golang.org", "storage.googleapis.com",
    "repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "services.gradle.org",
    "crates.io", "static.crates.io", "index.crates.io",
    "rubygems.org", "index.rubygems.org",
    "playwright.download.prss.microsoft.com", "cdn.playwright.dev", "playwright.azureedge.net",
    "github.com", "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com",
]
