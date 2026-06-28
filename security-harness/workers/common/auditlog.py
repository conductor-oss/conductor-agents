"""Tamper-evident action log (spec section 16, 23).

Every action the executor takes against the target is appended as one line to a
per-deployment ``audit.jsonl`` whose entries are hash-chained: each entry's hash
covers the previous entry's hash, so any later edit, reordering, or truncation of
the log is detectable. This gives reproducible, attributable, tamper-evident
provenance for what the harness did -- and lets the harness detect if its own
evidence trail was altered.

Best-effort and never raises: an audit-log failure must not crash an action.
"""

from __future__ import annotations

import hashlib
import json
import os

from common import memory

_GENESIS = "0" * 64


def _dir(host: str) -> str:
    d = os.path.join(memory.state_dir(), memory._safe(memory.fingerprint(host)))
    os.makedirs(d, exist_ok=True)
    return d


def _path(host: str) -> str:
    return os.path.join(_dir(host), "audit.jsonl")


def _last_hash(path: str) -> str:
    try:
        last = None
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last:
            return json.loads(last).get("entry_hash") or _GENESIS
    except (OSError, ValueError):
        pass
    return _GENESIS


def _entry_hash(prev_hash: str, body: dict) -> str:
    blob = prev_hash + json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def append(host: str, entry: dict) -> None:
    """Append one hash-chained action record for ``host``. Best-effort, never raises."""
    if not host:
        return
    try:
        path = _path(host)
        prev = _last_hash(path)
        body = dict(entry)
        body.pop("entry_hash", None)
        body.pop("prev_hash", None)
        record = {**body, "prev_hash": prev, "entry_hash": _entry_hash(prev, body)}
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def verify_chain(path: str) -> dict:
    """Verify the hash chain of an audit.jsonl. Returns {ok, entries, broken_at}."""
    prev = _GENESIS
    n = 0
    try:
        with open(path) as fh:
            for i, line in enumerate(fh):
                if not line.strip():
                    continue
                rec = json.loads(line)
                body = {k: v for k, v in rec.items() if k not in ("prev_hash", "entry_hash")}
                if rec.get("prev_hash") != prev or rec.get("entry_hash") != _entry_hash(prev, body):
                    return {"ok": False, "entries": n, "broken_at": i}
                prev = rec["entry_hash"]
                n += 1
    except (OSError, ValueError) as exc:
        return {"ok": False, "entries": n, "error": str(exc)}
    return {"ok": True, "entries": n, "broken_at": None}
