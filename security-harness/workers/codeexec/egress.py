"""Set up the network jail for the code_exec sandbox.

Creates (idempotently):
  - an INTERNAL docker network (no NAT/internet) the sandbox attaches to,
  - an EGRESS docker network the proxy uses to reach the allow-listed hosts,
  - one long-lived allow-listing proxy container (egress_proxy.py from the
    sc-codeexec image) attached to BOTH networks.

The sandbox then runs on the internal network with HTTP(S)_PROXY pointed at the
proxy, so its ONLY path off-box is through the allow-list. Best-effort: if Docker
networking can't be set up, the caller falls back to the hardened-but-unjailed mode
(bridge + the in-sandbox `sc` scope check).

The allow-list is the target host plus the OOB collaborator domain. It is applied
when the proxy is (re)created; if a later run needs a different target, the proxy is
recreated with the union so concurrent steps in one run share a stable jail.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from urllib.parse import urlparse

IMAGE = os.environ.get("SC_CODEEXEC_IMAGE", "sc-codeexec:latest")
INT_NET = "sc-cx-internal"
OUT_NET = "sc-cx-egress"
PROXY = "sc-cx-proxy"
PROXY_PORT = 8888


def _run(args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def _host(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"//{url}", scheme="https").netloc
    return (netloc.split("@")[-1].split(":")[0] or "").lower()


def _net_exists(docker, name):
    rc, out = _run([docker, "network", "inspect", name])
    return rc == 0


def _proxy_allowlist(docker):
    rc, out = _run([docker, "inspect", "-f",
                    "{{range .Config.Env}}{{println .}}{{end}}", PROXY])
    if rc != 0:
        return None  # not running
    for line in out.splitlines():
        if line.startswith("SC_ALLOW="):
            return [h for h in line[len("SC_ALLOW="):].split(",") if h]
    return []


@contextmanager
def _setup_lock():
    """Serialize jail setup across the worker's concurrent code_exec threads so 4
    agents firing run_code at once don't race on creating the shared proxy/networks."""
    lk = os.path.join(tempfile.gettempdir(), "sc-cx-jail.lock")
    fh = open(lk, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def ensure_jail(target_url: str, oob_base: str = "") -> dict | None:
    """Ensure the networks + proxy exist with an allow-list covering target+OOB.
    Returns {network, proxy_url} for the sandbox, or None if setup failed.
    Concurrency-safe: setup is serialized by a file lock."""
    docker = shutil.which("docker")
    if not docker:
        return None
    with _setup_lock():
        return _ensure_jail_locked(docker, target_url, oob_base)


def _ensure_jail_locked(docker, target_url: str, oob_base: str) -> dict | None:

    allow = set()
    th = _host(target_url)
    if th:
        allow.add(th)
    # Allow ONLY the exact OOB collaborator host (e.g. resolve-x.trycloudflare.com),
    # NEVER the trycloudflare.com apex -- allow-listing the apex would let agent code
    # reach ANY attacker's quick-tunnel and exfiltrate the injected token, and would
    # let the sandbox hit its own canary (false-positive SSRF). The host is the
    # ephemeral per-run tunnel FQDN from oob-setup.sh.
    oh = _host(oob_base) if oob_base else ""
    if oh:
        allow.add(oh)

    # Networks (idempotent).
    if not _net_exists(docker, INT_NET):
        _run([docker, "network", "create", "--internal", INT_NET])
    if not _net_exists(docker, OUT_NET):
        _run([docker, "network", "create", OUT_NET])
    if not _net_exists(docker, INT_NET) or not _net_exists(docker, OUT_NET):
        return None

    existing = _proxy_allowlist(docker)
    need = allow if existing is None else (allow | set(existing))
    if existing is None or set(existing) != need:
        # (Re)create the proxy with the (unioned) allow-list.
        _run([docker, "rm", "-f", PROXY])
        rc, out = _run([
            docker, "run", "-d", "--name", PROXY,
            "--network", INT_NET,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-e", f"SC_ALLOW={','.join(sorted(need))}",
            "-e", f"SC_PROXY_PORT={PROXY_PORT}",
            IMAGE, "python", "/opt/sc/egress_proxy.py",
        ])
        if rc != 0:
            return None
        _run([docker, "network", "connect", OUT_NET, PROXY])  # add egress leg

    return {"network": INT_NET, "proxy_url": f"http://{PROXY}:{PROXY_PORT}"}


def teardown_jail():
    docker = shutil.which("docker")
    if not docker:
        return
    _run([docker, "rm", "-f", PROXY])
    _run([docker, "network", "rm", INT_NET])
    _run([docker, "network", "rm", OUT_NET])
