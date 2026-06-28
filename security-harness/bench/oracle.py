"""The benchmark as a trustworthy *oracle* for the §19 self-improvement loop (§19.2).

A fixed, self-authored benchmark is an overfitting trap. Three disciplines turn it into an
oracle HC can safely climb:

  living      — every human-ratified real finding is distilled into a permanent fixture
                (``distill`` + ``merge_fixtures``); the corpus grows from production and a
                past win must keep passing forever (a forgetting guard).
  held-out    — promotion is measured on a split the proposer never trained on
                (``kfold`` / ``holdout``); the antidote to benchmark overfitting.
  adversarial — the corpus pairs near-miss NEGATIVES with subtle POSITIVES so precision and
                recall are both under tension (negatives live in expected fixtures with
                ``kind: negative``; see ``score`` precision_failures).

Pure logic, deterministic (order-stable folds, injected ``as_of``) so it is unit-testable.
"""

from __future__ import annotations

import re


def kfold(items: list, k: int) -> list:
    """Deterministic K-fold split. Returns ``k`` dicts ``{train, holdout}`` where holdout is
    the i-th contiguous fold and train is the rest. Order-stable (no RNG) -> reproducible
    promotions (§19.2 held-out, §19.8 reproducibility)."""
    items = list(items or [])
    n = len(items)
    k = max(1, min(int(k), n)) if n else 1
    folds = [items[i::k] for i in range(k)]          # round-robin -> balanced, stable
    out = []
    for i in range(k):
        holdout = folds[i]
        train = [x for j, f in enumerate(folds) if j != i for x in f]
        out.append({"train": train, "holdout": holdout})
    return out


def holdout(items: list, k: int, fold: int) -> list:
    """The holdout subset for one fold — the targets a promotion is scored on."""
    splits = kfold(items, k)
    return splits[fold % len(splits)]["holdout"] if splits else []


_WORD = re.compile(r"[a-z0-9/_-]{3,}")


def _keywords(finding: dict) -> list:
    blob = " ".join(str(finding.get(x, "")) for x in ("title", "location", "evidence")).lower()
    seen, kws = set(), []
    for w in _WORD.findall(blob):
        if w not in seen and w not in ("the", "and", "for", "with", "http", "https"):
            seen.add(w); kws.append(w)
        if len(kws) >= 6:
            break
    return kws


def distill(finding: dict, *, as_of: str) -> dict:
    """Distill a human-ratified confirmed finding into a permanent benchmark fixture
    (§19.2 'living'). Keeps the class/objective so coverage + per-class recall track it."""
    return {
        "id": f"ratified-{(finding.get('content_hash') or finding.get('title') or '')[:12]}",
        "kind": "positive",
        "origin": "ratified",
        "as_of": as_of,
        "class": finding.get("class") or finding.get("objective_class"),
        "objective_id": finding.get("objective_id"),
        "category": finding.get("category") or finding.get("title"),
        "cwe": finding.get("cwe", ""),
        "keywords": _keywords(finding),
    }


def _sig(fx: dict) -> tuple:
    return (fx.get("objective_id"), str(fx.get("category") or "").lower(),
            tuple(sorted(k.lower() for k in (fx.get("keywords") or []))))


def merge_fixtures(corpus: list, new: list) -> dict:
    """Add distilled fixtures to the living corpus, deduped by signature. Returns
    ``{corpus, added}`` — ``added`` is the list of newly-added fixture ids (the forgetting
    guard never removes, only adds)."""
    corpus = list(corpus or [])
    have = {_sig(fx) for fx in corpus}
    added = []
    for fx in new or []:
        s = _sig(fx)
        if s not in have:
            have.add(s); corpus.append(fx); added.append(fx.get("id"))
    return {"corpus": corpus, "added": added}
