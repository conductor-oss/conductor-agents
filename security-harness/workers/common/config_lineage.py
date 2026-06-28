"""Versioned, content-addressed lineage for the harness's tunable configuration.

This is the substrate the §19 hill-climbing meta-loop stands on (design rule H5):
*every config change is a provenanced, reversible assertion* — the catalog, prompts,
and profiles are all versioned, each edition is attributed to the traces/proposal that
motivated it, benchmark-scored before/after, and rolled back automatically on regression.
§19.8 additionally requires reproducible lineage: a champion carries the pinned
(model, benchmark, seed) + justifying traces so one can reproduce *exactly why* vN
replaced vN-1.

We realize "signed, content-addressed" as a **hash chain** (the same tamper-evident
construction as ``auditlog``): each edition is content-addressed by a sha256 over the
artifact, and the lineage store chains editions so any later edit/reorder/truncation is
detectable. A real keypair signature is a drop-in over ``entry_hash``; the integrity and
reproducibility guarantees H5 needs come from the chain + content addressing.

Pure logic, deterministic (the clock is injected as ``as_of`` — matching the workflow
constraint that scripts cannot call the clock), so it is fully unit-testable. Callers
persist the returned store with their own atomic writer (e.g. ``memory.save``).
"""

from __future__ import annotations

import hashlib
import json

# The tunable surfaces of §19.4. Safety/authz is deliberately NOT here: it is never
# tunable (H2), so it never gets an auto-generated edition.
SURFACES = ("catalog", "prompt", "profile", "evidence_bar", "tradecraft")

_GENESIS = "0" * 64


def content_hash(content: str) -> str:
    """Content-address a config artifact (a prompt file, a catalog/profile blob)."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _chain_hash(prev_hash: str, body: dict) -> str:
    blob = prev_hash + json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def next_version(store: list, surface: str, path: str) -> int:
    """The version a new edition of ``surface``/``path`` would take (monotonic, from 1)."""
    h = head(store, surface, path)
    return (h["version"] + 1) if h else 1


def make_edition(
    *,
    surface: str,
    path: str,
    content: str,
    as_of: str,
    version: int | None = None,
    parent_content_hash: str | None = None,
    model: str | None = None,
    seed: int | None = None,
    benchmark: str | None = None,
    provenance: dict | None = None,
    scores: dict | None = None,
) -> dict:
    """Build an edition *body* (no chain fields yet) for a tunable surface.

    ``scores`` is the benchmark before/after (H5 "benchmark-scored before/after");
    ``provenance`` is the proposal/traces that motivated it (§2, H5); ``model``/``seed``/
    ``benchmark`` pin reproducibility (§19.8). ``parent_content_hash`` links to the prior
    edition's content so a chain of *what changed* is explicit, independent of order.
    """
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r}; expected one of {SURFACES}")
    return {
        "surface": surface,
        "path": path,
        "version": int(version) if version is not None else 1,
        "content_hash": content_hash(content),
        "parent_content_hash": parent_content_hash,
        "as_of": as_of,
        "model": model,
        "seed": seed,
        "benchmark": benchmark,
        "provenance": provenance or {},
        "scores": scores or {},
    }


def append(store: list, body: dict) -> dict:
    """Append an edition body to the lineage store, chaining it. Returns the stored record.

    Mutates ``store`` in place (append) and returns the chained record. The chain covers
    the whole store (a single tamper-evident lineage across all surfaces), mirroring
    ``auditlog``."""
    prev = store[-1]["entry_hash"] if store else _GENESIS
    clean = {k: v for k, v in body.items() if k not in ("prev_hash", "entry_hash")}
    record = {**clean, "prev_hash": prev, "entry_hash": _chain_hash(prev, clean)}
    store.append(record)
    return record


def commit(
    store: list,
    *,
    surface: str,
    path: str,
    content: str,
    as_of: str,
    **kw,
) -> dict:
    """Convenience: compute the next version, build the edition (linking to the current
    head's content), and append it. Returns the stored record."""
    cur = head(store, surface, path)
    body = make_edition(
        surface=surface,
        path=path,
        content=content,
        as_of=as_of,
        version=next_version(store, surface, path),
        parent_content_hash=cur["content_hash"] if cur else None,
        **kw,
    )
    return append(store, body)


def snapshot(store: list, items, as_of: str, **pin) -> list:
    """Baseline a set of live config artifacts into the lineage (one edition each).

    ``items`` is an iterable of ``(surface, path, content)`` — typically the live
    ``catalog/objectives.yaml``, every ``prompts/*.md``, and every ``profiles/*.json``,
    so the whole tunable config tree becomes content-addressed and versioned (H5). Returns
    the committed records."""
    return [commit(store, surface=s, path=p, content=c, as_of=as_of, **pin) for s, p, c in items]


def lineage(store: list, surface: str, path: str) -> list:
    """All editions of ``surface``/``path`` in version order."""
    return [r for r in store if r.get("surface") == surface and r.get("path") == path]


def head(store: list, surface: str, path: str) -> dict | None:
    """The current (highest-version) edition of ``surface``/``path``, or None."""
    hist = lineage(store, surface, path)
    return max(hist, key=lambda r: r["version"]) if hist else None


def rollback_target(store: list, surface: str, path: str) -> dict | None:
    """The edition to revert to (one version below head) — the H5 automatic-rollback
    target. None if there is no prior edition (the surface has only its initial version)."""
    hist = sorted(lineage(store, surface, path), key=lambda r: r["version"])
    return hist[-2] if len(hist) >= 2 else None


def verify_chain(store: list) -> dict:
    """Verify the lineage hash chain. Returns {ok, entries, broken_at} (cf. auditlog)."""
    prev = _GENESIS
    for i, rec in enumerate(store):
        body = {k: v for k, v in rec.items() if k not in ("prev_hash", "entry_hash")}
        if rec.get("prev_hash") != prev or rec.get("entry_hash") != _chain_hash(prev, body):
            return {"ok": False, "entries": i, "broken_at": i}
        prev = rec["entry_hash"]
    return {"ok": True, "entries": len(store), "broken_at": None}


def why(record: dict) -> str:
    """One-line, human-readable "why this edition exists" — the §19.8 reproducibility
    answer for *why vN replaced vN-1*: the motivating diagnosis + the benchmark delta."""
    surface, path, ver = record.get("surface"), record.get("path"), record.get("version")
    diag = (record.get("provenance") or {}).get("diagnosis") or "initial"
    scores = record.get("scores") or {}
    before, after = scores.get("before") or {}, scores.get("after") or {}
    delta = ""
    if "recall" in before and "recall" in after:
        delta = f"; recall {before['recall']}→{after['recall']}"
    pin = record.get("model") or "?"
    return f"{surface}:{path} v{ver} — {diag}{delta} (model={pin}, bench={record.get('benchmark') or '?'})"
