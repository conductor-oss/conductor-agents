"""Analyst-ready reproduction steps for a confirmed finding.

The swarm confirms a finding with a deterministic oracle but persists only a one-line evidence
note. This turns the structured facts we DO have — the endpoint (from the title/signature), the
identities used, the oracle class, the observed marker, and the agent's own recorded technique
(`lesson`) — into a report section an analyst can act on: numbered **Steps to reproduce**, a runnable
**curl** skeleton, **what to observe**, and **how to verify a fix**.

It never invents an endpoint or marker it was not given; where the exact request body was not
captured it says so and points to the recorded technique. Pure functions, no I/O.
"""
from __future__ import annotations

import re

_METHOD_PATH = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_\-/{}.:]+)")
_BARE = re.compile(r"(/(?:api|webhook)/[A-Za-z0-9_\-/{}.:]+)")


def _endpoint(f: dict) -> tuple[str, str]:
    for src in (f.get("title", ""), f.get("signature", "")):
        m = _METHOD_PATH.search(src or "")
        if m:
            return m.group(1).upper(), m.group(2).rstrip(".,);:")
    for src in (f.get("title", ""), f.get("signature", "")):
        m = _BARE.search((src or "").replace("|", " "))
        if m:
            return "", m.group(1).rstrip(".,);:")
    return "", ""


def endpoint_key(method, path) -> str:
    """Normalized 'METHOD /path' key for joining a captured request to a finding — host stripped and
    id-ish segments collapsed to {id} so '/api/webhook/abc123' and '/api/webhook/{id}' match."""
    path = str(path or "")
    m = re.match(r"https?://[^/]+(/.*)", path)
    if m:
        path = m.group(1)
    segs = [s for s in path.split("?")[0].split("/") if s]
    norm = []
    for s in segs:
        # Collapse clear identifiers only (placeholders, pure numbers, UUIDs, long hex) — conservative
        # so distinct real segments like 'secrets-v2' are never merged.
        if (re.fullmatch(r"\{[^}]+\}", s) or re.fullmatch(r"\d+", s)
                or re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{12,}", s) or re.fullmatch(r"[0-9a-fA-F]{16,}", s)):
            norm.append("{id}")
        else:
            norm.append(s.lower())
    p = "/" + "/".join(norm)
    return ((str(method or "").upper() + " " + p).strip()) if p != "/" else ""


def _identities(f: dict) -> list[str]:
    for part in reversed((f.get("signature", "") or "").split("|")):
        if "," in part and re.fullmatch(r"[A-Za-z0-9_,\-]+", part):
            return [x for x in part.split(",") if x]
    ids = f.get("identities")
    return [str(i) for i in ids] if isinstance(ids, list) else []


def _classify(blob: str) -> str:
    if "xxe" in blob or "external entity" in blob or "import-bpm" in blob or "bpmn" in blob:
        return "xxe"
    if ("ssti" in blob or "nashorn" in blob or "graaljs" in blob or "prompt test" in blob
            or "prompt template" in blob or "expression eval" in blob or "script task" in blob
            or "inline" in blob or "/prompts/test" in blob or " rce" in blob or "code injection" in blob
            or "injection" in blob or "eval(" in blob):
        return "injection"
    if ("self-grant" in blob or "self grant" in blob or "privilege escalation" in blob or "privesc" in blob
            or "escalat" in blob or "self-promot" in blob or ("role" in blob and "grant" in blob)):
        return "privesc"
    if "cross_tenant_write" in blob or "poison" in blob or "overwrite" in blob or "write-persist" in blob or "tamper" in blob:
        return "xtenant_write"
    if "mask" in blob or "plaintext" in blob or "credential" in blob or "api_key" in blob or "api key" in blob or "secret" in blob or "token" in blob:
        return "secret"
    if "ssrf" in blob or "server-side request" in blob:
        return "ssrf"
    if "cross-identity" in blob or "idor" in blob or "bola" in blob or "cross-tenant" in blob or "cross-user" in blob or "disclos" in blob:
        return "bola"
    if ("auth" in blob and "bypass" in blob) or "unauth" in blob or "anonymous" in blob:
        return "auth_bypass"
    return "generic"


def oracle_class(f: dict) -> str:
    # Classify from the vuln's identity (title/objective/owasp) FIRST; the one-line evidence note only
    # records which oracle fired (often a generic reachability oracle) and would mislabel otherwise.
    primary = " ".join(str(f.get(k, "")) for k in ("title", "objective_id", "owasp")).lower()
    c = _classify(primary)
    if c != "generic":
        return c
    return _classify(" ".join(str(f.get(k, "")) for k in ("evidence", "lesson")).lower())


# Per-oracle-class reproduction recipe: {atk}/{vic} are identity labels, {disp} the "METHOD /path".
_RECIPES = {
    "bola": {
        "steps": [
            "As **{vic}** (tenant B), create or note a resource owned only by {vic} and record a unique marker in it.",
            "As **{atk}** (tenant A — a different, lower-privileged identity), call `{disp}` for {vic}'s object id.",
            "Confirm {atk}'s response contains {vic}'s data / the marker, i.e. the object-level authorization check is missing or checks the wrong permission.",
        ],
        "observe": "{atk} receives data that belongs to {vic} (the recorded marker is present) instead of 403/empty.",
        "fix": "Enforce per-object ownership/permission on this endpoint (check the resource's tenant/owner against the caller, not a generic or wrong permission).",
        "verify": "Re-run step 2 as {atk}: it must now return 403/404/empty and the marker must be absent.",
    },
    "xtenant_write": {
        "steps": [
            "As **{atk}** (tenant A), send `{disp}` to create/modify a shared or {vic}-owned resource, embedding a unique marker.",
            "As **{vic}** (tenant B), read that resource back.",
            "Confirm {vic} sees {atk}'s write (the marker persisted across the tenant boundary) — the write is not tenant-scoped.",
        ],
        "observe": "{atk}'s write is visible to {vic} (shared/global store), proving cross-tenant write persistence.",
        "fix": "Scope writes to the caller's tenant/namespace; reject or isolate writes that target another tenant's/global keys.",
        "verify": "Repeat: {atk}'s write must land only in {atk}'s own view; {vic} must not see the marker.",
    },
    "xtenant_read": {
        "steps": [
            "As **{vic}** (tenant B), ensure a resource with a unique marker exists in {vic}'s namespace.",
            "As **{atk}** (tenant A), call `{disp}`.",
            "Confirm {atk}'s response includes {vic}'s resource/marker — the listing/read is global rather than namespace-scoped.",
        ],
        "observe": "{atk} reads {vic}'s data (marker present); the store is sandbox-global, not tenant-scoped.",
        "fix": "Scope this read to the caller's tenant/namespace.",
        "verify": "Re-run as {atk}: {vic}'s marker must no longer appear.",
    },
    "ssrf": {
        "steps": [
            "As **{atk}**, configure the attacker-controlled destination this feature fetches (e.g. an integration/provider endpoint, HTTP-task URI, remote-service/gateway discovery URL, or webhook/callback) — point it at an internal address or your OOB collaborator.",
            "Trigger the feature so the server issues the outbound request to `{disp}`.",
            "Confirm the server reaches the internal target (response reflected in-band / error leak) or your OOB collaborator records the callback.",
        ],
        "observe": "The internal/OOB marker is reached only via the request under test (differential), proving server-side request forgery.",
        "fix": "Validate/allow-list outbound destinations; block RFC1918/link-local/169.254.169.254 metadata; disable redirects; strip reflected upstream bodies.",
        "verify": "Repeat with an internal/metadata URL: the request must be refused and no internal marker/OOB hit occurs.",
    },
    "injection": {
        "steps": [
            "As **{atk}**, submit the payload to `{disp}` using the technique recorded below (expression/script/template/prompt sink).",
            "Use a deterministic probe (arithmetic/time-blind/marker) so success is unambiguous.",
            "Confirm the server evaluates attacker input (the probe's expected effect occurs) — e.g. a secret is resolved into output, an expression is evaluated, or code executes.",
        ],
        "observe": "The injected expression/template/script is evaluated by the server (probe effect observed).",
        "fix": "Treat the field as untrusted data; disable server-side evaluation or sandbox it; never resolve ${secrets.*}/${workflow.*} from caller-supplied templates.",
        "verify": "Re-run the probe after the fix: the expression must be returned literally / rejected, not evaluated.",
    },
    "xxe": {
        "steps": [
            "As **{atk}**, craft an XML document with an external entity (OOB SYSTEM entity to your collaborator, or a file:// read).",
            "Submit it to `{disp}` (the XML/BPMN import parser).",
            "Confirm the parser resolves the external entity — your OOB collaborator receives a callback or file contents appear.",
        ],
        "observe": "The importer resolves external entities (OOB callback / file read), proving XXE.",
        "fix": "Disable external entities and DTDs in the XML parser (use a hardened/defused parser).",
        "verify": "Re-submit the XXE payload: no OOB callback / no file read occurs.",
    },
    "privesc": {
        "steps": [
            "As **{atk}** (low-privileged), call `{disp}` to grant yourself (or an app you create) an elevated role/authorization.",
            "Obtain a token/key for the elevated principal.",
            "Confirm you can now perform an admin-only action you could not before — the access-control model is self-escalatable.",
        ],
        "observe": "{atk} self-grants elevated authority and then performs a previously-denied privileged action.",
        "fix": "Require an existing admin to grant roles/authorizations; forbid self-granting; re-check authority on the privileged action.",
        "verify": "Repeat as {atk}: the self-grant must be rejected and the privileged action denied.",
    },
    "secret": {
        "steps": [
            "As **{atk}**, call `{disp}`.",
            "Inspect the response body (and execution output/logs where the feature echoes values).",
            "Confirm a secret/credential/API key that {atk} should not see appears in cleartext (not masked).",
        ],
        "observe": "A plaintext secret/credential/API key is returned to {atk} (masking invariant violated or cross-tenant store).",
        "fix": "Mask secret values in responses/logs; scope secret reads to the owner; never return provider credentials in list/get bodies.",
        "verify": "Re-run as {atk}: secret values must be masked/absent.",
    },
    "auth_bypass": {
        "steps": [
            "As an **anonymous/unauthenticated** caller (no token) — or {atk} where the legitimate path denies access — call `{disp}`.",
            "Compare against the legitimate authenticated/authorized path.",
            "Confirm the action succeeds without the required authentication/authorization.",
        ],
        "observe": "The endpoint grants access the legitimate path denies (anon/low-priv success).",
        "fix": "Require authentication/authorization on this route; align its permission check with equivalent protected routes.",
        "verify": "Re-run anonymously/low-priv: the request must return 401/403.",
    },
    "generic": {
        "steps": [
            "As **{atk}**, reproduce the condition described in the finding against `{disp}` using the technique recorded below.",
            "Observe the server behaviour the oracle checked.",
            "Confirm the described security property is violated.",
        ],
        "observe": "The behaviour the oracle verified (see Evidence) is reproduced.",
        "fix": "Remediate per the finding's remediation guidance.",
        "verify": "Re-run the steps after the fix: the oracle condition must no longer hold.",
    },
}


def _curl(method: str, path: str) -> str:
    verb = method or "GET"
    base = '"$TARGET' + path + '"'
    head = '-H "X-Authorization: $ATTACKER_JWT"'
    if verb in ("POST", "PUT", "PATCH", "DELETE"):
        return f"curl -sS -X {verb} {head} -H 'Content-Type: application/json' \\\n     -d '<request body — see technique>' {base}"
    return f"curl -sS {head} {base}"


def _curl_exact(req: dict) -> str:
    """Render the exact confirming request the verifier captured into a runnable curl."""
    import json as _j
    verb = (req.get("method") or "GET").upper()
    url = str(req.get("url") or req.get("path") or "")
    target = url if url.startswith("http") else ('"$TARGET' + url + '"')
    if not url.startswith("http"):
        pass  # already quoted above
    else:
        target = '"' + url + '"'
    ident = req.get("identity") or "attacker"
    head = f'-H "X-Authorization: $JWT_{ident}"'
    body = req.get("body")
    if isinstance(body, (dict, list)):
        body = _j.dumps(body)
    lines = [f"curl -sS -X {verb} {head}"]
    if verb in ("POST", "PUT", "PATCH", "DELETE") or body:
        lines[0] += " -H 'Content-Type: application/json'"
        if body:
            lines.append(f"     -d {_shq(str(body))}")
    lines.append("     " + target)
    return " \\\n".join(lines)


def _shq(s: str) -> str:
    """Single-quote a string safely for a shell command."""
    return "'" + s.replace("'", "'\\''") + "'"


def build(f: dict) -> dict:
    """Return a structured reproduction block for a confirmed finding. When the verifier captured the
    exact confirming request (``reproduction_request``), the curl is that exact request; otherwise a
    class-correct skeleton keyed on the endpoint."""
    method, path = _endpoint(f)
    req = f.get("reproduction_request") if isinstance(f.get("reproduction_request"), dict) else None
    exact = bool(req and (req.get("url") or req.get("path")))
    if exact:
        method = (req.get("method") or method or "GET").upper()
        path = str(req.get("url") or req.get("path") or path)
    disp = (method + " " + path).strip() or "the affected endpoint"
    ids = _identities(f)
    if exact and req.get("identity"):
        ids = [req["identity"]] + [i for i in ids if i != req["identity"]]
    atk = ids[0] if ids else "attacker"
    vic = ids[1] if len(ids) > 1 else "victim"
    cls = oracle_class(f)
    rec = _RECIPES.get(cls, _RECIPES["generic"])
    fmt = lambda s: s.format(atk=atk, vic=vic, disp=disp)  # noqa: E731
    steps = [fmt(s) for s in rec["steps"]]
    curl = _curl_exact(req) if exact else (_curl(method, path) if path and not path.startswith("http") else "")
    return {
        "oracle_class": cls,
        "endpoint": disp,
        "identities": ids,
        "steps": steps,
        "observe": fmt(rec["observe"]),
        "fix": rec["fix"],
        "verify": fmt(rec["verify"]),
        "curl": curl,
        "exact": exact,
        "technique": (f.get("lesson") or f.get("evidence") or "").strip(),
    }


def markdown(f: dict) -> str:
    """Render the reproduction block as report Markdown (used by scribe + the report template)."""
    r = build(f)
    out = ["**Steps to reproduce** (identities: "
           + (", ".join(r["identities"]) if r["identities"] else "attacker, victim")
           + f" · oracle: {r['oracle_class']}):", ""]
    for i, s in enumerate(r["steps"], 1):
        out.append(f"{i}. {s}")
    out.append("")
    tech = r["technique"]
    # Keep a genuine recorded technique, but drop the generic oracle boilerplate ("verified …"),
    # which is often the cross-cutting reachability oracle and can mismatch the finding's class.
    if tech and not tech.lower().startswith("verified "):
        out.append(f"> Technique: {tech}")
        out.append("")
    if r["curl"] and r.get("exact"):
        out += ["Exact confirming request (captured by the verifier — set `$TARGET` and the identity's `$JWT_*` token):",
                "```bash", r["curl"], "```", ""]
    elif r["curl"]:
        out += ["Request skeleton (set `$TARGET` and `$ATTACKER_JWT`; fill the body from the technique):",
                "```bash", r["curl"], "```", ""]
    out.append(f"**Validate:** {r['observe']}")
    out.append("")
    out.append(f"**Fix:** {r['fix']}")
    out.append("")
    out.append(f"**Verify the fix:** {r['verify']}")
    return "\n".join(out)
