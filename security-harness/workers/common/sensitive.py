"""Sensitive-data detection + redaction.

Two jobs, one source of truth:
  - scan(text)   -> what sensitive data a response actually contains. This is the
                    IMPACT signal for data-exposure findings: "the response leaked
                    5 emails + an AWS key + a JWT" is proof of a leak, not just a 200.
  - redact(text) -> the same data masked, so the scanner's OWN artifacts
                    (findings.json, reports, evidence chains) never persist customer
                    PII / secrets we observed in a target response.

Patterns are deliberately high-precision (favor false negatives over flooding every
JSON blob with "PII!"). `scan` returns counts + a FEW redacted samples (never raw
values) so a finding can cite "3 emails, 1 AWS key" without us storing them.
"""

from __future__ import annotations

import re

# name -> (compiled regex, redactor producing a masked placeholder)
_PATTERNS = {
    # JWT (header.payload.signature) — common bearer/token leak
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws_secret_key": re.compile(r"(?i)aws.{0,20}?(?:secret|sk).{0,5}?['\"\s:=]+([A-Za-z0-9/+]{40})\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[0-9A-Za-z_]{20,}\b"),
    "stripe_key": re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    "bearer": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "credit_card": re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12})\b"),
    "ssn_us": re.compile(r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b"),
    "us_phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?[2-9][0-9]{2}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}\b"),
    "iban": re.compile(r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{12,30}\b"),
    "connection_string": re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|jdbc:[a-z]+)://[^\s\"'<>]*:[^\s\"'<>@]+@[^\s\"'<>]+"),
    "imds": re.compile(r"\b169\.254\.169\.254\b"),
    "private_ip": re.compile(r"\b(?:10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b"),
}

# Emails/phones are PII but very common; secrets are higher-signal. Keep both but
# label so callers/triage can weight (a leaked AWS key >> a support email address).
_SECRET_TYPES = {"jwt", "aws_access_key", "aws_secret_key", "private_key", "google_api_key",
                 "slack_token", "github_token", "stripe_key", "bearer", "connection_string"}
_PII_TYPES = {"email", "credit_card", "ssn_us", "us_phone"}

# Data-CLASS taxonomy (E2/DLP): a type can belong to several classes (a card is both
# PCI and financial). Classes: secret | pii | pci | phi | financial | infra.
_CLASS_OF = {
    "jwt": ["secret"], "aws_access_key": ["secret"], "aws_secret_key": ["secret"],
    "private_key": ["secret"], "google_api_key": ["secret"], "slack_token": ["secret"],
    "github_token": ["secret"], "stripe_key": ["secret", "financial"], "bearer": ["secret"],
    "connection_string": ["secret", "infra"],
    "email": ["pii"], "us_phone": ["pii"], "ssn_us": ["pii"],
    "credit_card": ["pci", "financial"], "iban": ["financial"],
    "imds": ["infra"], "private_ip": ["infra"],
}

MAX_SAMPLES = 3

# Infra-class types (RFC1918 IPs, IMDS address) are returned BY DESIGN by infrastructure/
# control-plane APIs (VPC CIDRs, metadata IPs). They are NOT exfiltratable secrets/PII, so they
# are bucketed separately and excluded from the "bulk access to real secrets/PII" halt — counting
# a cluster object's VPC CIDR as leaked PII falsely halts a read-only assessment.
_INFRA_ONLY = {"private_ip", "imds"}


def _mask(s: str) -> str:
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-2:]}[{len(s)}c]"


def _luhn_ok(num: str) -> bool:
    """Luhn checksum — distinguishes a real card number from any 16-digit value (cluster IDs,
    account ids, vpc ids) that merely matches the card regex. Cuts the dominant credit_card FP."""
    digits = [int(c) for c in num if c.isdigit()]
    if len(digits) < 13:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scan(text) -> dict:
    """Return {found, secrets:{type:count}, pii:{type:count}, infra:{type:count}, samples:[masked]}.
    Samples are masked — raw values are never returned. ``credit_card`` matches are Luhn-validated;
    infra-class hits (private_ip/imds) are bucketed apart from PII (and excluded from the bulk-access
    halt). ``found`` reflects real secrets/PII only — an infra-only response is not 'sensitive'."""
    if not isinstance(text, str) or not text:
        return {"found": False, "secrets": {}, "pii": {}, "infra": {}, "samples": []}
    secrets, pii, infra, samples = {}, {}, {}, []
    for name, rx in _PATTERNS.items():
        matches = rx.findall(text)
        if name == "credit_card":
            matches = [m for m in matches if _luhn_ok(m if isinstance(m, str) else (m[0] if m else ""))]
        if not matches:
            continue
        n = len(matches)
        bucket = secrets if name in _SECRET_TYPES else (infra if name in _INFRA_ONLY else pii)
        bucket[name] = n
        for m in matches[:MAX_SAMPLES]:
            val = m if isinstance(m, str) else (m[0] if m else "")
            if val and len(samples) < MAX_SAMPLES * 2:
                samples.append({"type": name, "masked": _mask(val)})
    return {"found": bool(secrets or pii), "secrets": secrets, "pii": pii, "infra": infra, "samples": samples}


def classify(text) -> dict:
    """Data-classification view of scan() for DLP (E2): bucket detected values into data
    CLASSES (secret/pii/pci/phi/financial/infra) with counts, plus the raw type counts and
    masked samples. ``classes`` is what a leak/exfil finding cites ("3 pii, 1 secret")."""
    base = scan(text)
    classes: dict[str, int] = {}
    types = {**base["secrets"], **base["pii"], **base.get("infra", {})}
    for t, n in types.items():
        for c in _CLASS_OF.get(t, []):
            classes[c] = classes.get(c, 0) + n
    return {"found": base["found"], "classes": classes, "types": types, "samples": base["samples"]}


def redact(text, limit: int | None = None) -> str:
    """Mask every detected secret/PII match so the value never lands in our artifacts.
    Optionally truncate to `limit` chars first (matches body-excerpt bounds)."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    if limit is not None:
        text = text[:limit]
    for name, rx in _PATTERNS.items():
        tag = f"[REDACTED:{name}]"
        text = rx.sub(lambda _m, t=tag: t, text)
    return text
