"""The swarm blackboard — campaign-scoped shared memory (design/SWARM.md section 6).

The swarm tree binds nodes (identity, lineage, lifecycle, the kill switch); the blackboard is the
*shared memory* the tree does not give you: a place every agent reads and writes so independent work
in a wave can be synthesized at the standup.

Design (set in the SWARM.md decisions):

  * **Append-only facts, keyed by ``correlation_id``, one object per fact named by the author
    ``workflow_id``.** No two agents ever write the same key, so writes never need a lock and can
    never lose an update (the lesson that retires ``budget.py``'s flock file). Readers reconstruct
    state by listing the prefix.
  * **Bulky artifacts** (evidence, transcripts, PoCs) are written once and referenced, mirroring
    Conductor external-payload-storage: the fact carries a reference, not the blob.
  * **Hot ceilings are NOT here** — the hard throughput/kill limits are native Conductor (task-def
    ``concurrentExecLimit`` and root termination). The budget totals kept here are for reporting.

Storage is pluggable: a local-filesystem backend (default; single-host and the test backend) and an
S3 backend (used when ``SC_BLACKBOARD_S3_BUCKET`` is set). Both satisfy the append-log contract, so
nothing above the backend changes between "local disk" and "S3".
"""

from __future__ import annotations

import json
import os
import time
import uuid

from common import memory

# Fact kinds the swarm posts (design/SWARM.md section 6).
FACT_KINDS = ("hypothesis", "observation", "attempt", "verified_primitive",
              "finding", "claim", "critique", "coverage", "message", "presence")

# How many times a hypothesis may be dispatched before it is retired (anti-zero-shot): a hypothesis
# the exploiter couldn't crack is retried across rounds, carrying forward the prior lessons, up to
# this many attempts. Env-overridable per campaign.
RETRY_MAX = int(os.environ.get("SC_SWARM_RETRY_MAX", "3"))


# ─────────────────────────── storage backends ───────────────────────────────
class _LocalBackend:
    """Filesystem backend: keys are relative paths under ``root``. Shared only across processes
    that share the filesystem (single host, or a mounted volume)."""

    def __init__(self, root: str):
        self.root = root

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
        base = self._p(prefix)
        out: list[str] = []
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                if f.endswith(".tmp"):
                    continue
                full = os.path.join(dirpath, f)
                out.append(os.path.relpath(full, self.root))
        return sorted(out)


class _S3Backend:  # pragma: no cover - exercised only with boto3 + a bucket
    """S3 backend: keys are object keys under ``prefix`` in ``bucket``. Append-only, so strong
    read-after-write plus list is all we rely on (no atomic read-modify-write needed)."""

    def __init__(self, bucket: str, prefix: str = "", client=None):
        import boto3  # lazy: only needed when S3 is configured
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.s3 = client or boto3.client("s3")

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


def backend_from_env(backend=None):
    """Resolve the storage backend. Explicit ``backend`` wins (tests inject one); else S3 when
    ``SC_BLACKBOARD_S3_BUCKET`` is set; else a local dir (``SC_BLACKBOARD_DIR`` or the state dir)."""
    if backend is not None:
        return backend
    bucket = os.environ.get("SC_BLACKBOARD_S3_BUCKET")
    if bucket:
        return _S3Backend(bucket, os.environ.get("SC_BLACKBOARD_S3_PREFIX", "swarm"))
    root = os.environ.get("SC_BLACKBOARD_DIR") or os.path.join(memory.state_dir(), "blackboard")
    return _LocalBackend(root)


# ─────────────────────────────── facts ──────────────────────────────────────
def _safe(text: str) -> str:
    return memory._safe(str(text or "anon"))


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


def read_facts(correlation_id: str, kind: str | None = None, *, backend=None) -> list[dict]:
    """Return all facts for a campaign (optionally one kind), oldest first."""
    be = backend_from_env(backend)
    cid = _safe(correlation_id)
    prefix = f"facts/{cid}/{_safe(kind)}/" if kind else f"facts/{cid}/"
    recs: list[dict] = []
    for key in be.list(prefix):
        raw = be.get(key)
        if not raw:
            continue
        try:
            recs.append(json.loads(raw.decode("utf-8")))
        except (ValueError, AttributeError):
            continue
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

    Each author writes only its own key, so there is no shared counter to lock (the fix for the
    ``budget.py`` flock file that cannot survive hundreds of writers). Totals are the sum across
    authors — an approximate figure for reporting; the HARD ceiling is the task-def rate/concurrency
    limit, not this number."""
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


# ─────────────────────────── the read model ─────────────────────────────────
def assemble_state(correlation_id: str, *, backend=None) -> dict:
    """Assemble campaign state from the append-log: the coverage map, the verified-primitive pool,
    the global tried-signature set, and the findings list — what the allocator and scribe consume."""
    be = backend_from_env(backend)
    facts = read_facts(correlation_id, backend=be)
    primitives, findings, coverage = [], [], {}
    claims = {}
    refuted_titles = set()
    reviewed_titles = set()
    hyp_by_sig: dict[str, dict] = {}
    attempts: dict[str, int] = {}          # signature -> how many times dispatched
    lessons: dict[str, list] = {}          # signature -> lessons from failed attempts
    confirmed_sigs: set[str] = set()       # signatures that have been confirmed (stop retrying)
    for f in facts:
        kind, d = f.get("kind"), f.get("data") or {}
        if kind == "verified_primitive":
            primitives.append({**d, "author": d.get("author") or f.get("author")})
        elif kind == "finding":
            findings.append({**d, "author": d.get("author") or f.get("author")})
            fsig = str(d.get("signature") or "")
            if fsig:
                if d.get("confirmed") is True:
                    confirmed_sigs.add(fsig)
                elif d.get("lesson"):
                    lessons.setdefault(fsig, []).append(str(d["lesson"]))
        elif kind == "attempt" and d.get("signature"):
            # An ATTEMPT marks a hypothesis DISPATCHED; count them so we can RETRY up to the cap.
            attempts[str(d["signature"])] = attempts.get(str(d["signature"]), 0) + 1
        elif kind == "attempt_lesson" and d.get("lesson"):
            # A per-turn LESSON (Gap 3): shared so peers + retries learn from each probe. It feeds the
            # lessons pool but must NOT bump the dispatch counter (that would exhaust retries early).
            ls = str(d["lesson"])
            lsig = str(d.get("signature") or "")
            if lsig and ls not in lessons.get(lsig, []):
                lessons.setdefault(lsig, []).append(ls)
        elif kind == "hypothesis" and d.get("signature"):
            hyp_by_sig[str(d["signature"])] = d
        elif kind == "coverage" and d.get("area"):
            coverage[str(d["area"])] = {**coverage.get(str(d["area"]), {}), **d}
        elif kind == "claim" and d.get("area"):
            claims[str(d["area"])] = d.get("author") or f.get("author")
        elif kind == "critique" and d.get("candidate_title"):
            reviewed_titles.add(str(d["candidate_title"]))
            if d.get("survived") is False:
                refuted_titles.add(str(d["candidate_title"]))
    for fnd in findings:
        if str(fnd.get("title", "")) in refuted_titles:
            # The LLM refuter outranks an UNCONFIRMED candidate, but NOT a finding a deterministic
            # trusted oracle already confirmed (the verifier re-issued the probe and it fired). An LLM
            # opinion must never override a deterministic receipt — that drops real findings (the
            # orkescloud SSRF the refuter wrongly dismissed as "the provider's own auth error"). So an
            # oracle-confirmed finding SURVIVES; the refuter's dissent is recorded for human review.
            if fnd.get("confirmed") is True:
                fnd["refuter_dissent"] = True
            else:
                fnd["refuted"] = True
    # OPEN hypotheses = NOT yet confirmed AND attempted fewer than RETRY_MAX times. A hypothesis the
    # exploiter couldn't crack is RE-OPENED (a retry), carrying the lessons from its failed attempts,
    # so the swarm escalates across rounds instead of zero-shotting the toughest bugs.
    open_hypotheses = []
    for sig, h in hyp_by_sig.items():
        n = attempts.get(sig, 0)
        if sig in confirmed_sigs or n >= RETRY_MAX:
            continue
        open_hypotheses.append({**h, "attempt_count": n, "lessons": lessons.get(sig, [])[-3:]})
    # tried = confirmed or retry-exhausted (dedup/reporting); all known sigs block chain regeneration.
    tried = sorted({s for s in hyp_by_sig if s in confirmed_sigs or attempts.get(s, 0) >= RETRY_MAX})
    review_candidates = [f for f in findings if f.get("confirmed") is True
                         and f.get("refuted") is not True
                         and str(f.get("title", "")) not in reviewed_titles]
    return {
        "correlation_id": correlation_id,
        "verified_primitives": primitives,
        "findings": findings,
        "tried_signatures": tried,
        "all_hypothesis_signatures": sorted(hyp_by_sig.keys()),
        "open_hypotheses": open_hypotheses,
        "review_candidates": review_candidates,
        "coverage": coverage,
        "claims": claims,
        "fact_count": len(facts),
    }


def campaign_rollup(correlation_id: str, *, backend=None) -> dict:
    """Observability rollup by ``correlation_id``: coverage, confirmed findings, and spend — the
    campaign-level view the per-node execution tree does not give you at scale."""
    be = backend_from_env(backend)
    state = assemble_state(correlation_id, backend=be)
    spend = read_budget(correlation_id, backend=be)
    # Confirmed = oracle-confirmed AND not refuted by a peer-review refuter.
    confirmed = [f for f in state["findings"]
                 if f.get("confirmed") is True and f.get("refuted") is not True]
    refuted = [f for f in state["findings"] if f.get("refuted") is True]
    by_sev: dict[str, int] = {}
    for f in confirmed:
        sev = str(f.get("severity", "Unknown"))
        by_sev[sev] = by_sev.get(sev, 0) + 1
    return {
        "correlation_id": correlation_id,
        "coverage_areas": len(state["coverage"]),
        "verified_primitives": len(state["verified_primitives"]),
        "confirmed_count": len(confirmed),
        "confirmed_by_severity": by_sev,
        "refuted_count": len(refuted),
        "spend": spend,
        "fact_count": state["fact_count"],
    }


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
    This is what makes the collaboration a conversation: an agent reads who said what TO IT and can
    reply by workflowId. Returns ``{available, inbox, digest}`` (digest = a prompt-ready rendering)."""
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


def replay_ref(finding: dict) -> dict:
    """Reproducibility handle for a confirmed finding: the producing node's ``workflow_id`` and any
    artifact references, so the finding can be re-derived from durable state."""
    finding = finding or {}
    return {
        "workflow_id": finding.get("author") or finding.get("workflow_id") or "",
        "artifact_refs": [r for r in (finding.get("artifact_refs") or []) if r],
        "signature": finding.get("signature", ""),
    }
