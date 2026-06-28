"""Authenticated-scan helpers.

Acquire an auth credential once (a directly-supplied bearer token, or a
Conductor-style keyId/keySecret -> /api/token exchange) and turn it into the header
that every worker applies, so recon, crawl, API discovery, exploitation, and the
active checks all run AUTHENTICATED against the API surface.

Auth scheme is NOT assumed. Different platforms present the token differently:

  * Orkes Conductor uses ``X-Authorization: <raw-jwt>``  (NO "Bearer" prefix).
  * Many other APIs use      ``Authorization: Bearer <jwt>``.
  * Others use a custom header (``X-API-Key`` etc.).

Guessing wrong silently makes every request a 401 and the whole assessment runs
EFFECTIVELY UNAUTHENTICATED (a false "nothing found"). So when a base URL is known
we **probe** the candidate schemes against the live target and pick the one that
actually authenticates -- and report whether authentication was verified, so a run
can refuse to draw conclusions from an unauthenticated session.

The resolved secret value is never logged.
"""

import logging

import requests
import urllib3

from common import scope as scope_mod

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

# Generic API-mount conventions used ONLY to find SOME endpoint that distinguishes
# authenticated from anonymous. These are NOT app-specific resource paths (no Conductor
# knowledge): the signal is purely the WITH-credential vs WITHOUT-credential differential
# on whatever path turns out to be access-controlled. An operator/profile can supply
# `auth_probe_paths` for a known protected endpoint, and the harness re-verifies against
# the DISCOVERED surface after recon (see normalize_target / surface). The empty string
# probes the base URL itself.
_GENERIC_PROBE_PATHS = ("", "/api", "/api/v1", "/v1", "/me", "/account", "/admin")
_AUTH_FAIL = {401, 403}


def acquire_token(inp) -> str | None:
    """Return the raw token: a directly-supplied ``auth_token``, or the result of a
    key/secret -> ``token_url`` exchange. ``None`` if none configured/obtainable.

    The exchange SHAPE is data-driven (not hard-wired to any platform): an optional
    ``token_exchange`` spec ``{body, token_field}`` — typically supplied by a target
    profile — templates the request body (``$KEY``/``$SECRET`` substituted) and names
    the response field holding the token. Absent a spec, it defaults to the common
    ``{keyId, keySecret}`` -> ``token`` shape (overridable; see profiles/)."""
    inp = inp or {}
    token = inp.get("auth_token")
    if token:
        return str(token)
    key, secret, turl = inp.get("auth_key"), inp.get("auth_secret"), inp.get("token_url")
    if key and secret and turl:
        ex = inp.get("token_exchange") if isinstance(inp.get("token_exchange"), dict) else {}
        body_tmpl = ex.get("body") or {"keyId": "$KEY", "keySecret": "$SECRET"}
        token_field = ex.get("token_field") or "token"
        body = {k: (str(v).replace("$KEY", str(key)).replace("$SECRET", str(secret))
                    if isinstance(v, str) else v)
                for k, v in body_tmpl.items()}
        try:
            r = requests.post(turl, json=body, timeout=15, verify=False)
            r.raise_for_status()
            tok = (r.json() or {}).get(token_field)
            if tok:
                log.info("acquired auth token via key/secret exchange (%s)", turl)
                return str(tok)
            log.warning("token exchange returned no '%s' (HTTP %s)", token_field, r.status_code)
        except Exception as exc:
            log.warning("token exchange failed: %s", exc)
    return None


def _present(token: str, header: str, scheme: str | None) -> dict:
    """Compose a {header, value} for a raw token under a given header/scheme."""
    if scheme is None or scheme == "":
        scheme = "Bearer" if header.lower() == "authorization" else ""
    value = f"{scheme} {token}".strip() if scheme else str(token)
    return {"header": header, "value": value}


def resolve_auth(inp, base_url: str | None = None, scope: dict | None = None,
                 probe_paths: list | None = None):
    """Return {'header': name, 'value': value} to send on requests, or {} if no auth.

    Back-compatible: with no ``base_url`` and no explicit header, defaults to
    ``Authorization: Bearer <token>`` (legacy behavior). With a ``base_url`` and no
    explicit header, the candidate schemes are PROBED against the target and the one
    that actually authenticates is chosen (auto-detect); the result carries
    ``verified``/``note`` keys describing the outcome."""
    inp = inp or {}
    tok = acquire_token(inp)
    if not tok:
        return {}

    if probe_paths is None:
        probe_paths = inp.get("auth_probe_paths") if isinstance(inp.get("auth_probe_paths"), list) else []

    explicit_header = inp.get("auth_header")
    if explicit_header:
        # Caller pinned the header/scheme: honor it (verify if we can).
        chosen = _present(tok, explicit_header, inp.get("auth_scheme"))
        if base_url:
            v = _probe(base_url, chosen, scope, probe_paths)
            chosen = {**chosen, "verified": _v(v),
                      "note": "explicit header" + ("" if v is not False else " (probe did NOT authenticate)")}
        return chosen

    if not base_url:
        # Legacy path (no target to probe against): default to Authorization: Bearer.
        return _present(tok, "Authorization", inp.get("auth_scheme"))

    # Auto-detect: try known header SCHEMES (generic — trying multiple auth header
    # conventions is standard scanner behavior, not app-specific), pick one that
    # demonstrably authenticates against the target.
    candidates = [
        _present(tok, "Authorization", "Bearer"),     # most common generally
        _present(tok, "X-Authorization", ""),         # raw-token header (e.g. Conductor)
    ]
    chosen, verified, note = _pick(base_url, candidates, scope, probe_paths)
    return {**chosen, "verified": verified, "note": note}


def _v(b):
    return "true" if b is True else ("false" if b is False else "unknown")


def _probe(base_url: str, auth: dict, scope: dict | None, probe_paths: list):
    """Tri-state: True  = credential authenticates (a path is 401/403 anon but not with it);
    False = a protected path stayed 401/403 even WITH the credential (scheme rejected);
    None  = couldn't determine (no access-controlled path observed among the probes)."""
    sess = requests.Session()
    sess.verify = False
    saw_protected = False
    for path in (list(probe_paths) + list(_GENERIC_PROBE_PATHS)):
        p = path if (path == "" or path.startswith("/")) else "/" + path
        url = base_url.rstrip("/") + p
        if scope and not scope_mod.in_scope(url, scope):
            continue
        try:
            anon = sess.get(url, timeout=8, allow_redirects=False)
            if anon.status_code not in _AUTH_FAIL:
                continue  # not access-controlled here; can't judge auth from this path
            saw_protected = True
            authd = sess.get(url, timeout=8, allow_redirects=False,
                             headers={auth["header"]: auth["value"]})
            # The credential is ACCEPTED (authenticated) if the auth layer no longer rejects it:
            # 401 means "missing/invalid credential"; 403 means "authenticated but not authorized"
            # (the token WAS accepted — still proves auth). Any non-401, sub-500 response on a path
            # that was access-controlled anon (200/2xx/3xx/403/404) confirms authentication. (5xx is
            # ambiguous — e.g. a control plane returning 500 for a non-existent engine route — skip.)
            if authd.status_code != 401 and authd.status_code < 500:
                return True
        except requests.RequestException:
            continue
    return False if saw_protected else None


def _pick(base_url: str, candidates: list[dict], scope: dict | None, probe_paths: list):
    """Return (chosen_auth, verified_str, note). verified_str ∈ {true,false,unknown}.
    Picks the first candidate that authenticates; if a protected path rejected every
    scheme -> false; if no protected path was observable at all -> unknown (don't
    mislabel a generic app whose protected endpoints only appear after surface)."""
    saw_protected = False
    for auth in candidates:
        r = _probe(base_url, auth, scope, probe_paths)
        if r is True:
            return auth, "true", f"verified via {auth['header']}"
        if r is False:
            saw_protected = True
    if saw_protected:
        return candidates[0], "false", ("a protected endpoint rejected every auth scheme tried "
                                        "-- the session is effectively UNAUTHENTICATED")
    return candidates[0], "unknown", ("could not confirm authentication pre-surface (no "
                                      "access-controlled endpoint observed yet); will rely on "
                                      "observed behavior during testing")


def auth_headers(auth):
    """Resolved-auth dict -> headers dict for requests / Playwright contexts."""
    if isinstance(auth, dict) and auth.get("header") and auth.get("value"):
        return {auth["header"]: auth["value"]}
    return {}


def resolve_identities(inp, base_url: str | None = None, scope: dict | None = None,
                       probe_paths: list | None = None):
    """Resolve identity specs into {label: {header, value, ...}} for multi-identity
    (BOLA / privilege-escalation) testing, always including an anonymous identity. Each
    spec is a dict with a ``label`` plus the same auth fields resolve_auth understands.
    When ``base_url`` is given, each identity's scheme is auto-detected/verified.
    ``probe_paths`` (from a target profile) are applied to every identity's verification."""
    out = {"anon": {}}
    for ident in (inp or {}).get("identities") or []:
        if not isinstance(ident, dict):
            continue
        label = ident.get("label") or f"id{len(out)}"
        resolved = resolve_auth(ident, base_url=base_url, scope=scope, probe_paths=probe_paths)
        # carry an explicit tenant tag (if the operator supplied one) for adequacy analysis
        if isinstance(resolved, dict) and ident.get("tenant"):
            resolved["tenant"] = ident["tenant"]
        out[label] = resolved
    return out
