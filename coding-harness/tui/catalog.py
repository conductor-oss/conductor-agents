"""The workflow catalog — the single source of truth for the TUI's workflow knowledge.

One entry per *launchable* workflow. Each `WorkflowSpec` drives three things so the
screens stay generic:
  * the launcher form (`fields`),
  * the dashboard "target" column (`target`), and
  * the run-detail result card (`result`).

Adding a future workflow = one entry here; no screen/widget changes. A test
(`tests/test_catalog.py`) asserts these field defaults never drift from the registered
workflow JSONs under `workers/workflows/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Field:
    name: str                         # form field id (usually == the workflow input key)
    label: str
    kind: str                         # text|int|float|bool|enum|gh_issue|gh_pr|multiline
    default: object = None            # None => required
    help: str = ""
    choices: tuple[str, ...] = ()     # for enum
    advanced: bool = False            # collapsed under "Advanced"
    # Workflow input key(s) this field writes. Default: itself. A synthetic field like
    # "backend" writes several (planAgent + codeAgent) — lets one control drive many inputs.
    maps_to: tuple[str, ...] = ()
    # The value the launcher form initializes to, WHEN it should differ from the workflow
    # `default`. Used by the HITL gate fields: workflow default is False (so CLI/automation
    # runs gate-off), but the TUI form starts checked so interactive runs pause for review.
    # `build_payload` still compares against `default`, so a checked box sends the changed
    # value. None => fall back to `default`.
    tui_default: object = None

    @property
    def required(self) -> bool:
        return self.default is None

    @property
    def form_default(self) -> object:
        """The value the launcher initializes the widget to (tui_default if set)."""
        return self.tui_default if self.tui_default is not None else self.default

    @property
    def targets(self) -> tuple[str, ...]:
        return self.maps_to or (self.name,)


@dataclass(frozen=True)
class ResultCard:
    title: str
    rows: list[tuple[str, str]]
    primary_url: str | None = None
    primary_label: str = "open"


@dataclass(frozen=True)
class WorkflowSpec:
    name: str                         # conductor workflow name
    action: str                       # human label, e.g. "Review a pull request"
    blurb: str
    fields: tuple[Field, ...]
    target: Callable[[dict], str]     # workflow input dict -> dashboard target string
    result: Callable[[dict], ResultCard]  # workflow output dict -> result card

    def build_payload(self, values: dict) -> dict:
        """Turn form values into a start payload: required fields always sent; optional
        fields sent only when changed from their default (workflows apply their own
        inputTemplate defaults server-side). A field may fan out to several input keys."""
        payload: dict = {}
        for f in self.fields:
            if f.name not in values:
                continue
            val = values[f.name]
            if not f.required:
                if val == f.default or val is None or val == "":
                    continue
            for key in f.targets:
                payload[key] = val
        return payload


# --------------------------------------------------------------------------- helpers

def short_repo(repo: str) -> str:
    """`https://github.com/acme/app.git` or `acme/app` -> `acme/app`."""
    s = (repo or "").strip()
    if s.startswith("git@"):
        s = s.split(":", 1)[-1]
    elif "://" in s:
        s = s.split("://", 1)[-1].split("/", 1)[-1]
    if s.endswith(".git"):
        s = s[:-4]
    return s.strip("/")


def _get(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def normalize_local_paths(values: dict, *, cwd: str | None = None) -> dict:
    """Expand user-facing local paths before they become durable workflow inputs."""
    normalized = dict(values or {})
    raw = normalized.get("repoPath")
    if isinstance(raw, str) and raw.strip():
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            path = Path(cwd or Path.cwd()) / path
        normalized["repoPath"] = str(path.resolve(strict=False))
    source = normalized.get("specSource")
    if isinstance(source, str) and source.strip():
        raw_source = source.strip()
        local_source = raw_source.removeprefix("file://")
        is_remote = raw_source.startswith(("git@", "ssh://", "http://", "https://"))
        if not is_remote:
            path = Path(local_source).expanduser()
            if not path.is_absolute():
                path = Path(cwd or Path.cwd()) / path
            prefix = "file://" if raw_source.startswith("file://") else ""
            normalized["specSource"] = prefix + str(path.resolve(strict=False))
    return normalized


# --------------------------------------------------------------------------- shared field sets

_BACKENDS = ("claude", "codex", "gemini")
_MODEL_PROFILE = Field("modelProfile", "Model profile", "text", "", advanced=True,
                       help="profile name; blank = the configured default")

# Advanced caps common to every workflow (name -> default). Built per-workflow so the
# defaults match each JSON exactly.
def _caps(max_turns: int, budget: float) -> tuple[Field, ...]:
    return (
        Field("maxTurns", "Max turns", "int", max_turns, advanced=True,
              help="tool-use round cap per agent"),
        Field("maxBudgetUsd", "Max budget $", "float", budget, advanced=True,
              help="USD spend cap per agent"),
    )


# --------------------------------------------------------------------------- targets & results

def _t_pr(i: dict) -> str:
    return f"{short_repo(_get(i, 'repo', default=''))} PR#{i.get('prNumber', '?')}"


def _t_issue(i: dict) -> str:
    return f"{short_repo(_get(i, 'repo', default=''))} #{i.get('issueNumber', '?')}"


def _t_local(i: dict) -> str:
    return i.get("repoPath", "?")


def _t_openspec(i: dict) -> str:
    source = short_repo(str(i.get("specSource") or "")) or "?"
    target = source if i.get("useSpecSourceWorkspace") else i.get("repoPath", "?")
    return f"{target} ← {source}#{i.get('changeId', '?')}"


def _prompt_source(value: object) -> str:
    if isinstance(value, dict):
        direct = value.get("resolvedSource") or value.get("requestedSource")
        if direct:
            return str(direct)
        for nested in value.values():
            found = _prompt_source(nested)
            if found != "—":
                return found
    if isinstance(value, list):
        for nested in value:
            found = _prompt_source(nested)
            if found != "—":
                return found
    return "—"


def _r_pr_review(o: dict) -> ResultCard:
    url = o.get("reviewUrl") or None
    files = o.get("changedFiles") or []
    rows = [
        ("verdict", str(o.get("event", "?"))),
        ("workspace", "retained" if o.get("workspaceRetained", True) else "cleaned"),
        ("inline comments", str(o.get("inlineCount", 0))),
        ("files reviewed", str(len(files) if isinstance(files, list) else files)),
        ("tokens", str(o.get("tokenUsed", 0))),
        ("cost", f"${float(o.get('costUsd') or 0):.2f}"),
        ("prompt", _prompt_source(o.get("reviewPromptTemplate"))),
    ]
    return ResultCard("Review posted", rows, url, "open review")


def _r_local_review(o: dict) -> ResultCard:
    files = o.get("changedFiles") or []
    comments = o.get("comments") or []
    rows = [
        ("verdict", str(o.get("verdict") or "comment")),
        ("findings", str(len(comments) if isinstance(comments, list) else comments)),
        ("files reviewed", str(len(files) if isinstance(files, list) else files)),
        ("baseline", str(o.get("baseRef") or "—")),
        ("tokens", str(o.get("tokenUsed", 0))),
        ("cost", f"${float(o.get('costUsd') or 0):.2f}"),
        ("prompt", _prompt_source(o.get("reviewPromptTemplate"))),
    ]
    return ResultCard("Local review complete", rows, None)


def _r_issue_to_pr(o: dict) -> ResultCard:
    url = o.get("prUrl") or None
    subs = o.get("subtasks") or []
    conflicts = o.get("conflicts") or []
    rows = [
        ("PR", f"#{o.get('prNumber', '?')}"),
        ("branch", str(o.get("changeBranch", "?"))),
        ("workspace", "retained" if o.get("workspaceRetained", True) else "cleaned"),
        ("subtasks", str(len(subs) if isinstance(subs, list) else subs)),
        ("conflicts", str(len(conflicts) if isinstance(conflicts, list) else conflicts)),
        ("tokens", str(o.get("totalTokens", 0))),
        ("cost", f"${float(o.get('totalCostUsd') or 0):.2f}"),
        ("prompt", _prompt_source(o.get("promptTemplates"))),
    ]
    return ResultCard("Pull request opened", rows, url, "open PR")


def _r_address_pr(o: dict) -> ResultCard:
    url = o.get("replyUrl") or None
    rows = [
        ("pushed", "yes" if o.get("pushed") else "no"),
        ("engine", str(o.get("engine", "?"))),
        ("PR", f"#{o.get('prNumber', '?')}"),
        ("workspace", "retained" if o.get("workspaceRetained", True) else "cleaned"),
        ("tokens", str(o.get("totalTokens") or "—")),
        ("prompt", _prompt_source(o.get("agentPromptTemplate"))),
    ]
    return ResultCard("Feedback addressed", rows, url, "open PR comment")


def _r_code_parallel(o: dict) -> ResultCard:
    subs = o.get("subtasks") or []
    merged = o.get("merged") or []
    conflicts = o.get("conflicts") or []
    rows = [
        ("branch", str(o.get("changeBranch", "?"))),
        ("workspace", "retained" if o.get("workspaceRetained", True) else "cleaned"),
        ("subtasks", str(len(subs) if isinstance(subs, list) else subs)),
        ("merged", str(len(merged) if isinstance(merged, list) else merged)),
        ("conflicts", str(len(conflicts) if isinstance(conflicts, list) else conflicts)),
        ("tokens", str(o.get("totalTokens", 0))),
        ("cost", f"${float(o.get('totalCostUsd') or 0):.2f}"),
        ("plan prompt", _prompt_source(o.get("planPromptTemplate"))),
        ("code prompt", _prompt_source(o.get("codePromptTemplates"))),
    ]
    return ResultCard("Change coded & merged", rows, None)


def _r_feature_campaign(o: dict) -> ResultCard:
    tasks = o.get("completedDagTasks") or []
    waves = o.get("completedWaves") or []
    checks = o.get("finalChecks") or {}
    rows = [
        ("outcome", str(o.get("outcome") or "incomplete")),
        ("branch", str(o.get("verifiedBranch") or o.get("branch") or "retained (not verified)")),
        ("workspace", "retained" if o.get("workspaceRetained", True) else "cleaned"),
        ("DAG tasks", str(len(tasks) if isinstance(tasks, list) else tasks)),
        ("waves", str(len(waves) if isinstance(waves, list) else waves)),
        ("final checks", "pass" if checks.get("blockingPassed") else "needs attention"),
        ("tokens", str(o.get("totalTokens", 0))),
        ("cost", f"${float(o.get('totalCostUsd') or 0):.2f}"),
        ("review prompt", _prompt_source(o.get("reviewPromptTemplate"))),
    ]
    return ResultCard("Feature campaign complete", rows, None)


def _r_openspec(o: dict) -> ResultCard:
    archive = o.get("archive") or {}
    if not isinstance(archive, dict):
        archive = {}
    rows = [
        ("outcome", str(o.get("outcome") or "needs attention")),
        ("change", str(o.get("changeId") or "?")),
        ("workflow", str(o.get("selectedWorkflow") or "?")),
        ("branch", str(o.get("verifiedBranch") or "retained (not verified)")),
        ("workspace", "retained" if o.get("workspaceRetained", True) else "cleaned"),
        ("source workspace", "yes" if (o.get("sourceWorkspace") or {}).get("useSpecSourceWorkspace") else "no"),
        ("materialized", ", ".join(o.get("materializedSourcePaths") or []) or "none"),
        ("archived", "yes" if archive.get("archived") else "no"),
        ("tokens", str(o.get("totalTokens", 0))),
        ("cost", f"${float(o.get('totalCostUsd') or 0):.2f}"),
        ("assess prompt", _prompt_source(o.get("assessmentPromptTemplate"))),
    ]
    url = o.get("prUrl") or None
    return ResultCard("OpenSpec development complete", rows, url, "open draft PR")


def _r_sweep(o: dict) -> ResultCard:
    rows = [(key, str(o.get(key, 0))) for key in
            ("scanned", "eligible", "claimed", "dispatched")]
    rows += [("skipped", str(len(o.get("skipped") or []))),
             ("blocked", str(len(o.get("blocked") or [])))]
    return ResultCard("Automation sweep complete", rows, None)


def _automation_fields(max_new: int, max_active: int, *, template_field: str,
                       template_label: str, issues: bool = False) -> tuple[Field, ...]:
    fields = [
        Field("repo", "Repo", "text", help="URL or owner/name"),
        Field("approvalMode", "Approval", "enum", "human", choices=("human", "llm")),
        Field("agent", "Producer backend", "enum", "", choices=("", *_BACKENDS)),
        Field("judgeAgent", "Judge backend", "enum", "", choices=("", *_BACKENDS), advanced=True),
        Field("model", "Producer model", "text", "", advanced=True),
        Field("judgeModel", "Judge model", "text", "", advanced=True),
        _MODEL_PROFILE,
        Field("maxNew", "New per sweep", "int", max_new),
        Field("maxActive", "Active limit", "int", max_active),
        Field("judgeMaxTurns", "Judge turns", "int", 50, advanced=True),
        Field("judgeMaxBudgetUsd", "Judge budget $", "float", 5.0, advanced=True),
        Field("maxApprovalRevisions", "Approval revisions", "int", 2, advanced=True),
        Field("verificationProfile", "Check profile", "text", "", advanced=True),
        Field(template_field, template_label, "template", "", advanced=True,
              help="runtime template stored in the sweep input; inline text or @repo/path"),
        Field("approvalJudgePromptTemplate", "Approval judge template", "template", "", advanced=True,
              help="optional read-only LLM publication-judge prompt"),
    ]
    if issues:
        fields.insert(1, Field("issueLabel", "Issue label", "text", "conductor:auto"))
    return tuple(fields)


# --------------------------------------------------------------------------- the catalog

CATALOG: dict[str, WorkflowSpec] = {
    "pr_review_sweep": WorkflowSpec(
        name="pr_review_sweep", action="Sweep new PR revisions",
        blurb="Claim and dispatch up to five previously unseen PR head revisions.",
        fields=_automation_fields(5, 5, template_field="reviewPromptTemplate",
                                  template_label="Review prompt template"),
        target=lambda i: short_repo(i.get("repo", "")), result=_r_sweep,
    ),
    "pr_address_sweep": WorkflowSpec(
        name="pr_address_sweep", action="Sweep new PR feedback",
        blurb="Claim changed feedback on harness-created PRs and dispatch fixes.",
        fields=_automation_fields(2, 2, template_field="fixPromptTemplate",
                                  template_label="Feedback-fix prompt template"),
        target=lambda i: short_repo(i.get("repo", "")), result=_r_sweep,
    ),
    "issue_resolution_sweep": WorkflowSpec(
        name="issue_resolution_sweep", action="Sweep labeled issues",
        blurb="Claim labeled open issues with no linked PR and dispatch resolutions.",
        fields=_automation_fields(1, 1, issues=True, template_field="codePromptTemplate",
                                  template_label="Issue coding prompt template"),
        target=lambda i: short_repo(i.get("repo", "")), result=_r_sweep,
    ),
    "local_review": WorkflowSpec(
        name="local_review",
        action="Review local changes",
        blurb="Read-only review of a checked-out folder against a remote branch; nothing is committed, pushed, or posted.",
        fields=(
            Field("repoPath", "Repo path", "text", help="local checked-out git repository"),
            Field("baseRemote", "Remote", "text", "origin",
                  help="configured remote to fetch for the comparison baseline"),
            Field("baseBranch", "Base branch", "text", "main",
                  help="remote branch to compare the local checkout against"),
            Field("agent", "Backend", "enum", "", choices=("", *_BACKENDS)),
            Field("model", "Model", "text", "", advanced=True, help="empty = backend default"),
            _MODEL_PROFILE,
            Field("localReviewPromptTemplate", "Prompt template", "template", "", advanced=True,
                  help="override the local review prompt; inline text or @repo/path; blank = built-in (or .conductor/local_review.md)"),
            *_caps(250, 50.0),
        ),
        target=_t_local,
        result=_r_local_review,
    ),
    "pr_review": WorkflowSpec(
        name="pr_review",
        action="Review a pull request",
        blurb="Read the PR and post a formal review (inline comments + verdict; never approves).",
        fields=(
            Field("repo", "Repo", "text", help="URL or owner/name"),
            Field("prNumber", "PR", "gh_pr", help="pull request number"),
            Field("agent", "Backend", "enum", "", choices=("", *_BACKENDS)),
            Field("approve", "Review before posting", "bool", False, tui_default=True,
                  help="pause to review/edit the drafted comments before they post"),
            Field("model", "Model", "text", "", advanced=True, help="empty = backend default"),
            _MODEL_PROFILE,
            Field("reviewPromptTemplate", "Prompt template", "template", "", advanced=True,
                  help="override the review prompt; inline text or @repo/path; blank = built-in (or commit .conductor/pr_review.md)"),
            *_caps(250, 50.0),
        ),
        target=_t_pr,
        result=_r_pr_review,
    ),
    "issue_to_pr": WorkflowSpec(
        name="issue_to_pr",
        action="Resolve a GitHub issue into a PR",
        blurb="Fetch the issue, code the fix in parallel, push a branch, open a PR that closes it.",
        fields=(
            Field("repo", "Repo", "text", help="URL or owner/name"),
            Field("issueNumber", "Issue", "gh_issue", help="issue number"),
            Field("base", "Base branch", "text", "main"),
            Field("backend", "Backend", "enum", "claude", choices=_BACKENDS,
                  maps_to=("openspecPlanAgent", "codeAgent"), help="plan + code backend"),
            Field("openspecHumanApproval", "Human plan review", "bool", True,
                  help="pause after every OpenSpec plan pass; off = read-only coding-agent judge"),
            Field("approvePr", "Review before opening", "bool", False, tui_default=True,
                  help="pause to review/edit the drafted PR before anything hits the remote"),
            Field("openspecMaxIterations", "Plan iterations", "int", 5, advanced=True),
            _MODEL_PROFILE,
            Field("codePromptTemplate", "Coding prompt template", "template", "", advanced=True,
                  help="override the per-subtask coding prompt; inline text or @repo/path; blank = built-in (or .conductor/code.md)"),
            *_caps(300, 50.0),
        ),
        target=_t_issue,
        result=_r_issue_to_pr,
    ),
    "address_pr": WorkflowSpec(
        name="address_pr",
        action="Address review feedback on a PR",
        blurb="Consolidate the PR's comments, make the changes, push to the same branch.",
        fields=(
            Field("repo", "Repo", "text", help="URL or owner/name"),
            Field("prNumber", "PR", "gh_pr", help="pull request number"),
            Field("engine", "Engine", "enum", "code_parallel",
                  choices=("code_parallel", "coding_agent"),
                  help="code_parallel = decompose+parallel; coding_agent = single session"),
            Field("agent", "Backend", "enum", "claude", choices=_BACKENDS),
            _MODEL_PROFILE,
            Field("openspecHumanApproval", "Human plan review", "bool", True,
                  help="pause after every OpenSpec plan pass (code_parallel engine only); "
                       "off = read-only coding-agent judge"),
            Field("fixPromptTemplate", "Prompt template", "template", "", advanced=True,
                  help="override the coding prompt; inline text or @repo/path; blank = built-in (or .conductor/code.md)"),
            Field("openspecMaxIterations", "Plan iterations", "int", 5, advanced=True),
            *_caps(250, 50.0),
        ),
        target=_t_pr,
        result=_r_address_pr,
    ),
    "code_parallel": WorkflowSpec(
        name="code_parallel",
        action="Code a change in a local repo",
        blurb="Decompose one instruction, code the parts in parallel worktrees, merge back.",
        fields=(
            Field("repoPath", "Repo path", "text", help="local directory"),
            Field("instruction", "Instruction", "multiline", help="the coding goal"),
            Field("changeBranch", "Change branch", "text", "code-parallel"),
            Field("backend", "Backend", "enum", "claude", choices=_BACKENDS,
                  maps_to=("openspecPlanAgent", "codeAgent"), help="plan + code backend"),
            Field("openspecHumanApproval", "Human plan review", "bool", True,
                  help="pause after every OpenSpec plan pass; off = read-only coding-agent judge"),
            Field("openspecPlanModel", "Plan model", "text", "", advanced=True),
            _MODEL_PROFILE,
            Field("codeModel", "Code model", "text", "", advanced=True),
            Field("openspecMaxIterations", "Plan iterations", "int", 5, advanced=True),
            Field("codePromptTemplate", "Coding prompt template", "template", "", advanced=True,
                  help="override the per-subtask coding prompt; inline text or @repo/path; blank = built-in (or .conductor/code.md)"),
            *_caps(500, 50.0),
        ),
        target=_t_local,
        result=_r_code_parallel,
    ),
    "feature_campaign": WorkflowSpec(
        name="feature_campaign",
        action="Run an interactive feature campaign",
        blurb="Design, approve a dependency DAG, implement in resumable waves, and verify a local branch.",
        fields=(
            Field("repoPath", "Repo path", "text", help="local directory on the worker host"),
            Field("instruction", "Instruction", "multiline", help="the complex feature goal"),
            Field("keepWorktree", "Keep worktree", "bool", True,
                  help="retain the resumable campaign workspace"),
            Field("changeBranch", "Change branch", "text", "",
                  help="blank derives feature-campaign/<workflow-id>"),
            Field("backend", "Backend", "enum", "", choices=("", *_BACKENDS),
                  maps_to=("designAgent", "planAgent", "codeAgent", "reviewAgent")),
            _MODEL_PROFILE,
            Field("designDir", "Design docs", "text", "docs/design", advanced=True),
            Field("maxTasks", "Max DAG tasks", "int", 25),
            Field("maxParallelism", "Parallelism", "int", 6),
            Field("maxWaves", "Max waves", "int", 20, advanced=True),
            Field("designMaxRevisions", "Design revisions", "int", 5, advanced=True),
            Field("planMaxRevisions", "Plan revisions", "int", 5, advanced=True),
            Field("checksConfig", "Checks config", "text", ".conductor-code/checks.json", advanced=True),
            Field("waveProfile", "Wave profile", "text", "", advanced=True),
            Field("finalProfile", "Final profile", "text", "", advanced=True),
            Field("designPromptTemplate", "Design prompt template", "template", "", advanced=True),
            Field("planPromptTemplate", "Planning prompt template", "template", "", advanced=True),
            Field("codePromptTemplate", "Coding prompt template", "template", "", advanced=True),
            Field("reviewPromptTemplate", "Review prompt template", "template", "", advanced=True),
            Field("revisionPromptTemplate", "Revision prompt template", "template", "", advanced=True),
            *_caps(500, 50.0),
        ),
        target=_t_local,
        result=_r_feature_campaign,
    ),
    "openspec_development": WorkflowSpec(
        name="openspec_development",
        action="Develop an OpenSpec change",
        blurb="Resolve an apply-ready OpenSpec change, route by complexity, verify, and archive it.",
        fields=(
            Field("repoPath", "Target repo path", "text", "",
                  help="local implementation repository; blank only when using the local spec checkout"),
            Field("specSource", "Spec source", "text", help="local path, Git remote, or public HTTPS archive"),
            Field("useSpecSourceWorkspace", "Use local spec checkout", "bool", False,
                  help="create the implementation worktree from a local spec source and publish a draft PR"),
            Field("changeId", "Change ID", "text", help="OpenSpec lowercase kebab-case change id"),
            Field("keepWorktree", "Keep worktree", "bool", True,
                  help="retain the implementation workspace after verification"),
            Field("specSourceType", "Source type", "enum", "auto", choices=("auto", "local", "git", "url")),
            Field("executionMode", "Execution", "enum", "auto", choices=("auto", "parallel", "campaign")),
            Field("instruction", "Additional guidance", "multiline", ""),
            Field("backend", "Backend", "enum", "", choices=("", *_BACKENDS), maps_to=("agent",)),
            _MODEL_PROFILE,
            Field("specRef", "Spec Git ref", "text", "", advanced=True),
            Field("specPath", "OpenSpec path", "text", "", advanced=True),
            Field("specWritebackRepo", "Writeback repo", "text", "", advanced=True,
                  help="required for URL sources; external archive changes open a draft PR"),
            Field("specWritebackRef", "Writeback ref", "text", "", advanced=True),
            Field("changeBranch", "Change branch", "text", "", advanced=True),
            Field("archiveBranch", "Archive branch", "text", "", advanced=True),
            Field("base", "Archive PR base", "text", "main", advanced=True),
            Field("model", "Model", "text", "", advanced=True),
            Field("maxTasks", "Max DAG tasks", "int", 25, advanced=True),
            Field("maxParallelism", "Parallelism", "int", 6),
            Field("maxWaves", "Max waves", "int", 20, advanced=True),
            Field("checksConfig", "Checks config", "text", ".conductor-code/checks.json", advanced=True),
            Field("finalProfile", "Final profile", "text", "", advanced=True),
            Field("assessPromptTemplate", "Assessment prompt template", "template", "", advanced=True),
            Field("codePromptTemplate", "Coding prompt template", "template", "", advanced=True),
            Field("reviewPromptTemplate", "Review prompt template", "template", "", advanced=True),
            Field("verificationPromptTemplate", "Verification prompt template", "template", "", advanced=True),
            *_caps(500, 50.0),
        ),
        target=_t_openspec,
        result=_r_openspec,
    ),
}

# Launcher order (github_demo intentionally excluded — plumbing smoke test, UX decision 4).
LAUNCHABLE = ["local_review", "pr_review", "issue_to_pr", "address_pr", "pr_review_sweep", "pr_address_sweep", "issue_resolution_sweep", "openspec_development", "feature_campaign", "code_parallel"]

# Workflow types the dashboard lists (all user-facing runs, incl. github_demo which still
# appears once run via the CLI).
DASHBOARD_TYPES = ["local_review", "pr_review", "issue_to_pr", "address_pr", "pr_review_sweep", "pr_address_sweep", "issue_resolution_sweep", "automation_dispatch", "automation_reset", "openspec_development", "feature_campaign", "code_parallel", "github_demo"]


def target_for(workflow: str, input_data: dict) -> str:
    """Dashboard target string for any run (falls back to a repo/path guess)."""
    spec = CATALOG.get(workflow)
    if spec:
        try:
            return spec.target(input_data or {})
        except Exception:  # noqa: BLE001 — never let a bad input break the row
            pass
    i = input_data or {}
    return (short_repo(i["repo"]) if i.get("repo") else None) or i.get("repoPath") \
        or i.get("repoUrl") or "—"


def result_for(workflow: str, output_data: dict) -> ResultCard | None:
    spec = CATALOG.get(workflow)
    if not spec:
        return None
    try:
        return spec.result(output_data or {})
    except Exception:  # noqa: BLE001
        return None
