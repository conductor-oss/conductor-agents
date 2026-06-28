"""Security Objective Catalog loader (docs/ROADMAP.md#E0) — the harness spine.

The catalog (`catalog/objectives.yaml`) is the single source of truth for WHAT the
harness tests for. This module loads it, evaluates which objectives are APPLICABLE to a
given target (from `app_model` facts), and exposes helpers used by hypothesize (breadth),
coverage (one cell per applicable objective), and reporting (owasp/asvs/cwe mapping).

Applicability grammar (per entry `applicable_when`):
  "always"            -> always applicable
  "<fact>"            -> applicable iff facts[fact] is truthy
  ["<fact>", ...]     -> applicable iff ALL listed facts truthy (AND)
  + optional `any_of: ["<fact>", ...]` -> OR-ed in (applicable if any are truthy)

Facts are derived from the app_model (explicit `facts` block if present, else inferred
heuristically) plus runtime extras (authenticated, has_source). When unsure we lean
APPLICABLE — for a security tool, over-testing is safer than a false "not applicable".
"""

from __future__ import annotations

import json
import os

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def catalog_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    return os.environ.get("SC_CATALOG", os.path.join(repo, "catalog", "objectives.yaml"))


def load(path: str | None = None) -> list[dict]:
    """Load the catalog (YAML, or JSON fallback). Returns [] on any failure."""
    path = path or catalog_path()
    try:
        with open(path) as fh:
            text = fh.read()
        data = yaml.safe_load(text) if yaml else json.loads(text)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except Exception:
        return []


# Facts the catalog's applicable_when predicates reference.
FACT_KEYS = ["multi_tenant", "has_browser_ui", "handles_payments", "has_secrets_store",
             "has_outbound_fetch", "has_file_upload", "has_graphql", "has_source",
             "authenticated"]

# Heuristic fact inference: substrings in the (lowercased) app_model JSON that imply a fact.
_HEURISTICS = {
    "multi_tenant": ["tenant", "organization", " org ", "workspace", "multi-tenant"],
    "has_browser_ui": ["spa", "browser", " ui", "react", "angular", "vue", "html", "frontend"],
    "handles_payments": ["payment", "billing", "invoice", "charge", "refund", "credit", "price", "checkout"],
    "has_secrets_store": ["secret", "credential", "api key", "apikey", "token", "integration", "vault"],
    "has_outbound_fetch": ["webhook", "http task", "outbound", "fetch", "import from url", "ssrf", "callback", "event"],
    "has_file_upload": ["upload", "import", "file", "proto", "bpm", "attachment"],
    "has_graphql": ["graphql"],
}


def derive_facts(app_model: dict | None, extra: dict | None = None) -> dict:
    """Facts for applicability: heuristics from the model, overridden by an explicit
    `facts` block, overridden by runtime `extra` (authenticated, has_source)."""
    am = app_model if isinstance(app_model, dict) else {}
    blob = json.dumps(am).lower()
    facts = {k: any(s in blob for s in subs) for k, subs in _HEURISTICS.items()}
    explicit = am.get("facts") if isinstance(am.get("facts"), dict) else {}
    facts.update({k: bool(v) for k, v in explicit.items()})
    if extra:
        facts.update({k: bool(v) for k, v in extra.items()})
    return facts


def applicable(entry: dict, facts: dict | None) -> bool:
    facts = facts or {}
    cond = entry.get("applicable_when", "always")
    if cond in (None, "always"):
        ok = True
    elif isinstance(cond, str):
        ok = bool(facts.get(cond))
    elif isinstance(cond, list):
        ok = all(bool(facts.get(c)) for c in cond)
    else:
        ok = True
    any_of = entry.get("any_of")
    if any_of:
        ok = ok or any(bool(facts.get(c)) for c in any_of)
    return ok


def applicable_entries(catalog: list, facts: dict | None) -> list[dict]:
    return [e for e in catalog if applicable(e, facts)]


def na_entries(catalog: list, facts: dict | None) -> list[dict]:
    return [e for e in catalog if not applicable(e, facts)]


def for_class(catalog: list, cls: str) -> list[dict]:
    return [e for e in catalog if e.get("class") == cls]


def coverage_dimensions(catalog: list) -> list[str]:
    seen, out = set(), []
    for e in catalog:
        d = e.get("coverage_dimension")
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def compact(entries: list) -> list[dict]:
    """Trim entries for prompt/coverage use (drop verbose fields)."""
    keep = ("id", "class", "objective", "how_to_test", "impact_evidence",
            "coverage_dimension", "required_capability", "required_identities", "refs")
    return [{k: e[k] for k in keep if k in e} for e in entries]
