"""Prompt-template resolution + repo-context ingestion for the coding_agent worker.

Lets a caller fully override an agent step's task prompt from layered sources, highest
precedence first:

  1. explicit `promptTemplate` input — inline text, OR `@repo/relative/path` to read a
     prompt file from the checkout;
  2. repo-resident `<worktree>/.conductor/<templateKey>.md` — committed in the target
     repo, version-controlled, applies to every run with no payload change;
  3. the shipped default `workers/defaults/prompts/<templateKey>.md`;
  4. the inline `prompt` the workflow ships (safety fallback).

A chosen template is rendered against a `context` map: `{{key}}` placeholders are filled,
and any unused non-empty context entry is appended under a "## Context" trailer so a
persona-only template still receives the runtime context (diff/feedback/…).

Separately, `read_repo_guide` finds a repo "agent guide" (AGENTS.md / AGENT.md / CLAUDE.md)
that the worker injects into the prompt so every backend + the review step learn how to
build/test/review the repo.

Kept import-light (stdlib only) so it's unit-testable without the agent backends.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

# Repo "agent guide" discovery: first existing, non-empty root file wins. AGENTS.md is the
# cross-tool standard; CLAUDE.md is the Claude convention (kept as a fallback so Codex/Gemini
# also benefit). Capped so a huge guide can't blow up every prompt.
_GUIDE_NAMES = ("AGENTS.md", "AGENT.md", "CLAUDE.md")
_GUIDE_CAP = 24_000

# Canonical built-in prompts, one file per templateKey, in {{placeholder}} form. These are
# the single source of truth for the default prompts: the worker uses them (rendered) as the
# built-in, and the TUI seeds new templates from the same files. Ship alongside the workers.
_DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "defaults" / "prompts"


@dataclass(frozen=True)
class PromptResolution:
    prompt: str
    source: str
    template_key: str
    sha256: str


def _resolution(prompt: str, source: str, template_key) -> PromptResolution:
    return PromptResolution(
        prompt=prompt,
        source=source,
        template_key=str(template_key or ""),
        sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )


def bundled_default(template_key) -> str | None:
    """The shipped default prompt for a templateKey (workers/defaults/prompts/<key>.md),
    or None. Basename-sanitized like the repo layer."""
    if not template_key:
        return None
    key = str(template_key).strip()
    if not key or os.path.basename(key) != key:
        return None
    path = _DEFAULTS_DIR / f"{key}.md"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
    return None


def repo_templates_enabled() -> bool:
    """The repo-resident layer (.conductor/<key>.md) is a repo-controlled injection
    vector like CLAUDE.md; operators disable it for untrusted repos with
    CODING_AGENT_REPO_TEMPLATES=0. Read at call time so tests/operators can toggle it.
    An explicit `promptTemplate` input is unaffected (it never touches the repo)."""
    return str(os.environ.get("CODING_AGENT_REPO_TEMPLATES", "1")).strip().lower() \
        not in ("0", "false", "no", "off", "")


def repo_guide_enabled() -> bool:
    """Repo guide ingestion (AGENTS.md/…) is repo-controlled content — an injection vector
    like CLAUDE.md. Operators disable it for untrusted repos with CODING_AGENT_REPO_GUIDE=0.
    Read at call time. Default on."""
    return str(os.environ.get("CODING_AGENT_REPO_GUIDE", "1")).strip().lower() \
        not in ("0", "false", "no", "off", "")


def read_repo_guide(worktree: str, names: tuple[str, ...] = _GUIDE_NAMES) -> tuple[str, str] | None:
    """First existing, non-empty guide file at the worktree root → (filename, text), capped.
    Gated by `repo_guide_enabled()`. Names are fixed root basenames (no traversal)."""
    if not worktree or not repo_guide_enabled():
        return None
    for name in names:
        path = os.path.join(worktree, name)
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().strip()
                if text:
                    if len(text) > _GUIDE_CAP:
                        text = text[:_GUIDE_CAP] + "\n…[repo guide truncated]"
                    return (name, text)
        except OSError:
            continue
    return None


def should_inject_guide(backend, setting_sources, guide_name: str) -> bool:
    """Whether to prepend a discovered guide into the prompt. Always inject AGENTS.md/AGENT.md.
    Skip CLAUDE.md only for the Claude backend when a filesystem setting source that loads it
    (project/user/local) is active — Claude already ingests CLAUDE.md natively, so injecting it
    would double-load. (Codex/Gemini still get CLAUDE.md — the chosen fallback.)"""
    if guide_name != "CLAUDE.md":
        return True
    be = (backend or "claude")
    ss = setting_sources if setting_sources is not None else ["project"]
    if be == "claude" and any(s in ss for s in ("project", "user", "local")):
        return False
    return True


def read_repo_prompt_path(worktree: str, relpath: str) -> str | None:
    """Read a prompt file at a repo-relative path (the `@path` form of `promptTemplate`).
    Guarded against escaping the worktree; gated by `repo_templates_enabled()` (repo content)."""
    if not worktree or not relpath or not repo_templates_enabled():
        return None
    root = os.path.realpath(worktree)
    full = os.path.realpath(os.path.join(root, relpath))
    if full != root and not full.startswith(root + os.sep):
        return None                      # escaped the worktree
    try:
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as fh:
                return fh.read().strip() or None
    except OSError:
        return None
    return None


def read_repo_template(worktree: str, template_key) -> str | None:
    """Read <worktree>/.conductor/<templateKey>.md if present and the repo layer is on.
    The key is basename-sanitized (no path separators / traversal); a key that changes
    under basename() is rejected."""
    if not template_key or not worktree or not repo_templates_enabled():
        return None
    key = str(template_key).strip()
    if not key or os.path.basename(key) != key:
        return None
    path = os.path.join(worktree, ".conductor", f"{key}.md")
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip() or None
    except OSError:
        return None
    return None


def render_template(template: str, context: dict | None) -> str:
    """Fill {{key}} placeholders from `context`; append any UNUSED non-empty context
    entries in a delimited trailer. Placeholders with no matching context key are left
    as-is (never blindly blanked — a template may contain literal double braces)."""
    ctx = {k: ("" if v is None else str(v)) for k, v in (context or {}).items()}
    out, used = template, set()
    for k, v in ctx.items():
        ph = "{{" + k + "}}"
        if ph in out:
            out = out.replace(ph, v)
            used.add(k)
    leftover = [(k, ctx[k]) for k in ctx if k not in used and ctx[k].strip()]
    if leftover:
        out += "\n\n## Context" + "".join(f"\n\n### {k}\n{v}" for k, v in leftover)
    return out


def resolve_prompt_details(prompt: str, *, template, template_key, context,
                           worktree: str) -> PromptResolution:
    """Effective prompt, precedence highest-first:
      explicit `promptTemplate` (inline text, or `@repo/path` → a file in the checkout)
      > repo `.conductor/<key>.md` > shipped default (workers/defaults/prompts/<key>.md)
      > inline `prompt` (safety fallback).
    The first tiers are rendered against `context`; the inline prompt (which carries its own
    inline context) is returned verbatim. A missing/blocked `@path` falls through to the
    repo/shipped default rather than failing the run."""
    t = template.strip() if isinstance(template, str) else ""
    chosen = None
    source = ""
    if t.startswith("@"):
        relpath = t[1:].strip()
        chosen = read_repo_prompt_path(worktree, relpath)
        if chosen is not None:
            source = f"repo:@{relpath}"
    elif t:
        chosen = t
        source = "input:inline"
    if chosen is None:
        chosen = read_repo_template(worktree, template_key)
        if chosen is not None:
            source = f"repo:.conductor/{template_key}.md"
    if chosen is None:
        chosen = bundled_default(template_key)
        if chosen is not None:
            source = f"bundled:{template_key}"
    if chosen:
        return _resolution(render_template(chosen, context), source, template_key)
    return _resolution(prompt, "workflow:inline-prompt", template_key)


def resolve_prompt(prompt: str, *, template, template_key, context, worktree: str) -> str:
    """Backward-compatible text-only wrapper around :func:`resolve_prompt_details`."""
    return resolve_prompt_details(
        prompt, template=template, template_key=template_key,
        context=context, worktree=worktree,
    ).prompt
