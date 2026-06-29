"""Turn a captured browser session (after an interactive SSO login) into a credential the harness
can carry — closing the SSO gap. The harness never automates the IdP dance (Google/Okta/SAML);
instead ``sso_capture.py`` drives a headed browser the operator logs into, and these PURE helpers
distill the result into the same ``{auth_header, auth_token, auth_scheme}`` an ``--id`` already
understands (so it flows through ``auth.acquire_token`` with no workflow change), plus the full
Playwright ``storage_state`` for the browser/UI hand.

Credential precedence (most reliable first):
  bearer-sniffed  — an Authorization / X-Authorization header observed on a real authenticated XHR
                    to an in-scope host. This is exactly what the app sends, so it always works.
  bearer-storage  — a JWT-looking value stashed in localStorage (SPA pattern) when no XHR was seen.
  cookie          — fall back to replaying the session cookie as a ``Cookie:`` header.
"""

from __future__ import annotations

import re

_JWT = re.compile(r"^[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+$")
_AUTH_HEADERS = ("authorization", "x-authorization", "x-api-key", "x-auth-token")
_TOKEN_KEY_HINTS = ("token", "access", "jwt", "id_token", "auth", "bearer")


def looks_like_jwt(value: str) -> bool:
    """A three-segment dot-separated base64url string — the common access-token shape."""
    return bool(_JWT.match(str(value or "").strip()))


def _host_matches(cookie_domain: str, host: str) -> bool:
    d = str(cookie_domain or "").lstrip(".").lower()
    h = str(host or "").lower()
    return bool(d) and bool(h) and (h == d or h.endswith("." + d))


def cookie_header(storage_state: dict | None, host: str) -> str:
    """Build a ``Cookie:`` header value (``k=v; k2=v2``) from the cookies in ``storage_state``
    whose domain covers ``host``. Empty string if none."""
    cookies = (storage_state or {}).get("cookies") or []
    pairs = [f"{c.get('name')}={c.get('value')}" for c in cookies
             if c.get("name") and _host_matches(c.get("domain", ""), host)]
    return "; ".join(pairs)


def token_from_local_storage(storage_state: dict | None, host: str | None = None) -> str | None:
    """Scan ``storage_state`` origins' localStorage for a JWT-looking value under a token-ish key
    (the SPA 'access token in localStorage' pattern). Returns the token or None."""
    for origin in (storage_state or {}).get("origins") or []:
        for item in origin.get("localStorage") or []:
            name, value = str(item.get("name") or "").lower(), str(item.get("value") or "")
            if looks_like_jwt(value) and any(h in name for h in _TOKEN_KEY_HINTS):
                return value
        # Some apps store the JWT bare under any key — accept a lone JWT value as a weaker signal.
        for item in origin.get("localStorage") or []:
            if looks_like_jwt(item.get("value")):
                return str(item["value"])
    return None


def _parse_sniffed(header: str, value: str) -> dict:
    """A captured auth header -> {header, scheme, token}. Splits a ``Bearer <jwt>`` value into
    scheme+token; a raw token (e.g. Orkes X-Authorization) keeps scheme empty."""
    value = str(value or "").strip()
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token", "jwt"):
        return {"header": header, "scheme": parts[0], "token": parts[1]}
    return {"header": header, "scheme": "", "token": value}


def pick_credential(sniffed: list | None, storage_state: dict | None, host: str) -> dict:
    """Choose the strongest credential from a capture. ``sniffed`` is a list of
    ``{header, value, path?, api?}`` auth headers seen on in-scope requests. Returns
    ``{kind, header, scheme, token}`` (``token`` carries the cookie string when kind=='cookie'),
    or ``{kind: 'none'}`` if nothing usable was captured.

    Auth headers observed on an actual ``/api/*`` request (``api: true``) are preferred over
    ones seen elsewhere — on Auth0/OIDC SPAs an early ``/auth`` call carries the *id_token*,
    while the real API access token rides on the product's ``/api`` requests."""
    items = [s for s in (sniffed or [])
             if str(s.get("header") or "").lower() in _AUTH_HEADERS and s.get("value")]
    items.sort(key=lambda s: 0 if s.get("api") else 1)  # API-seen first; stable otherwise
    for s in items:
        return {"kind": "bearer-sniffed", **_parse_sniffed(str(s.get("header")), s["value"])}

    ls = token_from_local_storage(storage_state, host)
    if ls:
        return {"kind": "bearer-storage", "header": "Authorization", "scheme": "Bearer", "token": ls}

    ck = cookie_header(storage_state, host)
    if ck:
        return {"kind": "cookie", "header": "Cookie", "scheme": "", "token": ck}

    return {"kind": "none"}


def build_session_doc(cred: dict, *, label: str, target: str, captured_at: str,
                      storage_state: dict | None, verified: str | None = None) -> dict:
    """The session file written by the capture tool and read by ``--id 'label=session:<file>'``.
    Carries the identity credential (consumed by ``auth.acquire_token`` as ``auth_token``) plus the
    full ``storage_state`` for the browser/CDP hand. ``verified`` (true/false/unknown) records
    whether the captured credential actually authenticated against a protected endpoint."""
    return {
        "label": label,
        "target": target,
        "captured_at": captured_at,
        "credential_kind": cred.get("kind"),
        "auth_header": cred.get("header") or "",
        "auth_scheme": cred.get("scheme") or "",
        "auth_token": cred.get("token") or "",
        "verified": verified or "unknown",
        "storage_state": storage_state or {},
    }
