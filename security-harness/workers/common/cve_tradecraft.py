"""CVE exploitation tradecraft (§14): resolve a version-matched CVE lead into a concrete exploit
HINT (class + technique + oracle) the agent can actually issue, instead of synthesizing a "published
exploit" from the CVE id alone.

Hybrid knowledge source: the curated ``catalog/cve_tradecraft.yaml`` map is consulted first
(overrides[<cve_id>] → classify(dependency + advisory summary) → generic); ``deps.py`` attaches the
runtime OSV/GHSA advisory ``summary`` to each CVE so classify() can route unmapped CVEs, and the agent
can still fetch the advisory/PoC reference through the code_exec egress jail. Pure + best-effort:
returns the generic template on any error so the campaign always gets *some* technique.
"""
from __future__ import annotations

import functools
import os
import re

_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "catalog", "cve_tradecraft.yaml")

# keyword → CVE class, for classifying an unmapped CVE from its dependency name + advisory summary.
_CLASS_KEYWORDS = (
    ("deserialization-gadget", ("deserial", "readobject", "objectinputstream", "gadget", "marshal", "unpickle")),
    ("request-smuggling", ("smuggl", "transfer-encoding", "request splitting", "desync", "http/1.1 paring")),
    ("ssrf-filter-bypass", ("ssrf", "subnet", "ip filter", "ipsubnet", "allowlist", "egress", "rebinding")),
    ("jwt-auth-bypass", ("jwt", "jose", "jws", "jwe", "alg", "signature verif", "pbes2", "kid")),
    ("parser-confusion", ("xml", "xxe", "entity", "encoding", "parser", "protobuf", "deserializ xml", "yaml")),
    ("dos-amplification", ("dos", "denial of service", "decompress", "bomb", "unbounded", "redos",
                           "regular expression", "reset", "amplif", "resource exhaustion", "iteration")),
)


@functools.lru_cache(maxsize=4)
def load(path: str | None = None) -> dict:
    """Parse the curated CVE-tradecraft file. Returns {} on any error/absence. Cached; pass a path or
    set SC_CVE_TRADECRAFT to override (tests use distinct paths)."""
    p = path or os.environ.get("SC_CVE_TRADECRAFT") or _DEFAULT_PATH
    try:
        import yaml
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _classes() -> dict:
    c = load().get("classes")
    return c if isinstance(c, dict) else {}


def classify(dependency: str = "", summary: str = "") -> str:
    """Best-effort CVE class from the dependency name + advisory summary keywords; 'generic' if none."""
    blob = f"{dependency} {summary}".lower()
    for klass, kws in _CLASS_KEYWORDS:
        if any(k in blob for k in kws):
            return klass
    return "generic"


def hint(cve_id: str = "", dependency: str = "", summary: str = "") -> dict:
    """Resolve {cve_id, dependency, class, technique, oracle, family} for a CVE lead.
    Order: curated per-CVE override → classify(dependency+summary) → generic template."""
    classes = _classes()
    generic = classes.get("generic") or {
        "technique": "Map the reachable feature that exercises the dependency and issue the published "
                     "PoC payload from the advisory references; vary encoding/sizing to fit the deployment.",
        "oracle": "Demonstrated runtime impact (exec / OOB / timing / parser oracle / extracted data) — "
                  "dependency+version presence alone is NOT confirmation.",
        "family": "generic",
    }
    cid = str(cve_id or "").strip().upper()
    overrides = load().get("overrides")
    override = overrides.get(cid) if isinstance(overrides, dict) else None

    if isinstance(override, dict):
        klass = str(override.get("class") or "generic")
        base = classes.get(klass) or generic
        out = {**base, **{k: v for k, v in override.items() if k != "class"}}
        out["class"] = klass
    else:
        klass = classify(dependency, summary)
        out = {**(classes.get(klass) or generic)}
        out["class"] = klass

    out.setdefault("family", klass if isinstance(override, dict) else out.get("family") or klass)
    out["cve_id"] = cid
    out["dependency"] = str(dependency or "")
    return out


def hint_line(cve_id: str = "", dependency: str = "", summary: str = "") -> str:
    """A one-line exploit hint for a hypothesis test_plan: 'TECHNIQUE: … | ORACLE: …'."""
    h = hint(cve_id, dependency, summary)
    technique = re.sub(r"\s+", " ", str(h.get("technique") or "")).strip()
    oracle = re.sub(r"\s+", " ", str(h.get("oracle") or "")).strip()
    return f"TECHNIQUE [{h.get('class')}]: {technique}  ORACLE: {oracle}"
