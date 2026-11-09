"""The swarm blackboard — append-only shared memory for a run.

This module holds only the generic append-log mechanics and encodes no construction semantics; the
construction read model is ``common.team_board``. The coding harness owns this copy outright.

Design (unchanged from the original):

  * **Append-only facts, keyed by ``correlation_id``, one object per fact named by the author.**
    Every key is ``facts/{cid}/{kind}/{author}__{uuid}.json`` — per-author-unique, so no two actors
    ever write the same key, writes never need a lock and can never lose an update, even with
    hundreds of concurrent writers. Readers reconstruct state by listing the prefix.
  * **Bulky artifacts** are written once and referenced (the fact carries a reference, not the blob).
  * Storage is pluggable: a local-filesystem backend (default; single host and the test backend), an
    S3 backend, and a Postgres backend for swarm scale (roadmap H6). All satisfy the append-log
    contract with byte-identical keys and records.
  * **Incremental fold cache.** A fact never changes once written, so each process parses a fact at
    most once: ``read_facts`` lists keys, fetches/parses only keys it has not seen, and serves the rest
    from a per-process cache (bounded LRU over runs). Folding the board every wave therefore costs a
    key listing plus the *new* facts, not a re-read of the whole log.

**Configuration seam.** ``backend_from_env`` resolves the backend from env vars whose *names* and
default-dir fallback are configurable via ``configure(...)``. Config is module-level and
per-process. The defaults use ``SWARM_BLACKBOARD_*`` names.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict

__all__ = [
    "configure",
    "backend_from_env",
    "post_fact",
    "read_facts",
    "list_runs",
    "put_artifact",
    "get_artifact",
    "bump_budget",
    "read_budget",
    "send_message",
    "read_inbox",
]


# ─────────────────────────── configuration seam ──────────────────────────────
_CONFIG = {
    "bucket_env": "SWARM_BLACKBOARD_S3_BUCKET",
    "prefix_env": "SWARM_BLACKBOARD_S3_PREFIX",
    "prefix_default": "swarm",
    "dir_env": "SWARM_BLACKBOARD_DIR",
    "pg_env": "SWARM_BLACKBOARD_PG_DSN",
    "default_dir": None,  # str | callable[[], str] | None
}


def configure(**kwargs) -> None:
    """Set the env-var names and default-dir provider this process uses to resolve the backend.

    Recognized keys: ``bucket_env``, ``prefix_env``, ``prefix_default``, ``dir_env``, and
    ``default_dir`` (a path string, or a zero-arg callable returning one). Unknown keys are ignored
    so a caller can pass a superset safely."""
    for k, v in kwargs.items():
        if k in _CONFIG:
            _CONFIG[k] = v


def _default_dir() -> str:
    dd = _CONFIG.get("default_dir")
    if callable(dd):
        return dd()
    if isinstance(dd, str) and dd:
        return dd
    return os.path.join(os.environ.get("STATE_DIR", "./state"), "blackboard")


# ─────────────────────────── storage backends ───────────────────────────────
class _LocalBackend:
    """Filesystem backend: keys are relative paths under ``root``. Shared only across processes
    that share the filesystem (single host, or a mounted volume)."""

    def __init__(self, root: str):
        self.root = root
        self.cache_id = f"local:{os.path.abspath(root)}"

    def _p(self, key: str) -> str:
        return os.path.join(self.root, key)

    def put(self, key: str, data: bytes) -> None:
        p = self._p(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)  # atomic within a filesystem

    def get(self, key: str) -> bytes | None:
        try:
            with open(self._p(key), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def list(self, prefix: str) -> list[str]:
        out: list[str] = []
        stack = [prefix.rstrip("/")]
        while stack:  # scandir walk: one stat-free directory read per level
            rel = stack.pop()
            try:
                entries = list(os.scandir(self._p(rel)))
            except OSError:
                continue
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    stack.append(f"{rel}/{e.name}")
                elif not e.name.endswith(".tmp"):
                    out.append(f"{rel}/{e.name}")
        return sorted(out)


class _S3Backend:  # pragma: no cover - exercised only with boto3 + a bucket
    """S3 backend: keys are object keys under ``prefix`` in ``bucket``. Append-only, so strong
    read-after-write plus list is all we rely on (no atomic read-modify-write needed)."""

    def __init__(self, bucket: str, prefix: str = "", client=None):
        import boto3  # lazy: only needed when S3 is configured
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.s3 = client or boto3.client("s3")
        self.cache_id = f"s3:{bucket}/{self.prefix}"

    def _k(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data)

    def get(self, key: str) -> bytes | None:
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=self._k(key))
            return r["Body"].read()
        except Exception:
            return None

    def list(self, prefix: str) -> list[str]:
        out: list[str] = []
        token = None
        base = self._k(prefix)
        while True:
            kw = {"Bucket": self.bucket, "Prefix": base}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            for obj in r.get("Contents", []):
                k = obj["Key"]
                out.append(k[len(self.prefix) + 1:] if self.prefix else k)
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")
        return sorted(out)


class _PostgresBackend:
    """Postgres backend (roadmap H6: a board under hundreds of concurrent writers). One table, keys and
    record bytes identical to the file backend; ``(cid, kind, key)`` indexed so a kind listing is an
    index range scan. Append-only facts need no locking; the budget counter keys (one per author) are
    upserted. Connections are per thread (psycopg 3, lazy import like boto3)."""

    _DDL = ("CREATE TABLE IF NOT EXISTS swarm_blackboard ("
            " key text PRIMARY KEY, cid text, kind text, ts double precision, body bytea NOT NULL);"
            "CREATE INDEX IF NOT EXISTS swarm_blackboard_prefix ON swarm_blackboard (key text_pattern_ops);"
            "CREATE INDEX IF NOT EXISTS swarm_blackboard_cid_kind ON swarm_blackboard (cid, kind, ts);")

    def __init__(self, dsn: str):
        import psycopg  # noqa: F401  - lazy: only needed when Postgres is configured
        self.dsn = dsn
        self.cache_id = f"pg:{dsn}"
        self._local = threading.local()
        with self._conn().cursor() as cur:
            cur.execute(self._DDL)

    def _conn(self):
        import psycopg
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = psycopg.connect(self.dsn, autocommit=True)
            self._local.conn = conn
        return conn

    @staticmethod
    def _split(key: str) -> tuple[str | None, str | None]:
        parts = key.split("/")
        if len(parts) >= 4 and parts[0] == "facts":
            return parts[1], parts[2]
        return (parts[1] if len(parts) >= 2 else None), None

    def put(self, key: str, data: bytes) -> None:
        cid, kind = self._split(key)
        with self._conn().cursor() as cur:
            cur.execute("INSERT INTO swarm_blackboard (key, cid, kind, ts, body) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (key) DO UPDATE SET body = EXCLUDED.body, ts = EXCLUDED.ts",
                        (key, cid, kind, time.time(), data))

    def get(self, key: str) -> bytes | None:
        with self._conn().cursor() as cur:
            cur.execute("SELECT body FROM swarm_blackboard WHERE key = %s", (key,))
            row = cur.fetchone()
        return bytes(row[0]) if row else None

    def get_many(self, keys: list[str]) -> dict[str, bytes]:
        if not keys:
            return {}
        with self._conn().cursor() as cur:
            cur.execute("SELECT key, body FROM swarm_blackboard WHERE key = ANY(%s)", (list(keys),))
            return {k: bytes(b) for k, b in cur.fetchall()}

    def runs(self) -> list[str]:
        with self._conn().cursor() as cur:
            cur.execute("SELECT cid, max(ts) FROM swarm_blackboard WHERE kind IS NOT NULL "
                        "GROUP BY cid ORDER BY max(ts) DESC")
            return [r[0] for r in cur.fetchall()]

    def list(self, prefix: str) -> list[str]:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._conn().cursor() as cur:
            cur.execute("SELECT key FROM swarm_blackboard WHERE key LIKE %s ORDER BY key", (like,))
            return [r[0] for r in cur.fetchall()]


_PG: dict[str, _PostgresBackend] = {}
_PG_LOCK = threading.Lock()


def backend_from_env(backend=None):
    """Resolve the storage backend. Explicit ``backend`` wins (tests inject one); else Postgres when the
    configured DSN env var is set; else S3 when the configured bucket env var is set; else a local dir
    (the configured dir env var, or the default-dir provider). Env-var names come from ``configure``."""
    if backend is not None:
        return backend
    dsn = os.environ.get(_CONFIG.get("pg_env") or "")
    if dsn:
        with _PG_LOCK:
            if dsn not in _PG:
                _PG[dsn] = _PostgresBackend(dsn)
            return _PG[dsn]
    bucket = os.environ.get(_CONFIG["bucket_env"])
    if bucket:
        return _S3Backend(bucket, os.environ.get(_CONFIG["prefix_env"], _CONFIG["prefix_default"]))
    root = os.environ.get(_CONFIG["dir_env"]) or _default_dir()
    return _LocalBackend(root)


# ─────────────────────────────── facts ──────────────────────────────────────
def _safe(text: str) -> str:
    """Key-safe token. Identical in behavior to ``common.memory._safe(str(text or "anon"))`` so the
    on-disk key layout is byte-for-byte the same after the lift."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", str(text or "anon"))[:60] or "unknown"


def post_fact(correlation_id: str, author: str, kind: str, data: dict, *, backend=None) -> str:
    """Append one fact to the campaign log and return its key. Append-only: the key is unique to
    this author + call, so concurrent writers never collide. Never raises on a bad ``kind`` — it is
    recorded verbatim so a new kind is additive, not a crash."""
    be = backend_from_env(backend)
    cid = _safe(correlation_id)
    rec = {"correlation_id": correlation_id, "author": author, "kind": kind,
           "ts": time.time(), "data": data if isinstance(data, dict) else {"value": data}}
    key = f"facts/{cid}/{_safe(kind)}/{_safe(author)}__{uuid.uuid4().hex}.json"
    be.put(key, json.dumps(rec).encode("utf-8"))
    return key


def list_runs(*, backend=None) -> list[str]:
    """Run ids that have facts on the board, most recently written first."""
    be = backend_from_env(backend)
    if hasattr(be, "runs"):
        return be.runs()
    root = os.path.join(getattr(be, "root", ""), "facts")
    try:
        entries = [e for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return []
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    return [e.name for e in entries]


_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_RUNS = 256
STATS = {"parsed": 0}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _bucket(be, cid: str) -> dict | None:
    """The parsed-fact cache for one (backend, run), or None for a backend with no stable identity."""
    cache_id = getattr(be, "cache_id", None)
    if not cache_id:
        return None
    with _CACHE_LOCK:
        key = (cache_id, cid)
        b = _CACHE.get(key)
        if b is None:
            b = _CACHE[key] = {}
            while len(_CACHE) > _CACHE_RUNS:
                _CACHE.popitem(last=False)
        else:
            _CACHE.move_to_end(key)
        return b


def _parse(raw) -> dict | None:
    if not raw:
        return None
    try:
        rec = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, AttributeError):
        return None
    STATS["parsed"] += 1
    return rec if isinstance(rec, dict) else None


def read_facts(correlation_id: str, kind: str | None = None, *, since: float | None = None,
               backend=None) -> list[dict]:
    """Return all facts for a campaign (optionally one kind), oldest first. ``since`` keeps only facts
    written strictly after that timestamp. Facts already seen by this process come from the
    incremental cache; only new keys are fetched and parsed."""
    be = backend_from_env(backend)
    cid = _safe(correlation_id)
    prefix = f"facts/{cid}/{_safe(kind)}/" if kind else f"facts/{cid}/"
    keys = be.list(prefix)
    cache = _bucket(be, cid)
    recs: list[dict] = []
    missing = [k for k in keys if cache is None or k not in cache]
    fetched = be.get_many(missing) if hasattr(be, "get_many") else {k: be.get(k) for k in missing}
    for key in keys:
        rec = cache.get(key) if cache is not None else None
        if rec is None:
            rec = _parse(fetched.get(key))
            if rec is None:
                continue
            if cache is not None:
                cache[key] = rec
        if since is not None and float(rec.get("ts", 0)) <= float(since):
            continue
        # Cached records are shared; hand callers their own copy so a mutation cannot corrupt the cache.
        recs.append(copy.deepcopy(rec) if cache is not None else rec)
    recs.sort(key=lambda r: r.get("ts", 0))
    return recs


# ───────────────────────────── artifacts ────────────────────────────────────
def put_artifact(correlation_id: str, author: str, data: bytes, ext: str = "bin", *, backend=None) -> dict:
    """Store a bulky blob once and return a reference (never the blob) to thread through the tree."""
    be = backend_from_env(backend)
    cid = _safe(correlation_id)
    key = f"artifacts/{cid}/{_safe(author)}__{uuid.uuid4().hex}.{ext.lstrip('.')}"
    be.put(key, data if isinstance(data, bytes) else str(data).encode("utf-8"))
    return {"blackboard_ref": key, "size": len(data), "author": author}


def get_artifact(ref: dict, *, backend=None) -> bytes | None:
    be = backend_from_env(backend)
    if not isinstance(ref, dict) or not ref.get("blackboard_ref"):
        return None
    return be.get(ref["blackboard_ref"])


# ────────────────────────── budget (lock-free) ──────────────────────────────
def bump_budget(correlation_id: str, author: str, requests: int = 0, bytes: int = 0, *, backend=None) -> dict:
    """Accumulate this author's own counters in its OWN object, then return the campaign totals.

    Each author writes only its own key, so there is no shared counter to lock. Totals are the sum
    across authors — an approximate figure for reporting."""
    be = backend_from_env(backend)
    cid = _safe(correlation_id)
    key = f"budget/{cid}/{_safe(author)}.json"
    cur = {}
    raw = be.get(key)
    if raw:
        try:
            cur = json.loads(raw.decode("utf-8"))
        except (ValueError, AttributeError):
            cur = {}
    mine = {"requests": int(cur.get("requests", 0)) + int(requests or 0),
            "bytes": int(cur.get("bytes", 0)) + int(bytes or 0)}
    be.put(key, json.dumps(mine).encode("utf-8"))
    return read_budget(correlation_id, backend=be)


def read_budget(correlation_id: str, *, backend=None) -> dict:
    be = backend_from_env(backend)
    cid = _safe(correlation_id)
    total = {"requests": 0, "bytes": 0}
    for key in be.list(f"budget/{cid}/"):
        raw = be.get(key)
        if not raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8"))
        except (ValueError, AttributeError):
            continue
        total["requests"] += int(d.get("requests", 0))
        total["bytes"] += int(d.get("bytes", 0))
    return total


# ─────────────────────────── the message bus ────────────────────────────────
def send_message(correlation_id: str, sender: str, to: str, body: str, *, sender_role: str = "",
                 intent: str = "share", thread: str = "", reply_to: str = "", backend=None) -> str:
    """DIRECT agent-to-agent message. ``to`` addresses a specific teammate by workflowId, a whole
    role (``role:exploiter``), or everyone (``all``). Returns the message id, which doubles as the
    thread id for the first message of a conversation so replies can be correlated. Stored as a
    normal fact so it is durable and campaign-scoped like everything else on the board."""
    mid = uuid.uuid4().hex
    rec = {"msg_id": mid, "to": str(to or "all"), "from": sender, "from_role": sender_role,
           "intent": intent, "body": str(body or "")[:4000],
           "thread": thread or mid, "reply_to": reply_to or ""}
    post_fact(correlation_id, sender, "message", rec, backend=backend)
    return mid


def read_inbox(correlation_id: str, me: str, my_role: str = "", *, limit: int = 12, backend=None) -> dict:
    """Return the messages ADDRESSED TO this agent — directly (``to == me``), to its role
    (``to == role:<my_role>``), or broadcast (``to == all``) — newest first, excluding its own.
    Returns ``{available, inbox, digest}`` (digest = a prompt-ready rendering)."""
    msgs = [f.get("data") or {} for f in read_facts(correlation_id, kind="message", backend=backend)]
    role_tag = f"role:{my_role}".lower() if my_role else None
    mine = [m for m in msgs
            if str(m.get("from")) != str(me)
            and (str(m.get("to")) == str(me) or str(m.get("to", "")).lower() == role_tag
                 or str(m.get("to", "")).lower() == "all")]
    mine = mine[::-1][:limit]  # newest first, capped
    lines = []
    for m in mine:
        who = (m.get("from_role") or "agent") + " " + str(m.get("from", ""))[:8]
        tag = f"[{m.get('intent','share')}]"
        rt = f" (re:{m.get('reply_to')[:8]})" if m.get("reply_to") else ""
        lines.append(f"{tag} from {who}{rt}: {m.get('body','')}  <reply to from-id {m.get('from','')} thread {m.get('thread','')}>")
    return {"available": bool(mine), "inbox": mine, "digest": "\n".join(lines)}
