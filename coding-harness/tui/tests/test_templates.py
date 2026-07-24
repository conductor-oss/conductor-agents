"""Tests for the local prompt-template library (tui/templates.py).

Uses the CONDUCTOR_HARNESS_HOME override set by conftest.py so it writes to a tmp dir.
"""

from __future__ import annotations

from tui import templates


def test_save_list_load_roundtrip():
    templates.save("Security review", "Focus on authz and secrets.", workflows=("pr_review",))
    entries = templates.list_templates("pr_review")
    assert any(e.name == "Security review" for e in entries)
    e = next(e for e in entries if e.name == "Security review")
    assert templates.load(e) == "Focus on authz and secrets."
    assert e.workflows == ("pr_review",)


def test_workflow_filtering():
    templates.save("Only issues", "x", workflows=("issue_to_pr",))
    templates.save("Global one", "y")            # no workflows → applies everywhere
    pr = {e.name for e in templates.list_templates("pr_review")}
    assert "Global one" in pr
    assert "Only issues" not in pr               # scoped to issue_to_pr
    issues = {e.name for e in templates.list_templates("issue_to_pr")}
    assert "Only issues" in issues and "Global one" in issues


def test_parse_frontmatter_and_body():
    path = templates.templates_dir() / "manual.md"
    path.write_text(
        "---\nname: Manual\ndescription: hand written\nworkflows: [pr_review, address_pr]\n"
        "fields: [reviewPromptTemplate]\n---\n"
        "BODY LINE 1\nBODY LINE 2\n",
        encoding="utf-8",
    )
    e = next(e for e in templates.list_templates() if e.path == path)
    assert e.name == "Manual" and e.description == "hand written"
    assert e.workflows == ("pr_review", "address_pr")
    assert e.fields == ("reviewPromptTemplate",)
    assert templates.load(e) == "BODY LINE 1\nBODY LINE 2"


def test_repo_scoping_parse_and_filter():
    templates.save("SDK review", "x", workflows=("pr_review",),
                   repos=("conductor-oss/python-sdk", "acme/app"))
    templates.save("Any review", "y", workflows=("pr_review",))
    e = next(e for e in templates.list_templates() if e.name == "SDK review")
    assert e.repos == ("conductor-oss/python-sdk", "acme/app")
    # repo-restricted template only matches its repos (normalized from a URL too)
    assert e.applies_to_repo("https://github.com/conductor-oss/python-sdk.git")
    assert e.applies_to_repo("acme/app")
    assert not e.applies_to_repo("other/repo")
    assert not e.applies_to_repo(None)
    # list_templates repo filtering
    for_sdk = {t.name for t in templates.list_templates("pr_review", repo="conductor-oss/python-sdk")}
    assert for_sdk == {"SDK review", "Any review"}
    for_other = {t.name for t in templates.list_templates("pr_review", repo="x/y")}
    assert for_other == {"Any review"}                     # restricted one drops out
    empty_repo = {t.name for t in templates.list_templates("pr_review", repo="")}
    assert empty_repo == {"Any review"}                    # "" → restricted needs a matching repo
    unfiltered = {t.name for t in templates.list_templates("pr_review", repo=None)}
    assert unfiltered == {"SDK review", "Any review"}      # None → no repo filter (manager list)


def test_default_prompt_reads_shipped_defaults():
    # the TUI reads the same canonical files the worker uses
    for key in ("pr_review", "code", "address_pr"):
        assert templates.default_prompt(key), f"no shipped default for {key}"
    assert "{{diff}}" in templates.default_prompt("pr_review")
    assert templates.default_prompt("nope") is None


def test_create_seeds_from_default_prompt():
    entry = templates.create("My review", key="pr_review", workflows=("pr_review",))
    body = templates.load(entry)
    assert "senior code reviewer" in body and "{{diff}}" in body   # the real default, not a stub


def test_create_falls_back_to_stub_without_key():
    entry = templates.create("Freeform note")
    assert "Write the agent's prompt here" in templates.load(entry)


def test_field_and_workflow_key_maps():
    assert templates.FIELD_KEY["reviewPromptTemplate"] == "pr_review"
    assert templates.FIELD_KEY["fixPromptTemplate"] == "address_pr"
    assert templates.WORKFLOW_KEY["issue_to_pr"] == "code"


def test_no_frontmatter_uses_stem_and_full_body():
    path = templates.templates_dir() / "plain-note.md"
    path.write_text("just the prompt text", encoding="utf-8")
    e = next(e for e in templates.list_templates() if e.path == path)
    assert e.name == "plain-note" and templates.load(e) == "just the prompt text"
    assert e.applies_to("pr_review") and e.applies_to(None)


def test_apply_user_templates_routes_every_role_and_records_sources():
    code_path = templates.save(
        "Code style", "CODE {{subtask}}", workflows=("feature_campaign",),
        fields=("codePromptTemplate",))
    plan_path = templates.save(
        "Planning rules", "PLAN {{instruction}}", workflows=("feature_campaign",),
        fields=("planPromptTemplate",))

    payload, applied = templates.apply_user_templates(
        "feature_campaign", {"repoPath": "/tmp/repo", "instruction": "ship"})

    assert payload["codePromptTemplate"] == "CODE {{subtask}}"
    assert payload["codePromptTemplateSource"] == f"user:{code_path}"
    assert payload["planPromptTemplate"] == "PLAN {{instruction}}"
    assert payload["planPromptTemplateSource"] == f"user:{plan_path}"
    assert {(item.field, item.source) for item in applied} == {
        ("codePromptTemplate", f"user:{code_path}"),
        ("planPromptTemplate", f"user:{plan_path}"),
    }


def test_apply_user_templates_prefers_repo_and_explicit_values():
    templates.save(
        "Global code", "GLOBAL", workflows=("issue_to_pr",),
        fields=("codePromptTemplate",))
    repo_path = templates.save(
        "Repo code", "REPO", workflows=("issue_to_pr",), repos=("acme/app",),
        fields=("codePromptTemplate",))

    payload, _ = templates.apply_user_templates(
        "issue_to_pr", {"repo": "acme/app", "codePromptTemplate": "EXPLICIT"})
    assert payload["codePromptTemplate"] == "EXPLICIT"
    assert payload["codePromptTemplateSource"] == "input:inline"

    payload, _ = templates.apply_user_templates(
        "issue_to_pr", {"repo": "acme/app"})
    assert payload["codePromptTemplate"] == "REPO"
    assert payload["codePromptTemplateSource"] == f"user:{repo_path}"


def test_apply_user_templates_blocks_ambiguous_role():
    for name in ("Fix A", "Fix B"):
        templates.save(name, name, workflows=("address_pr",),
                       fields=("fixPromptTemplate",))
    try:
        templates.apply_user_templates("address_pr", {"repo": "acme/app"})
    except templates.TemplateSelectionError as exc:
        assert "address_pr.fixPromptTemplate" in str(exc)
        assert "Fix A" in str(exc) and "Fix B" in str(exc)
    else:
        raise AssertionError("ambiguous template role did not block the launch")


def test_feature_campaign_legacy_template_routes_to_code_prompt():
    path = templates.save("Campaign", "CODE", workflows=("feature_campaign",))
    payload, _ = templates.apply_user_templates(
        "feature_campaign", {"repoPath": "/tmp/repo", "instruction": "ship"})
    assert payload["codePromptTemplate"] == "CODE"
    assert payload["codePromptTemplateSource"] == f"user:{path}"
    assert "designPromptTemplate" not in payload
