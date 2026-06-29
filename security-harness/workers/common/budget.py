"""Campaign-wide request / data-volume counters (spec section 15.2).

The manifest can cap a campaign's total work — ``rate.max_requests`` and
``data_volume.max_bytes`` — and ``halt.evaluate`` enforces those caps, but only if it
is handed the running totals. Workers are stateless across task invocations, so the
totals live in a per-run counter file keyed by ``run_id`` (the campaign is one run).

``bump`` does an atomic read-increment-write under an OS file lock so the concurrent
http_request threads (and burst fan-out) accumulate a correct shared total. It is
best-effort and never raises: a counter-store failure must not crash an action — it
returns the in-memory delta so a single action is still counted, and the halt check
simply runs against a slightly stale total.
"""

from __future__ import annotations

import json
import os

from common import memory

try:
    import fcntl  # POSIX advisory locks (darwin/linux)
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def _path(run_id: str) -> str:
    d = os.path.join(memory.state_dir(), "budgets")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, memory._safe(run_id) + ".json")


def bump(run_id: str, requests: int = 0, bytes: int = 0) -> dict:
    """Add ``requests``/``bytes`` to this run's running totals and return the new
    ``{"requests": int, "bytes": int}``. Best-effort, never raises.

    With no ``run_id`` there is nowhere to accumulate, so the per-action delta is
    returned unstored (the action is still counted within itself, just not across the
    campaign)."""
    delta = {"requests": int(requests or 0), "bytes": int(bytes or 0)}
    if not run_id:
        return delta
    try:
        path = _path(run_id)
        # Open for read+write, creating if absent; hold an exclusive lock for the whole
        # read-modify-write so concurrent workers cannot lose increments.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 1 << 16)
            try:
                cur = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (ValueError, AttributeError):
                cur = {}
            total = {"requests": int(cur.get("requests", 0)) + delta["requests"],
                     "bytes": int(cur.get("bytes", 0)) + delta["bytes"]}
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(total).encode("utf-8"))
            return total
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except Exception:
        return delta


def read(run_id: str) -> dict:
    """Current totals for a run (``{"requests": 0, "bytes": 0}`` when unknown)."""
    if not run_id:
        return {"requests": 0, "bytes": 0}
    try:
        with open(_path(run_id)) as fh:
            cur = json.load(fh)
        return {"requests": int(cur.get("requests", 0)), "bytes": int(cur.get("bytes", 0))}
    except (OSError, ValueError):
        return {"requests": 0, "bytes": 0}
