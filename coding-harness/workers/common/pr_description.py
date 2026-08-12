"""Canonical, template-aware pull-request description policy.

Every harness-created PR has exactly one Markdown section: ``## Summary``.
Issue-backed workflows may derive that summary from the primary field in a
repository's GitHub issue template; reproduction steps, alternatives,
verification narratives, warnings, and harness metadata are never published.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path


HEADING = "## Summary"
MAX_SUMMARY_CHARS = 500
_TEMPLATE_SUFFIXES = {".md", ".markdown", ".yml", ".yaml"}
_PRIMARY_HINTS = (
    "summary",
    "describe the bug",
    "describe the feature request",
    "the feature, motivation and pitch",
    "description",
)
_PLACEHOLDER_FRAGMENTS = (
    "a clear and concise description",
    "please provide a clear and concise description",
    "add any other context",
)


def issue_template_files(repo: str | Path) -> list[Path]:
    root = Path(repo) / ".github" / "ISSUE_TEMPLATE"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir()
                  if path.is_file() and path.suffix.lower() in _TEMPLATE_SUFFIXES)


def _label(line: str) -> str:
    value = line.strip()
    heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", value)
    if heading:
        return heading.group(1).strip()
    bold = re.match(r"^\*\*(.+?)\*\*\s*$", value)
    if bold:
        return bold.group(1).strip()
    return ""


def _template_labels(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix.lower() in {".yml", ".yaml"}:
        labels = re.findall(r"^\s*label:\s*['\"]?(.+?)['\"]?\s*$", text,
                            flags=re.MULTILINE | re.IGNORECASE)
    else:
        labels = [label for line in text.splitlines() if (label := _label(line))]
    unique: list[str] = []
    for label in labels:
        clean = re.sub(r"\s+", " ", label).strip(" :'\"")
        if clean and clean.casefold() not in {item.casefold() for item in unique}:
            unique.append(clean)
    return unique


def _label_priority(label: str) -> tuple[int, str]:
    normalized = label.casefold()
    for index, hint in enumerate(_PRIMARY_HINTS):
        if hint in normalized:
            return index, normalized
    return len(_PRIMARY_HINTS), normalized


def _extract_labeled_section(text: str, wanted: str) -> str:
    lines = text.splitlines()
    wanted_key = wanted.casefold().strip()
    start = None
    for index, line in enumerate(lines):
        if _label(line).casefold() == wanted_key:
            start = index + 1
            break
    if start is None:
        return ""
    selected: list[str] = []
    for line in lines[start:]:
        if _label(line):
            break
        selected.append(line)
    return "\n".join(selected)


def _first_summary_block(value: object) -> str:
    """Select one prose block, never the later sections of a supplied story."""
    text = str(value or "")
    explicit = _extract_labeled_section(text, "Summary")
    if explicit.strip():
        return explicit
    selected: list[str] = []
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if _label(line):
            if started:
                break
            continue
        if not line:
            if started:
                break
            continue
        selected.append(line)
        started = True
    return "\n".join(selected)


def _compact(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", "", text,
                  flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    pieces: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"[-_*]{3,}", line):
            continue
        label = _label(line)
        if label:
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        if any(fragment in line.casefold() for fragment in _PLACEHOLDER_FRAGMENTS):
            continue
        pieces.append(line)
    summary = re.sub(r"\s+", " ", " ".join(pieces)).strip()
    summary = re.sub(r"^(?:summary|description)\s*:\s*", "", summary,
                     flags=re.IGNORECASE)
    if len(summary) > MAX_SUMMARY_CHARS:
        prefix = summary[:MAX_SUMMARY_CHARS - 1]
        summary = (prefix.rsplit(" ", 1)[0].rstrip() or prefix) + "…"
    return summary


def format_summary(repo: str | Path, summary: object, *, issue_body: object = "") -> dict:
    """Return the only legal PR body plus auditable template-source metadata."""
    templates = issue_template_files(repo)
    selected = ""
    source = "provided_summary"
    if templates and str(issue_body or "").strip():
        labels = sorted(
            {label for path in templates for label in _template_labels(path)},
            key=_label_priority,
        )
        for label in labels:
            candidate = _compact(_extract_labeled_section(str(issue_body), label))
            if candidate:
                selected = candidate
                source = f"issue_template:{label}"
                break
    if not selected:
        selected = _compact(_first_summary_block(summary))
    if not selected:
        selected = "Automated change."
        source = "fallback"
    return {
        "body": f"{HEADING}\n\n{selected}",
        "summary": selected,
        "summarySource": source,
        "issueTemplateFiles": [path.name for path in templates],
    }


def policy_health_report() -> dict:
    """Execute the policy against hostile prose and a template-backed issue."""
    with tempfile.TemporaryDirectory(prefix="conductor-pr-description-") as temp:
        repo = Path(temp)
        templates = repo / ".github" / "ISSUE_TEMPLATE"
        templates.mkdir(parents=True)
        (templates / "bug_report.md").write_text(
            "**Describe the bug**\nA concise description.\n\n"
            "**To Reproduce**\nA long reproduction story.\n",
            encoding="utf-8",
        )
        provided = format_summary(
            repo,
            "## Summary\nKeep this concise.\n\n## Verification\nDo not publish this.",
        )
        templated = format_summary(
            repo,
            "fallback",
            issue_body=("### Describe the bug\nThe scheduler loses a task.\n\n"
                        "### To Reproduce\nWait forever."),
        )

    def valid(result: dict) -> bool:
        body = str(result.get("body") or "")
        summary = str(result.get("summary") or "")
        return (
            body == f"{HEADING}\n\n{summary}"
            and body.count("\n") == 2
            and len(summary) <= MAX_SUMMARY_CHARS
            and not any(marker in body for marker in (
                "## Verification", "## Subtasks", "Closes #", "<!--", "[!WARNING]",
            ))
        )

    healthy = valid(provided) and valid(templated) and templated["summarySource"].startswith(
        "issue_template:"
    )
    return {
        "healthy": healthy,
        "requiredHeading": HEADING,
        "maxSummaryChars": MAX_SUMMARY_CHARS,
        "providedSample": provided,
        "templateSample": templated,
    }
