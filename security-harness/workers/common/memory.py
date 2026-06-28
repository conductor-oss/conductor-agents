"""Persistent cross-run knowledge store (spec section 13).

The harness must have durable investigative memory, not just a per-execution
conversation. Conductor ``workflow.variables`` live for one execution and are lost
at completion; this module is the disk-backed store that survives, keyed by a
*deployment fingerprint* so a conclusion for one build/host never silently
transfers to another.

A run loads prior knowledge at the start (so already-tried hypotheses are skipped
and prior findings inform chaining/regression) and writes the merged knowledge back
at the end. When the deployment fingerprint changes (a release), prior confirmed
findings are marked ``stale`` and must be re-validated rather than trusted (spec 13,
release-triggered invalidation).

Layout (under ``STATE_DIR``, default ``./state``):

    state/<fingerprint>/state.json     -- the versioned knowledge model
    state/<fingerprint>/history.jsonl  -- one append-only line per run
    state/<fingerprint>/audit.jsonl    -- tamper-evident action log (see auditlog.py)

Files are written atomically (temp + os.replace) under a best-effort advisory lock,
which tolerates -- but does not fully serialize -- concurrent runs against one target.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone

from common import findings as findings_mod

try:
    import fcntl  # POSIX only; advisory lock is best-effort
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None


def state_dir() -> str:
    return os.environ.get("STATE_DIR", "./state")


def _safe(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", str(text or ""))[:60] or "unknown"


def fingerprint(host: str, app_version: str | None = None) -> str:
    """Stable directory key for a deployment. The HOST is the stable key (so load
    and save agree across a run); ``app_version`` is stored as a field for
    release-invalidation, NOT folded into the key (which would orphan prior state
    on every version bump). Returns ``<safe-host>-<sha1[:8]>``."""
    h = hashlib.sha1(_safe(host).encode()).hexdigest()[:8]
    return f"{_safe(host)}-{h}"


def _dir(fp: str) -> str:
    d = os.path.join(state_dir(), _safe(fp))
    os.makedirs(d, exist_ok=True)
    return d


def traces_path(fp: str) -> str:
    """Path to the per-deployment verdict-with-reasons trace corpus (P3-5). The §19 ``hc_analyze``
    loop loads this jsonl (via ``trace.load``) to mine recurring failure signatures across runs."""
    return os.path.join(_dir(fp), "traces.jsonl")


def empty_state() -> dict:
    return {
        "host": "", "app_version": "", "updated": "",
        "all_confirmed": [], "all_rejected": [], "all_blind": [],
        "tried_signatures": [], "gaps": [], "coverage": {},
        "contradictions": [], "runs": 0,
    }


def load(fp: str) -> dict:
    """Load the knowledge model for a fingerprint, or an empty skeleton if none."""
    path = os.path.join(state_dir(), _safe(fp), "state.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            skel = empty_state()
            skel.update(data)
            _flag_tampering(skel)
            return skel
    except (OSError, ValueError):
        pass
    return empty_state()


def _flag_tampering(state: dict) -> None:
    """Recompute each stored confirmed finding's content hash; if it no longer matches,
    the on-disk record was altered out-of-band (memory poisoning, spec 23). Mark it
    ``tampered`` and quarantine it from "confirmed" so it is re-verified, not trusted."""
    for f in state.get("all_confirmed") or []:
        stored = f.get("content_hash")
        if stored and findings_mod.content_hash(f) != stored:
            f["tampered"] = True
            f["lifecycle"] = "inconclusive"


def save(fp: str, state: dict) -> str:
    """Atomically write the knowledge model. Returns the file path."""
    d = _dir(fp)
    path = os.path.join(d, "state.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
        json.dump(state, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic on POSIX
    return path


def signature(finding: dict) -> str:
    """Stable cross-run identity of a finding: category + normalized title. Used to
    dedupe the cumulative confirmed set and to detect re-occurrence across runs."""
    cat = str(finding.get("category") or finding.get("owasp") or "other").lower()
    title = re.sub(r"\s+", " ", str(finding.get("title") or "")).strip().lower()
    return f"{cat}|{title}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(finding: dict, fp: str, now: str) -> dict:
    """Attach epistemic-provenance fields (spec section 4) + a tamper-evidence hash."""
    f = dict(finding)
    f.setdefault("provenance", "observed")  # confirmed findings are runtime-observed
    f.setdefault("timestamp", now)
    f.setdefault("fingerprint", fp)
    f.setdefault("confidence", f.get("confidence") or "medium")
    f.setdefault("lifecycle", "confirmed")
    f["content_hash"] = findings_mod.content_hash(f)  # always (re)stamp the integrity hash
    return f


def detect_contradictions(documented_invariants: list, confirmed: list) -> list:
    """Emit unresolved-contradiction assertions (spec section 4): a documented invariant
    the app CLAIMS to enforce that an observed confirmed finding appears to VIOLATE. These
    are high-value research leads (claim vs reality). Token-overlap heuristic."""
    import re

    def toks(t):
        return {w for w in re.split(r"[^a-z0-9]+", str(t or "").lower()) if len(w) >= 4}

    out = []
    for inv in documented_invariants or []:
        text = inv.get("invariant") if isinstance(inv, dict) else inv
        itoks = toks(text)
        if not itoks:
            continue
        for f in confirmed or []:
            ftoks = toks(f.get("title")) | toks(f.get("category"))
            if len(itoks & ftoks) >= 2:
                out.append({
                    "type": "documented-vs-observed",
                    "invariant": text,
                    "violated_by": f.get("title"),
                    "note": "the app documents this invariant but a confirmed finding appears to violate it",
                })
                break
    return out


def merge_run(prior: dict, *, fp: str, host: str, app_version: str,
              new_confirmed: list, new_rejected: list, new_blind: list,
              new_tried: list, gaps: list, coverage: dict,
              documented_invariants: list | None = None,
              run_id: str, now: str | None = None) -> tuple[dict, dict]:
    """Merge one run's results into the prior knowledge model.

    Returns ``(state, stats)``. Dedupes confirmed findings by ``signature``; unions
    tried-signatures; and, when the deployment fingerprint (app_version) changed,
    marks prior confirmed findings that did NOT re-occur this run as ``stale`` so they
    are re-validated rather than silently trusted (release-triggered invalidation).
    """
    now = now or _now()
    prior = prior or empty_state()
    prior_conf = prior.get("all_confirmed") or []
    prior_version = prior.get("app_version") or ""
    released = bool(prior_version) and bool(app_version) and prior_version != app_version

    new_by_sig = {signature(f): _stamp(f, fp, now) for f in (new_confirmed or [])}
    merged: dict[str, dict] = {}
    stale = 0
    reconfirmed = 0

    # Carry prior confirmed forward, applying lifecycle transitions.
    for f in prior_conf:
        sig = signature(f)
        if sig in new_by_sig:
            # Re-observed this run -> keep the fresh evidence; if it was stale/remediated,
            # it has reopened.
            nf = new_by_sig.pop(sig)
            nf["lifecycle"] = "confirmed"
            nf["first_seen"] = f.get("first_seen") or f.get("timestamp") or now
            merged[sig] = nf
            reconfirmed += 1
        else:
            g = dict(f)
            if released:
                g["lifecycle"] = "stale"
                g["stale_reason"] = f"not re-observed after release {prior_version} -> {app_version}"
                stale += 1
            merged[sig] = g

    # Brand-new findings this run.
    for sig, f in new_by_sig.items():
        f["first_seen"] = now
        merged[sig] = f

    merged_confirmed = list(merged.values())
    contradictions = _unique((prior.get("contradictions") or [])
                             + detect_contradictions(documented_invariants or [], merged_confirmed))
    state = dict(prior)
    state.update({
        "host": host or prior.get("host") or "",
        "app_version": app_version or prior_version,
        "updated": now,
        "all_confirmed": merged_confirmed,
        "all_rejected": (prior.get("all_rejected") or []) + (new_rejected or []),
        "all_blind": _unique((prior.get("all_blind") or []) + (new_blind or [])),
        "tried_signatures": sorted(set((prior.get("tried_signatures") or []) + (new_tried or []))),
        "gaps": gaps if gaps else prior.get("gaps") or [],
        "coverage": coverage if coverage else prior.get("coverage") or {},
        "contradictions": contradictions,
        "runs": int(prior.get("runs") or 0) + 1,
    })
    stats = {
        "prior_loaded": len(prior_conf),
        "reconfirmed": reconfirmed,
        "stale_revalidated": stale,
        "total_confirmed": len(merged),
        "released": released,
        "new_this_run": len(new_confirmed or []),
    }
    return state, stats


def append_history(fp: str, entry: dict) -> None:
    """Append one run record to history.jsonl (best-effort, never raises)."""
    try:
        with open(os.path.join(_dir(fp), "history.jsonl"), "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _unique(items: list) -> list:
    out, seen = [], set()
    for it in items:
        key = json.dumps(it, sort_keys=True) if isinstance(it, (dict, list)) else it
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out
