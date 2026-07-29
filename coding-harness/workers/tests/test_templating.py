"""Unit tests for prompt-template resolution (common/templating.py).

Import-light: exercises resolve_prompt/render_template/read_repo_template without the
agent backends. Run from workers/:  python -m pytest tests/test_templating.py -q
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common import templating as t  # noqa: E402


# --------------------------------------------------------------------------- render

def test_render_substitutes_placeholders():
    out = t.render_template("Review this:\n{{diff}}", {"diff": "DIFF-BODY"})
    assert out == "Review this:\nDIFF-BODY"


def test_render_appends_unused_context():
    out = t.render_template("Be a strict reviewer.", {"diff": "DIFF", "feedback": "FB"})
    assert out.startswith("Be a strict reviewer.")
    assert "## Context" in out and "### diff\nDIFF" in out and "### feedback\nFB" in out


def test_render_used_placeholder_not_reappended():
    out = t.render_template("D={{diff}}", {"diff": "X", "feedback": "FB"})
    assert "D=X" in out
    assert "### diff" not in out          # diff was consumed by its placeholder
    assert "### feedback\nFB" in out      # feedback was unused → appended


def test_render_empty_context_values_skipped_in_trailer():
    out = t.render_template("Persona.", {"diff": "", "feedback": "  "})
    assert out == "Persona."              # nothing non-empty to append


def test_render_unknown_placeholder_left_literal():
    out = t.render_template("keep {{notacontextkey}} literal", {"diff": "X"})
    assert "{{notacontextkey}}" in out


# --------------------------------------------------------------------------- resolve precedence

def test_resolve_prefers_explicit_template(tmp_path):
    _write_repo_template(tmp_path, "pr_review", "REPO TEMPLATE")
    out = t.resolve_prompt("BUILTIN", template="EXPLICIT {{diff}}",
                           template_key="pr_review", context={"diff": "D"}, worktree=str(tmp_path))
    assert out == "EXPLICIT D"


def test_resolve_details_reports_inline_source_and_hash(tmp_path):
    result = t.resolve_prompt_details(
        "BUILTIN", template="EXPLICIT {{diff}}", template_key="pr_review",
        context={"diff": "D"}, worktree=str(tmp_path),
    )
    assert result.prompt == "EXPLICIT D"
    assert result.source == "input:inline"
    assert result.template_key == "pr_review"
    assert len(result.sha256) == 64


def test_resolve_uses_repo_file_when_no_explicit(tmp_path):
    _write_repo_template(tmp_path, "pr_review", "REPO {{diff}}")
    out = t.resolve_prompt("BUILTIN", template="", template_key="pr_review",
                           context={"diff": "D"}, worktree=str(tmp_path))
    assert out == "REPO D"


def test_resolve_details_reports_repo_and_bundled_sources(tmp_path):
    _write_repo_template(tmp_path, "pr_review", "REPO {{diff}}")
    repo = t.resolve_prompt_details(
        "BUILTIN", template="", template_key="pr_review",
        context={"diff": "D"}, worktree=str(tmp_path),
    )
    assert repo.source == "repo:.conductor/pr_review.md"

    (tmp_path / ".conductor" / "pr_review.md").unlink()
    bundled = t.resolve_prompt_details(
        "BUILTIN", template="", template_key="pr_review",
        context={"diff": "D"}, worktree=str(tmp_path),
    )
    assert bundled.source == "bundled:pr_review"


def test_resolve_falls_back_to_builtin_when_no_bundled(tmp_path):
    # a key with no shipped default file → inline built-in used verbatim
    out = t.resolve_prompt("BUILTIN PROMPT", template=None, template_key="no-such-key",
                           context={"diff": "D"}, worktree=str(tmp_path))
    assert out == "BUILTIN PROMPT"


def test_resolve_uses_bundled_default(tmp_path):
    # no explicit, no repo file, but a shipped default exists for pr_review → it's used,
    # rendered with context (the built-in the worker uses by default)
    out = t.resolve_prompt("INLINE FALLBACK", template=None, template_key="pr_review",
                           context={"diff": "THE-DIFF", "feedback": "THE-FB"},
                           worktree=str(tmp_path))
    assert out != "INLINE FALLBACK"
    assert "senior code reviewer" in out and "THE-DIFF" in out and "THE-FB" in out


def test_bundled_default_exists_for_each_key():
    for key in ("pr_review", "code", "address_pr"):
        assert t.bundled_default(key), f"missing shipped default for {key}"


def test_repo_layer_disabled_by_env_falls_to_bundled(tmp_path, monkeypatch):
    _write_repo_template(tmp_path, "pr_review", "REPO SHOULD BE IGNORED")
    monkeypatch.setenv("CODING_AGENT_REPO_TEMPLATES", "0")
    out = t.resolve_prompt("BUILTIN", template="", template_key="pr_review",
                           context={"diff": "D", "feedback": "F"}, worktree=str(tmp_path))
    assert "REPO SHOULD BE IGNORED" not in out       # repo layer disabled
    assert "senior code reviewer" in out             # shipped default used instead


def test_repo_template_key_traversal_rejected(tmp_path):
    # a key with path separators must not escape .conductor/
    (tmp_path / "secret.md").write_text("SECRET", encoding="utf-8")
    assert t.read_repo_template(str(tmp_path), "../secret") is None
    assert t.read_repo_template(str(tmp_path), "a/b") is None


def _write_repo_template(root: pathlib.Path, key: str, body: str) -> None:
    d = root / ".conductor"
    d.mkdir(exist_ok=True)
    (d / f"{key}.md").write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- repo guide (AGENTS.md)

def test_read_repo_guide_discovery_order(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("claude", encoding="utf-8")
    (tmp_path / "AGENT.md").write_text("agent-singular", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("AGENTS wins", encoding="utf-8")
    assert t.read_repo_guide(str(tmp_path)) == ("AGENTS.md", "AGENTS wins")
    (tmp_path / "AGENTS.md").unlink()
    assert t.read_repo_guide(str(tmp_path)) == ("AGENT.md", "agent-singular")
    (tmp_path / "AGENT.md").unlink()
    assert t.read_repo_guide(str(tmp_path)) == ("CLAUDE.md", "claude")


def test_read_repo_guide_absent_and_empty(tmp_path):
    assert t.read_repo_guide(str(tmp_path)) is None       # nothing there
    (tmp_path / "AGENTS.md").write_text("   \n", encoding="utf-8")
    assert t.read_repo_guide(str(tmp_path)) is None       # empty → skipped


def test_read_repo_guide_capped(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x" * (t._GUIDE_CAP + 500), encoding="utf-8")
    name, text = t.read_repo_guide(str(tmp_path))
    assert name == "AGENTS.md" and len(text) <= t._GUIDE_CAP + 40 and text.endswith("truncated]")


def test_read_repo_guide_env_gate(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("guide", encoding="utf-8")
    monkeypatch.setenv("CODING_AGENT_REPO_GUIDE", "0")
    assert t.read_repo_guide(str(tmp_path)) is None


def test_should_inject_guide_truth_table():
    # AGENTS.md / AGENT.md always inject, regardless of backend/settings
    assert t.should_inject_guide("claude", ["project"], "AGENTS.md") is True
    assert t.should_inject_guide("claude", ["project"], "AGENT.md") is True
    # CLAUDE.md: skip for Claude when a fs setting source loads it; inject otherwise
    assert t.should_inject_guide("claude", ["project"], "CLAUDE.md") is False
    assert t.should_inject_guide("claude", None, "CLAUDE.md") is False        # None → default project
    assert t.should_inject_guide("claude", [], "CLAUDE.md") is True           # settings off → inject
    assert t.should_inject_guide("codex", ["project"], "CLAUDE.md") is True   # non-Claude → inject
    assert t.should_inject_guide("gemini", ["project"], "CLAUDE.md") is True


# --------------------------------------------------------------------------- @path prompt

def test_resolve_at_path_reads_repo_file(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "review.md").write_text("REVIEW GUIDE {{diff}}", encoding="utf-8")
    out = t.resolve_prompt("BUILTIN", template="@docs/review.md", template_key="pr_review",
                           context={"diff": "D"}, worktree=str(tmp_path))
    assert out == "REVIEW GUIDE D"

    details = t.resolve_prompt_details(
        "BUILTIN", template="@docs/review.md", template_key="pr_review",
        context={"diff": "D"}, worktree=str(tmp_path),
    )
    assert details.source == "repo:@docs/review.md"


def test_resolve_at_path_missing_falls_through_to_repo_then_bundled(tmp_path):
    # @path not found → fall through to .conductor/<key>.md
    _write_repo_template(tmp_path, "pr_review", "REPO {{diff}}")
    out = t.resolve_prompt("BUILTIN", template="@nope.md", template_key="pr_review",
                           context={"diff": "D"}, worktree=str(tmp_path))
    assert out == "REPO D"
    # …and with no repo file either, to the shipped default
    (tmp_path / ".conductor" / "pr_review.md").unlink()
    out2 = t.resolve_prompt("BUILTIN", template="@nope.md", template_key="pr_review",
                            context={"diff": "D", "feedback": "F"}, worktree=str(tmp_path))
    assert "senior code reviewer" in out2


def test_resolve_at_path_escape_blocked(tmp_path):
    secret = tmp_path.parent / "secret.md"
    secret.write_text("SECRET", encoding="utf-8")
    out = t.resolve_prompt("BUILTIN INLINE", template="@../secret.md", template_key="no-key",
                           context={}, worktree=str(tmp_path))
    assert "SECRET" not in out and out == "BUILTIN INLINE"   # escape blocked → falls to inline


def test_resolve_at_path_gated_by_repo_templates(tmp_path, monkeypatch):
    (tmp_path / "p.md").write_text("FROM PATH", encoding="utf-8")
    monkeypatch.setenv("CODING_AGENT_REPO_TEMPLATES", "0")
    out = t.resolve_prompt("BUILTIN INLINE", template="@p.md", template_key="no-key",
                           context={}, worktree=str(tmp_path))
    assert out == "BUILTIN INLINE"                           # repo reads disabled → inline
