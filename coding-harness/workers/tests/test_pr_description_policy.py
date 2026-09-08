from __future__ import annotations

import json
from pathlib import Path

from common import pr_description


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_canonical_body_is_one_bounded_summary_section(tmp_path: Path):
    result = pr_description.format_summary(
        tmp_path,
        "## Background\nA long explanation.\n\n## Verification\nNever publish this section.",
    )

    assert result["body"] == "## Summary\n\nA long explanation."
    assert result["body"].count("## ") == 1
    assert "\n" not in result["summary"]
    assert len(result["summary"]) <= pr_description.MAX_SUMMARY_CHARS


def test_markdown_issue_template_primary_field_supplies_summary(tmp_path: Path):
    templates = tmp_path / ".github" / "ISSUE_TEMPLATE"
    templates.mkdir(parents=True)
    (templates / "bug_report.md").write_text(
        "**Describe the bug**\nThe scheduler loses a task.\n\n"
        "**To Reproduce**\nA very long story that must not be copied.\n",
        encoding="utf-8",
    )
    issue = "### Describe the bug\nThe scheduler loses a task.\n\n### To Reproduce\n1. Wait forever."

    result = pr_description.format_summary(tmp_path, "fallback", issue_body=issue)

    assert result["body"] == "## Summary\n\nThe scheduler loses a task."
    assert result["summarySource"] == "issue_template:Describe the bug"
    assert result["issueTemplateFiles"] == ["bug_report.md"]


def test_yaml_issue_form_primary_field_supplies_summary(tmp_path: Path):
    templates = tmp_path / ".github" / "ISSUE_TEMPLATE"
    templates.mkdir(parents=True)
    (templates / "feature.yml").write_text(
        "body:\n  - type: textarea\n    attributes:\n"
        "      label: The feature, motivation and pitch\n"
        "  - type: textarea\n    attributes:\n      label: Alternatives\n",
        encoding="utf-8",
    )
    issue = ("### The feature, motivation and pitch\nAdd durable signals.\n\n"
             "### Alternatives\nDo nothing.")

    result = pr_description.format_summary(tmp_path, "fallback", issue_body=issue)

    assert result["body"] == "## Summary\n\nAdd durable signals."
    assert "Alternatives" not in result["body"]


def test_every_pr_producer_supplies_an_explicit_summary():
    producers = []
    for path in WORKFLOWS.glob("*.json"):
        workflow = json.loads(path.read_text(encoding="utf-8"))
        producers.extend(node for node in _walk(workflow)
                         if node.get("type") == "SIMPLE" and node.get("name") == "pr_create")

    assert len(producers) == 6
    assert all("summary" in node.get("inputParameters", {}) for node in producers)


def test_executable_policy_health_report_proves_both_sources():
    report = pr_description.policy_health_report()

    assert report["healthy"] is True
    assert report["providedSample"]["body"] == "## Summary\n\nKeep this concise."
    assert report["templateSample"]["body"] == "## Summary\n\nThe scheduler loses a task."


def test_issue_link_survives_compaction_and_is_not_a_second_section(tmp_path):
    """`Closes #n` must be appended by the policy, not glued on by the caller.

    Compaction keeps only the first prose block, so an issue link concatenated
    onto the body upstream is silently dropped the moment the body has real
    prose in front of it -- which is how a campaign PR ended up with the link
    as its entire summary while another lost the link altogether.
    """
    from common import pr_description

    linked = pr_description.format_summary(
        tmp_path, "Remove workflow introspection from core.", closes=4001)
    assert linked["body"] == (
        "## Summary\n\nRemove workflow introspection from core.\n\nCloses #4001")
    assert linked["summary"] == "Remove workflow introspection from core."
    assert linked["closesIssue"] == 4001
    assert linked["body"].count("## ") == 1

    # No issue: byte-identical to the previous behaviour.
    plain = pr_description.format_summary(tmp_path, "Remove workflow introspection from core.")
    assert plain["body"] == "## Summary\n\nRemove workflow introspection from core."
    assert plain["closesIssue"] == 0

    # Non-numeric, zero and negative references never emit a link.
    for value in ("", "abc", 0, -3, None):
        assert pr_description.format_summary(tmp_path, "Body", closes=value)["closesIssue"] == 0

    # A string number (Conductor passes inputs as strings) still links.
    assert pr_description.format_summary(tmp_path, "Body", closes="4001")["closesIssue"] == 4001


def test_pr_create_worker_forwards_the_issue_number(fake_task_input, tmp_path, monkeypatch):
    """The worker is the only place that knows the driving issue number."""
    from gitops.tasks import pr_create

    captured = {}

    def fake_pr_create(repo, **kwargs):
        captured.update(kwargs)
        return {"created": True, "number": 7, "url": "u", "draft": False}

    monkeypatch.setattr("gitops.tasks.github.pr_create", fake_pr_create)

    result = pr_create(fake_task_input(repoPath=str(tmp_path), title="T",
                                       body="Real prose here.", issueNumber=4001))
    assert result.status.value == "COMPLETED"
    assert captured["closes"] == 4001

    # With no issue the published body is unchanged from before.
    captured.clear()
    plain = pr_create(fake_task_input(repoPath=str(tmp_path), title="T",
                                      body="Real prose here."))
    assert plain.status.value == "COMPLETED"
    assert captured["closes"] == 0
