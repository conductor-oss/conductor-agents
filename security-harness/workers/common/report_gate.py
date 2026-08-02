"""Report quality gate (PLAN_V3 Phase 6.2).

The report is the deliverable, so a shared gate runs over the generated Markdown before PDF
rendering and rejects/repairs four failure classes the prior `sanitize_md`-only step did not check:

  1. **Secret leakage** -- JWTs, AWS keys, private keys, labeled secret literals, long hex/base64.
     These are REDACTED in place (the gate returns cleaned Markdown) and recorded as violations.
  2. **Missing required sections** -- e.g. a deep-mode report must carry Executive Summary,
     Findings Overview, Detailed Findings, and (when tail data exists) the Corner and Neglected
     Feature Coverage section. A missing required section is a violation.
  3. **Malformed Markdown tables** -- a header row with no `|---|` separator renders as literal
     pipes in the PDF.
  4. **PDF-hostile characters** -- Unicode arrows / em-en dashes / smart quotes / emoji that
     `prompts/report.md` forbids because they break the PDF text layer.

`check()` is pure and never raises; the worker wraps it. Redaction is defense-in-depth: the real
fix for secret exposure is the credential broker (Phase 5.1) upstream, but a report must never be
the leak path. Unit-tested (`tests/test_report_gate.py`).
"""

from __future__ import annotations

import re

REDACTION = "[REDACTED]"

REQUIRED_DEEP_SECTIONS = ("Executive Summary", "Findings Overview", "Detailed Findings")

# A secret-ish label: any identifier that ENDS in secret/password/passwd/passphrase/credential/
# token/bearer or ...key (so camelCase suffixes like accessKeySecret, clientSecret, encryptionKey,
# sentry_key, apiKey all match, not just bare `secret`).
_LABEL = (r"[a-z0-9_.]*(?:secret|passwd|passphrase|password|credential|access[_-]?token|"
          r"auth[_-]?token|bearer|[a-z0-9_]*key)")

# Secret-like patterns. Ordered most-specific first. `labeled-*` patterns redact the VALUE group
# only (keeping the label visible); the rest redact the whole match.
_SECRET_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # label = value  /  label: value  (optionally quoted)
    ("labeled-assign", re.compile(r"(?i)\b" + _LABEL + r"\b\s*[:=]\s*['\"]?([A-Za-z0-9/+=._-]{8,})")),
    # label 'value'  /  label "value"  (e.g. `worker secret 'AAb...'`, `encryptionKey 'AI_...'`)
    ("labeled-quoted", re.compile(r"(?i)\b" + _LABEL + r"\b\s*['\"]([^'\"\n]{6,})['\"]")),
    ("long-hex", re.compile(r"\b[A-Fa-f0-9]{40,}\b")),
)

# PDF-hostile characters (report.md mandates plain ASCII): em/en dash, arrows, smart quotes, bullets.
_BAD_CHARS = {
    "—": "--", "–": "-", "→": "->", "←": "<-",
    "‘": "'", "’": "'", "“": "\"", "”": "\"", "•": "-",
}


def redact_secrets(markdown: str) -> tuple[str, list[str]]:
    """Replace secret-like literals with REDACTION. Returns (clean, [pattern names hit])."""
    text = markdown or ""
    hits: list[str] = []
    for name, pat in _SECRET_PATTERNS:
        if pat.search(text):
            hits.append(name)
            if name.startswith("labeled"):
                # redact the captured VALUE (last group), keep the label for readability
                text = pat.sub(lambda m: m.group(0).replace(m.group(m.lastindex), REDACTION), text)
            else:
                text = pat.sub(REDACTION, text)
    return text, hits


def _strip_bad_chars(markdown: str) -> tuple[str, bool]:
    found = any(ch in markdown for ch in _BAD_CHARS)
    for ch, repl in _BAD_CHARS.items():
        markdown = markdown.replace(ch, repl)
    # emoji / other non-ASCII (except common accented text) -> flag but keep
    return markdown, found


def _table_violations(markdown: str) -> list[str]:
    """A Markdown table header (a `| a | b |` line) must be followed by a `|---|---|` separator, or
    it renders as literal pipes. Flag any header row not followed by a separator."""
    lines = (markdown or "").splitlines()
    viol = []
    sep = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
    for i, ln in enumerate(lines):
        if ln.count("|") >= 2 and not sep.match(ln):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            prev = lines[i - 1] if i > 0 else ""
            # a row is fine if the line above or below it is a separator (header or body row)
            if not (sep.match(nxt) or sep.match(prev)):
                # tolerate inline pipes in prose/code: only flag rows that start with '|'
                if ln.lstrip().startswith("|"):
                    viol.append(f"malformed-table:line-{i + 1}")
    return viol


def check(markdown: str, required_sections: tuple | list | None = None,
          tail_expected: bool = False, deep: bool = True) -> dict:
    """Validate + repair a report. Returns {ok, violations[], markdown (redacted+cleaned),
    redactions[]}. ``ok`` is False iff a NON-repairable violation remains (missing section or
    malformed table); secret redaction and bad-char stripping are repairs, recorded but not
    blocking (the returned Markdown is clean)."""
    text = markdown or ""
    violations: list[str] = []
    text, redactions = redact_secrets(text)
    if redactions:
        violations += [f"secret-leak:{r}" for r in redactions]
    text, had_bad = _strip_bad_chars(text)
    if had_bad:
        violations.append("pdf-hostile-chars")

    sections = tuple(required_sections) if required_sections is not None else \
        (REQUIRED_DEEP_SECTIONS if deep else ())
    missing = [s for s in sections if s.lower() not in text.lower()]
    if tail_expected and "corner and neglected feature coverage" not in text.lower():
        missing.append("Corner and Neglected Feature Coverage")
    violations += [f"missing-section:{s}" for s in missing]

    table_viol = _table_violations(text)
    violations += table_viol

    blocking = bool(missing) or bool(table_viol)
    return {"ok": not blocking, "violations": violations, "markdown": text,
            "redactions": redactions}
