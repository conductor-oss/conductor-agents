"""Dependency extraction + known-CVE lookup (ROADMAP E4 — supply chain).

Two sources of dependencies:
  1. SOURCE manifests (high fidelity): build.gradle / gradle.properties / libs.versions.toml,
     pom.xml, package.json, requirements.txt, go.mod -> pinned {ecosystem, name, version}.
  2. STACK INFERENCE (when no source): fingerprint the deployed stack from recon headers
     (Server, X-Powered-By) + the app_model tech list -> best-effort components (version
     often unknown -> flagged).

CVEs come from OSV.dev (free, no key): version-matched when we know the version, else all
known vulns for the package (flagged "version unknown — verify"). The point is to turn
"this app ships dependency X@Y" into "X@Y has CVE-Z — TRY to exploit it", feeding the
supply-chain objective. Pure logic + an injectable fetcher so it is unit-testable.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re

MAX_DEPS = 80
SKIP_DIRS = {".git", "node_modules", "build", "dist", "out", "target", ".gradle", "vendor", ".venv"}

# Gradle/Maven coordinate: group:artifact:version (version may be a $var)
_GRADLE_COORD = re.compile(r"""['"]([a-zA-Z0-9_.\-]+:[a-zA-Z0-9_.\-]+):([a-zA-Z0-9_.\-${}]+)['"]""")
_GRADLE_MAP = re.compile(r"""group\s*:\s*['"]([^'"]+)['"]\s*,\s*name\s*:\s*['"]([^'"]+)['"]\s*,\s*version\s*:\s*['"]([^'"]+)['"]""")
# key=value (gradle.properties / toml) OR key : 'value', (Groovy `versions = [...]` map entry)
_PROP = re.compile(r"""^\s*['"]?([A-Za-z0-9_.\-]+)['"]?\s*[=:]\s*['"]?([0-9][A-Za-z0-9_.\-]*)['"]?\s*,?\s*$""", re.M)
_EXT_VER = re.compile(r"""ext\[['"]([A-Za-z0-9_.\-]+)['"]\]\s*=\s*['"]([0-9][A-Za-z0-9_.\-]*)['"]""")
_PIP = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([0-9][A-Za-z0-9_.\-]*)", re.M)


def _resolve(version: str, props: dict) -> str:
    v = version.strip()
    m = re.fullmatch(r"\$\{?([A-Za-z0-9_.\-]+)\}?", v)
    if m:
        return props.get(m.group(1), "")
    return v if re.match(r"^[0-9]", v) else ""


def _props_from(text: str) -> dict:
    """Version variables from a manifest: gradle.properties / toml `key=value`, Groovy
    `versions = [ key : 'value' ]` map entries, and `ext['x.version']='y'`. Each key is also
    aliased as `versions.<key>` so `${versions.postgres}`-style refs resolve."""
    props: dict = {}
    for k, v in _PROP.findall(text):
        props[k] = v
        props[f"versions.{k}"] = v
    for k, v in _EXT_VER.findall(text):
        props[k] = v
    return props


def parse_source(source_path: str, max_deps: int = MAX_DEPS) -> list[dict]:
    """Extract pinned dependencies from manifests under ``source_path`` (best-effort)."""
    if not source_path or not os.path.isdir(source_path):
        return []
    deps: dict[tuple, dict] = {}
    props: dict = {}
    manifests: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(source_path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in ("gradle.properties", "libs.versions.toml") or fn.endswith(".gradle") \
               or fn in ("pom.xml", "package.json", "requirements.txt", "go.mod"):
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) <= 600_000:
                        manifests.append((fn, open(fp, encoding="utf-8", errors="ignore").read()))
                except OSError:
                    continue
        if len(manifests) > 400:
            break
    # first pass: collect version properties (gradle.properties, toml, AND the Groovy
    # `versions = [...]` map / ext[] vars declared inside .gradle build files)
    for fn, text in manifests:
        if fn in ("gradle.properties", "libs.versions.toml") or fn.endswith(".gradle"):
            props.update(_props_from(text))

    def add(ecosystem, name, version, scope="compile"):
        version = (version or "").strip()
        key = (ecosystem, name, version)
        if name and key not in deps and len(deps) < max_deps:
            deps[key] = {"ecosystem": ecosystem, "name": name, "version": version, "scope": scope}

    for fn, text in manifests:
        if fn.endswith(".gradle"):
            for g, a, ver in [(m.group(1).split(":")[0], m.group(1).split(":")[1], m.group(2)) for m in _GRADLE_COORD.finditer(text)]:
                add("Maven", f"{g}:{a}", _resolve(ver, props),
                    "test" if "test" in text[max(0, text.find(f'{g}:{a}')-30):text.find(f'{g}:{a}')].lower() else "compile")
            for g, a, ver in _GRADLE_MAP.findall(text):
                add("Maven", f"{g}:{a}", _resolve(ver, props))
        elif fn == "pom.xml":
            for dep in re.findall(r"<dependency>(.*?)</dependency>", text, re.S):
                g = re.search(r"<groupId>([^<]+)</groupId>", dep)
                a = re.search(r"<artifactId>([^<]+)</artifactId>", dep)
                v = re.search(r"<version>([^<]+)</version>", dep)
                if g and a:
                    add("Maven", f"{g.group(1)}:{a.group(1)}", _resolve(v.group(1) if v else "", props))
        elif fn == "package.json":
            try:
                pj = json.loads(text)
                for sect, scope in (("dependencies", "compile"), ("devDependencies", "test")):
                    for name, ver in (pj.get(sect) or {}).items():
                        add("npm", name, re.sub(r"^[\^~>=<\s]+", "", str(ver)), scope)
            except ValueError:
                pass
        elif fn == "requirements.txt":
            for name, ver in _PIP.findall(text):
                add("PyPI", name, ver)
        elif fn == "go.mod":
            for name, ver in re.findall(r"^\s*([\w./\-]+)\s+v([0-9][\w.\-]*)", text, re.M):
                add("Go", name, ver)
    return list(deps.values())


# Fingerprint -> guessed component. Low fidelity (version often unknown).
_STACK_HINTS = [
    (re.compile(r"(?i)express"), "npm", "express"),
    (re.compile(r"(?i)nginx"), "OSS", "nginx"),
    (re.compile(r"(?i)spring"), "Maven", "org.springframework.boot:spring-boot"),
    (re.compile(r"(?i)tomcat"), "Maven", "org.apache.tomcat:tomcat-catalina"),
    (re.compile(r"(?i)django"), "PyPI", "django"),
    (re.compile(r"(?i)flask"), "PyPI", "flask"),
    (re.compile(r"(?i)rails"), "RubyGems", "rails"),
]


def infer_stack(app_model: dict | None, recon_meta: dict | None) -> list[dict]:
    """Best-effort component guesses from fingerprint when no source is available."""
    blob = json.dumps(app_model or {}).lower() + " " + json.dumps(recon_meta or {}).lower()
    out, seen = [], set()
    for rx, eco, name in _STACK_HINTS:
        if rx.search(blob) and name not in seen:
            seen.add(name)
            # try to capture an adjacent version like "nginx/1.18.0"
            mv = re.search(rx.pattern + r"[ /-]?v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", blob)
            out.append({"ecosystem": eco, "name": name, "version": (mv.group(1) if mv else ""),
                        "scope": "inferred"})
    return out


def _sort_records(out: list) -> list:
    # Rank version-MATCHED (real, exploitable) above version-unknown (historical-only, must be
    # verified), then by severity. Version-unknown must never outrank a matched CVE (D10).
    out.sort(key=lambda r: (0 if r["version_known"] else 1,
                            _sev_rank(r["top"][0].get("severity") if r["top"] else ""),
                            -r["cve_count"]))
    return out


def _query_feed(deps: list, fetch, *, matched_when_versioned: bool) -> list[dict]:
    """Core per-dep vuln lookup shared by the identity feeds. ``fetch(ecosystem, name, version) ->
    [{id, severity, summary}]`` is injected (the worker passes a real HTTP fetcher; tests pass a
    stub). A feed that version-matches server-side (OSV) sets version_known when a version is
    resolved; a feed that returns the package's advisory list without matching (GHSA) is treated as
    version-UNKNOWN (historical lead, capped at INFO, never auto-attempted).

    Each dependency is an independent network call, so they are fetched CONCURRENTLY (bounded
    pool) — ~80 sequential feed lookups previously pushed dep_cve_scan past its task timeout and
    killed the whole assessment; the result set is order-independent (``_sort_records`` re-sorts)."""
    targets = deps[:MAX_DEPS]

    def _lookup(d: dict) -> dict | None:
        try:
            vulns = fetch(d["ecosystem"], d["name"], d.get("version") or "")
        except Exception:
            vulns = []
        if not vulns:
            return None
        return {
            "dependency": f"{d['name']}@{d.get('version') or '?'}",
            "ecosystem": d["ecosystem"], "scope": d.get("scope", "compile"),
            "version_known": bool(matched_when_versioned and d.get("version")),
            "cve_count": len(vulns),
            "top": vulns[:5],  # each: {id, severity, summary}
        }

    out = []
    if targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
            out = [r for r in ex.map(_lookup, targets) if r]
    return _sort_records(out)


def query_osv(deps: list, fetch) -> list[dict]:
    """Identity feed: OSV.dev, server-side version-matched (free, no key)."""
    return _query_feed(deps, fetch, matched_when_versioned=True)


def query_ghsa(deps: list, fetch) -> list[dict]:
    """Identity feed: GitHub Advisory Database (package-based, like OSV). The worker's GHSA fetch
    returns advisories for the package; we treat them as version-UNKNOWN historical leads (no
    server-side version match), so GHSA broadens CVE *identity* coverage without inflating
    exploitable risk — the merge prefers OSV's version-matched entry for any shared CVE. Degrades
    to [] when no GitHub token is configured."""
    return _query_feed(deps, fetch, matched_when_versioned=False)


def merge_cve_records(*record_lists) -> list[dict]:
    """Union per-dependency CVE records across identity feeds (OSV ∪ GHSA), deduped by CVE id. A
    dependency is version_known if ANY feed matched the version; for a CVE in multiple feeds the
    higher-severity / fuller entry wins. Re-sorted by the standard attempt order."""
    by_dep: dict = {}
    for records in record_lists:
        for r in records or []:
            key = (r.get("dependency"), r.get("ecosystem"))
            acc = by_dep.setdefault(key, {"dependency": r.get("dependency"), "ecosystem": r.get("ecosystem"),
                                          "scope": r.get("scope", "compile"), "version_known": False, "top": {}})
            acc["version_known"] = acc["version_known"] or bool(r.get("version_known"))
            for cve in r.get("top") or []:
                cid = str(cve.get("id") or "")
                if not cid:
                    continue
                cur = acc["top"].get(cid)
                if cur is None or _sev_rank(cve.get("severity")) < _sev_rank(cur.get("severity")):
                    acc["top"][cid] = {**(cur or {}), **{k: v for k, v in cve.items() if v not in (None, "")}}
    merged = []
    for acc in by_dep.values():
        cves = sorted(acc["top"].values(), key=lambda c: _sev_rank(c.get("severity")))
        merged.append({"dependency": acc["dependency"], "ecosystem": acc["ecosystem"], "scope": acc["scope"],
                       "version_known": acc["version_known"], "cve_count": len(cves), "top": cves[:5]})
    return _sort_records(merged)


def nvd_enrich(records: list, fetch_nvd) -> list:
    """Severity enrichment via NVD (by CVE id — NVD is CPE-based, so it's a severity/detail source,
    not a package identity feed). Backfills a missing severity on each top CVE from NVD's CVSS
    baseline. ``fetch_nvd(cve_id) -> {severity, cvss} | None``. Best-effort; mutates + returns."""
    seen: dict = {}
    for r in records or []:
        for cve in r.get("top") or []:
            cid = str(cve.get("id") or "")
            if not cid.upper().startswith("CVE-"):
                continue
            if cid not in seen:
                try:
                    seen[cid] = fetch_nvd(cid) or {}
                except Exception:
                    seen[cid] = {}
            info = seen[cid]
            if info.get("severity") and not cve.get("severity"):
                cve["severity"] = info["severity"]
            if info.get("cvss") and "cvss" not in cve:
                cve["cvss"] = info["cvss"]
    return records


_SEV = {"critical": 0, "high": 1, "medium": 2, "moderate": 2, "low": 3, "": 4}


def _sev_rank(s: str) -> int:
    return _SEV.get(str(s or "").lower(), 4)


# ── Real-world prioritization (§14): which CVE to actually ATTEMPT ───────────────────
# Identity/severity come from OSV/NVD/GHSA; CISA KEV + FIRST EPSS say whether it's
# exploited in the wild; an exploit/PoC index says whether it's weaponized. The attempt
# order is reachable × version-matched × severity × (KEV / high-EPSS) × exploit-available
# (design §14). KEV + exploit-available + reachable beats a higher-CVSS lead with neither.

_SEV_WEIGHT = {"critical": 1.0, "high": 0.8, "medium": 0.5, "moderate": 0.5, "low": 0.2, "": 0.1}
_REACH_MULT = {True: 1.0, None: 0.7, False: 0.2}   # known-reachable / unknown / not-reachable


def _cve_score(cve: dict, *, version_known, reachable, kev, epss, exploitable):
    cid = str(cve.get("id") or "")
    sev = _SEV_WEIGHT.get(str(cve.get("severity") or "").lower(), 0.1)
    score = sev * (1.0 if version_known else 0.3) * _REACH_MULT[reachable]   # D10: matched >> unknown
    on_kev = cid in (kev or set())
    ex = cid in (exploitable or set())
    p = float((epss or {}).get(cid, 0.0))
    score += (0.5 if on_kev else 0.0) + (0.3 if ex else 0.0) + 0.5 * p
    return round(score, 4), {"kev": on_kev, "exploit_available": ex, "epss": round(p, 4)}


def prioritize(records: list, *, kev=None, epss=None, exploitable=None, reachable=None) -> list:
    """Enrich + rank OSV records by real-world exploitation priority (§14).

    ``reachable`` is a set of reachable dependency names (None => reachability unknown).
    Tags each top CVE with {kev, exploit_available, epss}, sets a record ``priority_score``,
    and returns the records sorted by it — with the hard invariant (D10) that a version-
    UNKNOWN record never outranks a version-known one."""
    for r in records or []:
        dep_name = (r.get("dependency") or "").split("@")[0]
        reach = None if reachable is None else (dep_name in reachable)
        best = 0.0
        for cve in r.get("top") or []:
            s, flags = _cve_score(cve, version_known=r.get("version_known"),
                                   reachable=reach, kev=kev, epss=epss, exploitable=exploitable)
            cve.update(flags)
            best = max(best, s)
        r["reachable"] = reach
        r["priority_score"] = best
        r["kev"] = any(c.get("kev") for c in r.get("top") or [])
        r["exploit_available"] = any(c.get("exploit_available") for c in r.get("top") or [])
    return sorted(records or [], key=lambda r: (0 if r.get("version_known") else 1,
                                                -r.get("priority_score", 0.0)))


def top_attempt(records: list) -> dict | None:
    """The single highest-priority CVE to ATTEMPT — restricted to version-MATCHED records
    (D10: a version-unknown CVE is a lead to verify, never auto-exploited). Returns
    {dependency, cve, priority_score} or None. Assumes ``records`` already ``prioritize``d."""
    for r in records or []:
        if not r.get("version_known") or not r.get("top"):
            continue
        cve = max(r["top"], key=lambda c: (c.get("kev", False), c.get("exploit_available", False),
                                           c.get("epss", 0.0), -_sev_rank(c.get("severity"))))
        return {"dependency": r["dependency"], "cve": cve, "priority_score": r.get("priority_score")}
    return None


# ── Intel feeds (§5/§14, P1-1): KEV (exploited-in-the-wild) + EPSS (exploit-probability) ──
# Pure parsers for the public feed shapes; dep_cve_scan's `_intel_feeds` fetches + stamps `as_of`.

def parse_kev(feed) -> set:
    """CISA KEV catalog -> set of CVE ids known-exploited-in-the-wild. Accepts the public
    ``{"vulnerabilities":[{"cveID": ...}]}`` shape (or a bare list of ids/objects)."""
    items = feed.get("vulnerabilities") if isinstance(feed, dict) else feed
    out = set()
    for v in items or []:
        cid = (v.get("cveID") or v.get("cve") or "") if isinstance(v, dict) else str(v)
        if cid:
            out.add(cid)
    return out


def parse_epss(feed) -> dict:
    """FIRST EPSS -> {cve_id: probability}. Accepts ``{"data":[{"cve":..,"epss":"0.97"}]}``."""
    rows = feed.get("data") if isinstance(feed, dict) else feed
    out = {}
    for r in rows or []:
        if isinstance(r, dict) and r.get("cve"):
            try:
                out[r["cve"]] = float(r.get("epss") or 0.0)
            except (TypeError, ValueError):
                pass
    return out
