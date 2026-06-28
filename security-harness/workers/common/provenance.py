"""Assertion provenance (design §2 principle 7, §12).

Every claim the harness makes carries WHERE it came from, so contradiction detection and
trust work (a documented invariant that an observation violates is a high-value lead, §12).
Four kinds:
  observed   — seen in the live target's behavior (http_request, exploit, verify, oob, browser)
  documented — stated in the target's docs (docs ingestion)
  source     — read from the implementation (SAST, route extraction, dependency manifests)
  inferred   — a heuristic guess (recon/stack fingerprinting), the weakest

Pure logic: maps a producer's ``source_tool`` to its provenance so producers don't each
have to remember to tag. Unknown producers default to ``observed`` (the safest assumption
for a live-acting tool) unless a caller overrides.
"""

from __future__ import annotations

OBSERVED = "observed"
DOCUMENTED = "documented"
SOURCE = "source"
INFERRED = "inferred"

KINDS = (OBSERVED, DOCUMENTED, SOURCE, INFERRED)

# Substring match on the (lowercased) source_tool -> provenance. Order-independent;
# first hit wins on iteration, so keep keys unambiguous.
_BY_TOOL = {
    "docs_digest": DOCUMENTED, "ingest_docs": DOCUMENTED, "docs": DOCUMENTED,
    "sast": SOURCE, "semgrep": SOURCE, "gitleaks": SOURCE, "trivy": SOURCE,
    "route_extract": SOURCE, "dep_cve": SOURCE, "deps": SOURCE,
    "recon": INFERRED, "infer": INFERRED, "fingerprint": INFERRED, "stack": INFERRED,
}


def classify(source_tool: str, default: str = OBSERVED) -> str:
    """Provenance for a finding/assertion produced by ``source_tool``."""
    t = str(source_tool or "").lower()
    for key, prov in _BY_TOOL.items():
        if key in t:
            return prov
    return default   # http/exploit/verify/oob/browser/active_check -> live behavior = observed
