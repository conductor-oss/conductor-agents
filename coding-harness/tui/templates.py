"""Local prompt-template library for the launcher.

Each template is one markdown file under ``~/.conductor-harness/templates/`` (override with
``$CONDUCTOR_HARNESS_HOME``) with optional YAML-ish frontmatter:

    ---
    name: Security-focused review
    description: Emphasize authz, input validation, secrets
    workflows: [pr_review]
    fields: [reviewPromptTemplate]
    ---
    You are a security-minded reviewer. ...

The TUI lists templates filtered by workflow/repository and routes them to prompt roles via
the optional ``fields`` key. A legacy template without ``fields`` applies to the workflow's
primary prompt role. The file *body* (frontmatter stripped) is what gets sent as the
workflow's ``*PromptTemplate`` input.

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
    "local_review": "local_review", "pr_review": "pr_review", "code_parallel": "code", "issue_to_pr": "code",
    "address_pr": "code", "design_docs": "design", "feature_campaign": "code",
    "openspec_development": "code",
    "pr_review_sweep": "pr_review", "pr_address_sweep": "address_pr",
    "issue_resolution_sweep": "code",
}

# The field that receives legacy templates which predate ``fields`` frontmatter. Keep this
# explicit: some workflows list supporting prompts (assessment/design) before their main coding
# prompt in the form catalog.
PRIMARY_FIELD = {
    "local_review": "localReviewPromptTemplate",
    "pr_review": "reviewPromptTemplate",
    "code_parallel": "codePromptTemplate",
    "issue_to_pr": "codePromptTemplate",
    "address_pr": "fixPromptTemplate",
    "design_docs": "designPromptTemplate",
    "feature_campaign": "codePromptTemplate",
    "openspec_development": "codePromptTemplate",
    "pr_review_sweep": "reviewPromptTemplate",
    "pr_address_sweep": "fixPromptTemplate",
    "issue_resolution_sweep": "codePromptTemplate",
}
# Launcher field (workflow input name) → built-in prompt key, for "load the default".
FIELD_KEY = {
    "reviewPromptTemplate": "pr_review", "localReviewPromptTemplate": "local_review", "codePromptTemplate": "code",
    "planPromptTemplate": "plan", "designPromptTemplate": "design",
    "fixPromptTemplate": "address_pr",
    "designJudgePromptTemplate": "design_judge",
    "approvalJudgePromptTemplate": "approval_judge",
    "reviewPromptTemplate": "pr_review",
    "revisionPromptTemplate": "campaign_revision",
    "assessPromptTemplate": "openspec_assess",
    "verificationPromptTemplate": "openspec_verify",
}

WORKFLOW_FIELD_KEY = {
    ("pr_review", "approvalJudgePromptTemplate"): "pr_review_judge",
    ("pr_review_sweep", "approvalJudgePromptTemplate"): "pr_review_judge",
    ("address_pr", "approvalJudgePromptTemplate"): "address_pr_judge",
    ("pr_address_sweep", "approvalJudgePromptTemplate"): "address_pr_judge",
    ("issue_to_pr", "approvalJudgePromptTemplate"): "issue_to_pr_judge",
    ("issue_resolution_sweep", "approvalJudgePromptTemplate"): "issue_to_pr_judge",
    ("feature_campaign", "reviewPromptTemplate"): "campaign_review",
}


def field_key(workflow: str, field_name: str) -> str | None:
    return WORKFLOW_FIELD_KEY.get((workflow, field_name)) or FIELD_KEY.get(field_name)


def primary_field(workflow: str) -> str | None:
    """The workflow's primary prompt input, including legacy-template routing."""
    return PRIMARY_FIELD.get(workflow)


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
    fields: tuple[str, ...] = field(default_factory=tuple)
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
    name, desc, workflows, repos, fields, body = path.stem, "", (), (), (), text
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
            elif key in ("field", "fields"):
                val = val.strip("[]")
                fields = tuple(f.strip() for f in val.split(",") if f.strip())
    return TemplateEntry(path=path, name=name, description=desc,
                         workflows=workflows, repos=repos, fields=fields, body=body.strip())


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


def list_field_templates(workflow: str, field_name: str, repo: str | None = None) -> list[TemplateEntry]:
    """Templates which can populate one field (including legacy templates for the primary)."""
    primary = primary_field(workflow)
    return [entry for entry in list_templates(workflow, repo=repo)
            if field_name in entry.fields or (field_name == primary and not entry.fields)]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "template"


def save(name: str, text: str, *, workflows: tuple[str, ...] = (),
         repos: tuple[str, ...] = (), fields: tuple[str, ...] = ()) -> Path:
    """Write ``text`` as a new template with a small frontmatter. Returns the path."""
    path = templates_dir() / f"{_slug(name)}.md"
    front = ["---", f"name: {name}"]
    if workflows:
        front.append(f"workflows: [{', '.join(workflows)}]")
    if repos:
        front.append(f"repos: [{', '.join(repos)}]")
    if fields:
        front.append(f"fields: [{', '.join(fields)}]")
    front.append("---\n")
    path.write_text("\n".join(front) + text.strip() + "\n", encoding="utf-8")
    return path


_STUB = ("Write the agent's prompt here. Use {{diff}}, {{feedback}}, {{instruction}}, or "
         "{{subtask}} where you want the runtime context injected; anything you omit is "
         "appended automatically.")


def create(name: str, *, key: str | None = None, workflows: tuple[str, ...] = (),
           repos: tuple[str, ...] = (), fields: tuple[str, ...] = ()) -> TemplateEntry:
    """Create a new template file if it doesn't exist and return its entry. Seeds the body
    from the shipped default prompt for `key` (so the user starts from the real default);
    falls back to a stub when there's no matching default."""
    path = templates_dir() / f"{_slug(name)}.md"
    if not path.exists():
        save(name, default_prompt(key) or _STUB, workflows=workflows, repos=repos,
             fields=fields)
    return _parse(path.read_text(encoding="utf-8"), path)


class TemplateSelectionError(ValueError):
    """Raised when a TUI launch cannot select a unique template for a prompt role."""


@dataclass(frozen=True)
class AppliedTemplate:
    field: str
    source: str


def _candidates(entries: list[TemplateEntry], field_name: str, primary: str | None) -> list[TemplateEntry]:
    """Applicable candidates for one prompt role, keeping only the most-specific tier."""
    candidates = [entry for entry in entries if field_name in entry.fields]
    if field_name == primary:
        candidates.extend(entry for entry in entries if not entry.fields)
    if not candidates:
        return []
    # Repo-scoped beats global; an explicit field mapping beats legacy primary routing.
    best = max((bool(entry.repos), bool(entry.fields)) for entry in candidates)
    return [entry for entry in candidates
            if (bool(entry.repos), bool(entry.fields)) == best]


def apply_user_templates(workflow: str, inputs: dict, *,
                         skip_fields: set[str] | None = None) -> tuple[dict, list[AppliedTemplate]]:
    """Resolve every prompt role for a TUI-originated workflow input.

    Explicit workflow input always wins. Otherwise, a unique applicable user-library template
    is attached with durable ``*Source`` provenance. Ambiguity is an error rather than silently
    choosing a file. Roles without a user template remain blank so the worker can continue with
    the repository ``.conductor/<key>.md`` and bundled-default layers.
    """
    from . import catalog

    result = dict(inputs)
    spec = catalog.CATALOG.get(workflow)
    if not spec:
        return result, []
    prompt_fields = [item.name for item in spec.fields if item.kind == "template"]
    if not prompt_fields:
        return result, []
    primary = primary_field(workflow) or prompt_fields[0]
    repo = str(result.get("repo") or "") if any(item.name == "repo" for item in spec.fields) else ""
    entries = list_templates(workflow, repo=repo)
    skipped = skip_fields or set()
    applied: list[AppliedTemplate] = []
    for field_name in prompt_fields:
        if field_name in skipped:
            continue
        source_name = f"{field_name}Source"
        value = result.get(field_name)
        if value not in (None, ""):
            if not result.get(source_name):
                text = str(value).strip()
                result[source_name] = f"repo:{text[1:]}" if text.startswith("@") else "input:inline"
            applied.append(AppliedTemplate(field_name, str(result[source_name])))
            continue
        candidates = _candidates(entries, field_name, primary)
        if len(candidates) > 1:
            choices = ", ".join(f"{entry.name} ({entry.path})" for entry in candidates)
            raise TemplateSelectionError(
                f"multiple equally specific templates apply to {workflow}.{field_name}: "
                f"{choices}. Select one in the launcher or pass {field_name} explicitly."
            )
        if candidates:
            entry = candidates[0]
            result[field_name] = load(entry)
            result[source_name] = f"user:{entry.path}"
            applied.append(AppliedTemplate(field_name, result[source_name]))
    return result, applied


def delete(entry: TemplateEntry) -> None:
    try:
        entry.path.unlink()
    except OSError:
        pass
