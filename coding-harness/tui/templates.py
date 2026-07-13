"""Local prompt-template library for the launcher.

Each template is one markdown file under ``~/.conductor-harness/templates/`` (override with
``$CONDUCTOR_HARNESS_HOME``) with optional YAML-ish frontmatter:

    ---
    name: Security-focused review
    description: Emphasize authz, input validation, secrets
    workflows: [pr_review]
    ---
    You are a security-minded reviewer. ...

The launcher lists templates (filtered by workflow via the ``workflows`` key — a template
with no ``workflows`` key shows for all), loads one into the prompt-template field, and can
save the current field text back as a new template. The file *body* (frontmatter stripped)
is what gets sent as the workflow's ``*PromptTemplate`` input.

Frontmatter parsing is intentionally tiny (no PyYAML dependency): ``key: value`` lines and a
``workflows: [a, b]`` / comma list. Anything fancier is ignored.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def templates_dir() -> Path:
    home = os.environ.get("CONDUCTOR_HARNESS_HOME")
    base = Path(home) if home else Path.home() / ".conductor-harness"
    d = base / "templates"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Canonical built-in prompts shipped with the workers (single source of truth — the worker
# uses these as the default at runtime). The TUI seeds new/edited templates from them so a
# user starts from the real default, not a blank stub.
_DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "workers" / "defaults" / "prompts"

# Which built-in prompt (templateKey) a workflow's primary template field maps to.
WORKFLOW_KEY = {
    "pr_review": "pr_review", "code_parallel": "code", "issue_to_pr": "code",
    "address_pr": "code", "design_docs": "design",
}
# Launcher field (workflow input name) → built-in prompt key, for "load the default".
FIELD_KEY = {
    "reviewPromptTemplate": "pr_review", "codePromptTemplate": "code",
    "planPromptTemplate": "plan", "designPromptTemplate": "design",
    "fixPromptTemplate": "address_pr",
}


def default_prompt(key: str | None) -> str | None:
    """The shipped default prompt text for a templateKey, or None if unavailable."""
    if not key:
        return None
    path = _DEFAULTS_DIR / f"{key}.md"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _norm_repo(repo: str) -> str:
    from .catalog import short_repo
    return short_repo(repo or "").lower()


@dataclass
class TemplateEntry:
    path: Path
    name: str
    description: str = ""
    workflows: tuple[str, ...] = field(default_factory=tuple)
    repos: tuple[str, ...] = field(default_factory=tuple)
    body: str = ""

    def applies_to(self, workflow: str | None) -> bool:
        # no `workflows` key → applies everywhere; else must list this workflow
        return not self.workflows or workflow is None or workflow in self.workflows

    def applies_to_repo(self, repo: str | None) -> bool:
        # no `repos` key → applies to any repo; else the target must match one (owner/name)
        if not self.repos:
            return True
        if not repo:
            return False
        target = _norm_repo(repo)
        return any(_norm_repo(r) == target for r in self.repos)


_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def _parse(text: str, path: Path) -> TemplateEntry:
    name, desc, workflows, repos, body = path.stem, "", (), (), text
    m = _FM_RE.match(text)
    if m:
        front, body = m.group(1), m.group(2)
        for line in front.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip().lower(), val.strip()
            if key == "name" and val:
                name = val
            elif key == "description":
                desc = val
            elif key == "workflows":
                val = val.strip("[]")
                workflows = tuple(w.strip() for w in val.split(",") if w.strip())
            elif key == "repos":
                val = val.strip("[]")
                repos = tuple(r.strip() for r in val.split(",") if r.strip())
    return TemplateEntry(path=path, name=name, description=desc,
                         workflows=workflows, repos=repos, body=body.strip())


def list_templates(workflow: str | None = None, repo: str | None = None) -> list[TemplateEntry]:
    """All *.md templates (parsed), filtered to those that apply to ``workflow`` and ``repo``.

    ``repo`` semantics:
      * ``None`` — no repo filtering (the manager's full list; shows repo-scoped ones too).
      * ``""``  — repo unknown/not applicable (local workflows, or the repo field is empty):
                   only unrestricted templates apply.
      * ``"owner/name"`` (or URL) — unrestricted templates plus those scoped to that repo."""
    out: list[TemplateEntry] = []
    for p in sorted(templates_dir().glob("*.md")):
        try:
            entry = _parse(p.read_text(encoding="utf-8"), p)
        except OSError:
            continue
        if entry.applies_to(workflow) and (repo is None or entry.applies_to_repo(repo)):
            out.append(entry)
    return out


def load(entry: TemplateEntry) -> str:
    """The template body (frontmatter stripped) — what to send as the *PromptTemplate input."""
    return entry.body or _parse(entry.path.read_text(encoding="utf-8"), entry.path).body


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "template"


def save(name: str, text: str, *, workflows: tuple[str, ...] = (),
         repos: tuple[str, ...] = ()) -> Path:
    """Write ``text`` as a new template with a small frontmatter. Returns the path."""
    path = templates_dir() / f"{_slug(name)}.md"
    front = ["---", f"name: {name}"]
    if workflows:
        front.append(f"workflows: [{', '.join(workflows)}]")
    if repos:
        front.append(f"repos: [{', '.join(repos)}]")
    front.append("---\n")
    path.write_text("\n".join(front) + text.strip() + "\n", encoding="utf-8")
    return path


_STUB = ("Write the agent's prompt here. Use {{diff}}, {{feedback}}, {{instruction}}, or "
         "{{subtask}} where you want the runtime context injected; anything you omit is "
         "appended automatically.")


def create(name: str, *, key: str | None = None, workflows: tuple[str, ...] = (),
           repos: tuple[str, ...] = ()) -> TemplateEntry:
    """Create a new template file if it doesn't exist and return its entry. Seeds the body
    from the shipped default prompt for `key` (so the user starts from the real default);
    falls back to a stub when there's no matching default."""
    path = templates_dir() / f"{_slug(name)}.md"
    if not path.exists():
        save(name, default_prompt(key) or _STUB, workflows=workflows, repos=repos)
    return _parse(path.read_text(encoding="utf-8"), path)


def delete(entry: TemplateEntry) -> None:
    try:
        entry.path.unlink()
    except OSError:
        pass
