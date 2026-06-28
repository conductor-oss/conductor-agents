"""Feature inventory + reflection classifier for the feature-complete exploitation sweep.

Leaks hide in features never intended as security-sensitive (a display-name that stores XSS, a
search filter with SQLi, an export-filename with traversal). The objective/LLM-driven hypothesizer
probes what *looks* interesting; this module instead enumerates EVERY input-bearing feature from all
available signal so the sweep can triage each one and a per-feature coverage ledger can prove "we
tested every feature".

`build_inventory` is defensive: app_model.features is often sparse/absent on real targets, so it
merges whatever is present across the live surface, the app model, and the docs digest.
`classify_reflection` maps how a planted polyglot canary surfaces in a response to candidate
injection classes (the cheap triage signal that gates the expensive deep ladder walk).

Pure logic + unit-tested; the worker tasks (build_feature_inventory / feature_triage) wrap it.
"""

from __future__ import annotations

import re

from common import tradecraft

# Common injectable query-param names to probe on GET/DELETE features when the crawl didn't capture
# the real parameter (black-box targets rarely expose param names). The polyglot canary is fired in
# each; a hit on any (e.g. ?q=) surfaces the leak. Curated + bounded to keep one request per feature.
COMMON_QUERY_PARAMS = tradecraft.signatures("common_query_params", (
    "q", "query", "search", "s", "term", "keyword", "filter", "id", "name", "title",
    "file", "filename", "path", "dir", "url", "uri", "redirect", "return", "next", "callback",
    "page", "sort", "order", "category", "lang", "view", "template", "include",
))

# Name/path cues that a feature carries user input which is reflected, stored, or reaches a sink —
# these get priority in the sweep (the "unintended" leak surfaces).
_PRIO_CUES = (
    "search", "query", "filter", "name", "label", "title", "comment", "feedback", "message",
    "description", "template", "import", "upload", "webhook", "callback", "url", "redirect",
    "file", "path", "export", "report", "render", "preview", "email", "note", "tag", "alias",
)

# Polyglot triage payload: ONE value per field that simultaneously probes every class. We watch
# which marker surfaces. 1337*1337=1787569 is an arithmetic oracle unlikely to occur naturally.
_XSS_MARK = "<svg/onload=scx>"
_SSTI_EXPR = ("{{1337*1337}}", "${1337*1337}", "#{1337*1337}")
_SSTI_RESULT = "1787569"
_TRAV_MARK = "../../../../etc/passwd"
_SQL_SIGNATURES = tradecraft.signatures("sql_signatures", (
    "sql syntax", "sqlstate", "ora-0", "ora-1", "you have an error in your sql",
    "unterminated quoted string", "unclosed quotation", "psqlexception",
    "sqliteexception", "sqlite_error", "sqlite3", "sqliteerror", "sequelize",
    "queryfailederror", "sqlexception", "odbc", "jdbc", "syntaxerrorexception",
    "near \"", "mysqlsyntaxerror", "syntax error at or near", "warning: mysql",
    "syntax error",   # common across SQLite/Sequelize error pages (SQLITE_ERROR ... syntax error)
))
_FILE_SIGNATURES = tradecraft.signatures("file_signatures", (
    "no such file", "filenotfound", "java.io.filenotfoundexception", "enoent",
    "failed to open stream", "could not open", "is a directory",
))

# ───────────────────────── SSRF internal-reach oracle ─────────────────────────
# An egress denylist returns a distinctive cluster-block 403 ("...blocked in this cluster...") for
# the targets it filters. A bypass that REACHES the internal service instead returns a backend
# response — so the deterministic oracle is the DIFFERENTIAL: an internal target was addressed AND
# the response carries a SERVER-emitted reach signal that is not the cluster block. This is what
# turns an [::1]:8080 200/401 into a confirmed internal-reach finding instead of a false-negative
# "blocked". All three lists are HC-tunable data (catalog/tradecraft.yaml).
_EGRESS_BLOCK_SIGNATURES = tradecraft.signatures("egress_block_signatures", (
    "blocked in this cluster", "calls to this domain are blocked",
))
# Proof that an INTERNAL target representation was the thing addressed (the attempt aimed inside).
_INTERNAL_TARGET_SIGNATURES = tradecraft.signatures("internal_target_signatures", (
    "127.0.0.1", "localhost", "[::1]", "0:0:0:0:0:0:0:1", "169.254.169.254", "[fd00:ec2::254]",
    "[::ffff:", "metadata.google.internal", "100.100.100.200", "actuator", "computemetadata",
    "kubernetes.default", "2130706433", "0x7f",
))
# SERVER-emitted signals the request REACHED an internal service — health/actuator bodies, cloud
# IMDS bodies, an internal OpenAPI spec, or a backend-generated auth error (INVALID_TOKEN / "Token
# cannot be null") that PROVES the request traversed to the backend app (the egress filter would
# have returned the cluster block instead). None of these appear in the attacker's own payload.
_INTERNAL_REACH_SIGNATURES = tradecraft.signatures("internal_reach_signatures", (
    "\"status\":\"up\"", "\"status\": \"up\"", "activeprofiles", "propertysources",
    "ami-id", "instance-identity", "iam/security-credentials", "accesskeyid", "metadata-flavor",
    "computemetadata", "invalid_token", "token cannot be null", "\"openapi\"", "\"swagger\"",
))


def egress_blocked(text: str | None) -> bool:
    """True iff the response is the cluster egress-denylist block — BLOCKED, never confirmation."""
    t = (text or "").lower()
    return any(s in t for s in _EGRESS_BLOCK_SIGNATURES)


def internal_reach(text: str | None) -> tuple[bool, str]:
    """SSRF oracle: True iff an internal target was addressed AND a server-emitted reach signal is
    present (so the request reached the internal service rather than being turned away by the egress
    filter). The cluster-block 403 alone is BLOCKED, not a reach. Pure / signature-driven so the
    verdict is deterministic, not LLM judgement."""
    t = (text or "").lower()
    if not any(tok in t for tok in _INTERNAL_TARGET_SIGNATURES):
        return False, ""
    hit = next((s for s in _INTERNAL_REACH_SIGNATURES if s in t), "")
    if not hit:
        return False, ""
    return True, (
        f"internal SSRF reach confirmed: an internal target was addressed and the server returned "
        f"'{hit.strip()}' (a backend reach signal), not the cluster egress block")


def polyglot_canary(token: str, oob: str = "") -> str:
    """A single value that probes XSS / SQLi / SSTI / traversal / (SSRF via oob) at once."""
    parts = [f"scx{token}", "'\"`", _XSS_MARK, _SSTI_EXPR[0], _SSTI_EXPR[1], _TRAV_MARK]
    if oob:
        parts.append(oob)
    return "".join(parts)


def classify_reflection(token: str, body: str | None, headers: dict | None = None,
                        oob_hits: list | None = None) -> list[str]:
    """Map how the canary surfaced to candidate injection classes (the triage signal).
    Conservative: a class is only proposed on a concrete surfacing, never on a bare 200."""
    out: set[str] = set()
    b = (body or "")
    bl = b.lower()
    if oob_hits:
        out.update({"ssrf", "rce"})
    # XSS: our exact tag came back UNENCODED (not &lt;svg...)
    if _XSS_MARK in b:
        out.add("xss")
    # SQLi: a DB/SQL error fragment surfaced
    if any(sig in bl for sig in _SQL_SIGNATURES):
        out.add("sqli")
    # SSTI: the arithmetic evaluated (result present, literal expression NOT echoed verbatim)
    if _SSTI_RESULT in b and not any(expr in b for expr in _SSTI_EXPR):
        out.add("ssti")
    # traversal: /etc/passwd content or a file-open error surfaced
    if ("root:x:0:0" in b) or (("/etc/passwd" in b) and ("root:" in b)) or any(s in bl for s in _FILE_SIGNATURES):
        out.add("traversal")
    # open-redirect: our canary token echoed into a Location header
    loc = ""
    for k, v in (headers or {}).items():
        if str(k).lower() == "location":
            loc = str(v)
            break
    if loc and (f"scx{token}" in loc or loc.startswith("http") and "scx" in loc):
        out.add("open-redirect")
    return sorted(out)


# class -> catalog objective the sweep records coverage against
CLASS_OBJECTIVE = {
    "sqli": "INFRA-RCE-INJECTION",
    "ssti": "INFRA-RCE-INJECTION",
    "command": "INFRA-RCE-INJECTION",
    "eval": "INFRA-RCE-INJECTION",
    "xss": "CLIENT-XSS-CSRF",
    "traversal": "INFRA-PATH-TRAVERSAL",
    "ssrf": "INFRA-SSRF",
    "open-redirect": "INFRA-SSRF",
    "rce": "INFRA-RCE-INJECTION",
}


# For an orchestration engine, the real injectable surface is WORKFLOW-DEFINITION content, not REST
# params. A playbook primitive's objective maps to an injection class; the task_type to the field
# that carries the payload. These features can't be host-triaged (they need define+run), so they are
# seeded as deep probes directly (location "definition", marked needs_deep at triage).
_OBJ_CLASS = {"INFRA-SSRF": "ssrf", "INFRA-RCE-INJECTION": "eval"}
_TASKTYPE_FIELD = {
    "HTTP": "uri", "INLINE": "expression", "JAVASCRIPT": "expression", "SCRIPT": "expression",
    "EVENT": "sink", "WEBHOOK": "sink", "JSON_JQ": "queryExpression",
}


def definition_field_features(playbook: dict | None) -> list[dict]:
    """Enumerate injectable workflow-definition fields from the profile's feature-exploitation
    playbook (e.g. INLINE.expression -> JS eval, HTTP.uri -> SSRF). The engine's true injection
    surface, complementary to REST-param triage."""
    out = []
    for prim in ((playbook or {}).get("primitives") or []):
        if not isinstance(prim, dict):
            continue
        tt = str(prim.get("task_type") or "")
        cls = _OBJ_CLASS.get(str(prim.get("objective") or ""))
        if not cls:                       # only injection-class primitives are sweep targets
            continue
        field = next((f for k, f in _TASKTYPE_FIELD.items() if k in tt.upper()), "payload")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", tt)[:24].strip("-")
        out.append({
            "id": f"WFDEF:{slug}:{field}",
            "name": f"{tt} task {field}",
            "method": "WFDEF",
            "path": tt,
            "inputs": [{"name": field, "location": "definition"}],
            "source": "playbook",
            "sink_hints": [str(prim.get("how") or "")[:140]],
            "class_hint": cls,
            "prio": 100,                  # engine injection surface — top priority, never truncated
        })
    return out


def _split_endpoint(s: str) -> tuple[str, str]:
    """'POST /api/x' -> ('POST','/api/x'); a bare path -> ('GET', path)."""
    s = str(s or "").strip()
    m = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(.+)$", s, re.I)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return "GET", s


def _path_params(path: str) -> list[dict]:
    """Templated path segments {id}/:id -> path inputs."""
    names = re.findall(r"\{([A-Za-z0-9_]+)\}", path) + re.findall(r"(?:^|/):([A-Za-z0-9_]+)", path)
    return [{"name": n, "location": "path"} for n in dict.fromkeys(names)]


def _norm_path(url_or_path: str) -> str:
    p = str(url_or_path or "")
    p = re.sub(r"^[a-zA-Z]+://[^/]+", "", p)   # strip scheme+host
    p = p.split("?", 1)[0].split("#", 1)[0]
    return p or "/"


def _prio(method: str, path: str, inputs: list, sink_hints: list) -> int:
    blob = (method + " " + path + " " + " ".join(i.get("name", "") for i in inputs)).lower()
    score = 0
    score += sum(1 for cue in _PRIO_CUES if cue in blob)
    score += 2 * len(sink_hints)
    if any(i.get("location") == "body" for i in inputs):
        score += 1
    if method in ("POST", "PUT", "PATCH"):
        score += 1
    return score


def _sink_hints(path: str, app_model: dict) -> list[str]:
    """Trust boundaries / sensitive operations whose text mentions this path's leaf — a hint the
    feature reaches a dangerous sink (heavily prioritised)."""
    leaf = [seg for seg in _norm_path(path).split("/") if seg and "{" not in seg]
    tail = (leaf[-1] if leaf else "").lower()
    hints = []
    for src in ((app_model.get("trust_boundaries") or []) + (app_model.get("sensitive_operations") or [])):
        s = str(src).lower()
        if tail and len(tail) >= 4 and tail in s:
            hints.append(str(src)[:120])
    return hints[:3]


def _merge_inputs(dst: dict, new_inputs: list) -> None:
    seen = {(i.get("name"), i.get("location")) for i in dst["inputs"]}
    for i in new_inputs:
        key = (i.get("name"), i.get("location"))
        if i.get("name") and key not in seen:
            dst["inputs"].append(i)
            seen.add(key)


def build_inventory(app_model: dict | None, surface: dict | None,
                    docs_digest: dict | None, playbook: dict | None = None,
                    max_features: int = 60) -> list[dict]:
    """Merge every available feature/endpoint signal into a deduped, prioritised inventory.
    Defensive: every source is optional (app_model.features is frequently sparse on real targets)."""
    app_model = app_model if isinstance(app_model, dict) else {}
    surface = surface if isinstance(surface, dict) else {}
    docs_digest = docs_digest if isinstance(docs_digest, dict) else {}
    feats: dict[tuple, dict] = {}

    def upsert(method: str, path: str, source: str, inputs: list | None = None, name: str = "") -> dict:
        path = _norm_path(path)
        key = (method.upper(), path)
        if key not in feats:
            feats[key] = {"id": f"{method.upper()}:{path}", "name": name or path,
                          "method": method.upper(), "path": path,
                          "inputs": list(_path_params(path)), "sources": set(), "sink_hints": []}
        f = feats[key]
        f["sources"].add(source)
        if inputs:
            _merge_inputs(f, inputs)
        return f

    # 1) live surface — endpoints, forms (with typed inputs), query params
    for ep in (surface.get("endpoints") or []):
        if isinstance(ep, dict):
            upsert(ep.get("method") or "GET", ep.get("url") or ep.get("path") or "/", "surface")
    for form in (surface.get("forms") or []):
        if isinstance(form, dict):
            inputs = [{"name": i.get("name"), "location": "form"}
                      for i in (form.get("inputs") or []) if isinstance(i, dict) and i.get("name")]
            upsert(form.get("method") or "POST", form.get("action") or "/", "browser", inputs)
    global_params = [p for p in (surface.get("params") or []) if isinstance(p, str)][:25]

    # 2) app model features ("GET /api/x" endpoint strings)
    for feat in (app_model.get("features") or []):
        if not isinstance(feat, dict):
            continue
        nm = feat.get("name") or ""
        for ep in (feat.get("endpoints") or []):
            method, path = _split_endpoint(ep)
            upsert(method, path, "model", name=nm)

    # 3) docs operational recipes / intended workflows — body_sketch field names are real inputs
    for rec in ((docs_digest.get("operational_recipes") or []) + (docs_digest.get("intended_workflows") or [])):
        if not isinstance(rec, dict):
            continue
        for step in (rec.get("steps") or []):
            if not isinstance(step, dict):
                continue
            method = step.get("method") or "POST"
            path = step.get("path") or ""
            if not path:
                continue
            body = step.get("body_sketch") if isinstance(step.get("body_sketch"), dict) else {}
            inputs = [{"name": k, "location": "body"} for k in body.keys()]
            upsert(method, path, "docs", inputs, name=rec.get("name") or "")

    # finalize: attach sink hints + global query params (to GET features lacking inputs) + priority
    out = []
    for f in feats.values():
        f["sink_hints"] = _sink_hints(f["path"], app_model)
        # GET/DELETE: probe common injectable param names (+ any crawl-discovered ones), since the
        # real parameter is rarely exposed black-box. This is what makes e.g. ?q= reachable.
        if f["method"] in ("GET", "DELETE"):
            existing = {i.get("name") for i in f["inputs"]}
            extra = [p for p in (list(COMMON_QUERY_PARAMS) + global_params) if p not in existing]
            f["inputs"] += [{"name": p, "location": "query"} for p in extra[:18]]
        f["source"] = ",".join(sorted(f.pop("sources")))
        f["prio"] = _prio(f["method"], f["path"], f["inputs"], f["sink_hints"])
        out.append(f)
    out.sort(key=lambda x: (-x["prio"], x["id"]))
    rest = out[:max_features]
    # Engine workflow-definition injectable fields (INLINE.expression, HTTP.uri, ...) are the
    # HIGHEST-value injection surface for an orchestration platform — always included, never
    # truncated (they'd otherwise lose the cap race against many REST features).
    deffeats = definition_field_features(playbook)
    have = {f["id"] for f in deffeats}
    return deffeats + [f for f in rest if f["id"] not in have]


def input_bearing(feature: dict) -> bool:
    """A feature worth sweeping: it carries user input, or is a state-changing method (body likely)."""
    if not isinstance(feature, dict):
        return False
    return bool(feature.get("inputs")) or feature.get("method") in ("POST", "PUT", "PATCH", "DELETE")


def sweep_candidates(inventory: list, max_candidates: int = 40) -> list[dict]:
    """Input-bearing features, highest-priority first, bounded."""
    return [f for f in (inventory or []) if input_bearing(f)][:max_candidates]


def feature_coverage(inventory: list | None, probed: list | None,
                     operations: list | None = None) -> dict:
    """Auditable per-feature coverage ledger: of every input-bearing feature, which were triaged,
    which surfaced an injection signal, which were deep-exploited, and which were blocked/untested.
    Makes 'we tested every feature' provable rather than asserted."""
    inv = inventory or []
    cands = sweep_candidates(inv, max_candidates=10_000)
    probed_by = {p.get("feature_id"): p for p in (probed or []) if isinstance(p, dict)}
    deep_ids = {o.get("feature_id") for o in (operations or [])
                if isinstance(o, dict) and o.get("feature_id")}
    per = []
    for f in cands:
        fid = f.get("id")
        rec = probed_by.get(fid, {})
        pstatus = rec.get("status", "untested")
        if fid in deep_ids:
            status = "deep-exploited"
        elif pstatus == "blocked":
            status = "blocked"
        elif pstatus == "triaged":
            status = "signal"          # triaged AND surfaced an injection class
        elif pstatus in ("clean", "out-of-scope", "error"):
            status = "triaged-clean" if pstatus == "clean" else pstatus
        else:
            status = "untested"
        per.append({"id": fid, "method": f.get("method"), "path": f.get("path"),
                    "status": status, "classes": rec.get("classes", [])})
    counts: dict[str, int] = {}
    for p in per:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    triaged = sum(1 for p in per if p["status"] not in ("untested", "blocked", "out-of-scope", "error"))
    return {
        "total_features": len(inv),
        "input_bearing": len(cands),
        "triaged": triaged,
        "with_signal": counts.get("signal", 0) + counts.get("deep-exploited", 0),
        "deep_exploited": counts.get("deep-exploited", 0),
        "blocked": counts.get("blocked", 0),
        "untested": counts.get("untested", 0),
        "triage_rate": (triaged / len(cands)) if cands else 1.0,
        "by_status": counts,
        "per_feature": per,
    }
