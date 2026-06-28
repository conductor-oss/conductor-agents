"""Network helpers shared by scanner workers."""

import os
from urllib.parse import urlsplit, urlunsplit

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def in_docker() -> bool:
    return os.environ.get("SC_IN_DOCKER", "").lower() in ("1", "true", "yes")


def reachable_url(url: str) -> str:
    """Rewrite a localhost target to host.docker.internal when running in a
    container, so a Dockerized worker can reach a target port-mapped on the host.

    On the host this is a no-op. Scope is always enforced on the ORIGINAL url
    (and findings report the original) — only the actual request target is
    rewritten.
    """
    if not in_docker():
        return url
    parts = urlsplit(url)
    if (parts.hostname or "").lower() in LOCAL_HOSTS:
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, f"host.docker.internal{port}",
                           parts.path, parts.query, parts.fragment))
    return url
