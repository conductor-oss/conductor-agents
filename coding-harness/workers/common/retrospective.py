"""Run retrospectives → learnings that later runs use (pure logic + thin board I/O).

At the end of every run — delivered, incomplete, failed or terminated — an agent reviews what actually
happened and records durable learnings. Later runs read the relevant ones before planning and inject
them into every implementer's prompt, so the swarm stops repeating the same mistakes.

Learnings are scoped so each lands where it applies:

- ``repo``        — this codebase (keyed by its origin URL, else the main checkout's real path)
- ``environment`` — this host's toolchain (keyed by host name): "JDK 25 is at /opt/jdk-25", "go 1.26"
- ``language``    — a language/framework convention (keyed by language): "Go: std-lib only, go test -race"
- ``process``     — how the team should work (global): what failed in the workflow and the adjustment

They live on the shared blackboard (``LEARNINGS_RUN``), not on the run's branch: a failed or
terminated run's branch is never merged, but its lessons are exactly the ones worth keeping. Like
every lesson (H10/H15) each carries evidence and confidence, can be retracted, and is deduplicated.
"""

from __future__ import annotations

import json
import os
import re
import socket

from common import team_board

LEARNINGS_RUN = "_swarm_learnings"
LEARNING_KIND = "learning"
SCOPES = ("repo", "environment", "language", "process")
# A learning shorter than this names nothing "durable, specific and actionable"; it is a placeholder
# (the retrospective of run a062184f recorded {"text": "a"} after the validator rejected its real output).
MIN_LEARNING_CHARS = 10

_LANG_MARKERS = (
    ("go", ("go.mod",)),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")),
    ("typescript", ("tsconfig.json",)),
    ("javascript", ("package.json",)),
    ("python", ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")),
    ("rust", ("Cargo.toml",)),
    ("ruby", ("Gemfile",)),
    ("csharp", ("*.csproj", "*.sln")),
)


def _mapping(v) -> dict:
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _listed(v) -> list:
    return v if isinstance(v, list) else []


def detect_languages(repo: str) -> list[str]:
    """Languages present at the repository root, by their build markers."""
    try:
        names = set(os.listdir(repo))
    except OSError:
        return []
    out = []
    for lang, markers in _LANG_MARKERS:
        if any((m.startswith("*") and any(n.endswith(m[1:]) for n in names)) or m in names for m in markers):
            if lang == "javascript" and "typescript" in out:
                continue
            out.append(lang)
    return out


def repo_key(path: str) -> str:
    """A stable identity for the repository behind ``path`` (a checkout or any of its worktrees): the
    ``origin`` URL when there is one, else the main checkout's real path."""
    from common import git
    if not path or not os.path.isdir(path):
        return os.path.realpath(path or "")
    url = git.git(path, "remote", "get-url", "origin", check=False)
    if url.code == 0 and url.stdout.strip():
        return re.sub(r"\.git$", "", url.stdout.strip())
    common = git.git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if common.code == 0 and common.stdout.strip():
        return os.path.realpath(os.path.dirname(common.stdout.strip()))
    return os.path.realpath(path)


def host() -> str:
    return socket.gethostname()


# ─────────────────────────────── evidence ─────────────────────────────────────
def evidence(correlation_id: str, *, verdict: str = "", report: str = "", status: str = "") -> dict:
    """What actually happened in the run (and its child teams), compact enough for one prompt."""
    from common import blackboard as bb
    runs = [correlation_id] + [str(_mapping(f.get("data")).get("childRun"))
                               for f in bb.read_facts(correlation_id, kind="child")
                               if _mapping(f.get("data")).get("childRun")]
    teams = []
    for run in runs:
        board = team_board.fold_board(run)
        items = [{"id": it.get("id"), "state": it.get("state"), "deliverable": it.get("deliverable") or "code",
                  "attempts": it.get("attempt", 0), "reason": str(it.get("reason") or "")[:240]}
                 for it in board["items"].values()]
        escalations = [{"item": _mapping(e).get("item"), "kind": _mapping(e).get("kind"),
                        "resolved": bool(_mapping(e).get("resolved")), "by": _mapping(e).get("by"),
                        "detail": str(_mapping(e).get("detail") or "")[:300]}
                       for e in board.get("exceptions") or []]
        human = [{"target": d.get("target"), "text": str(d.get("text") or "")[:300], "by": d.get("by")}
                 for d in board.get("directives") or [] if str(d.get("by") or "").startswith("human")]
        exemptions = [{"directive": d.get("exempts"), "item": d.get("item"), "reason": str(d.get("reason") or "")[:300]}
                      for d in board.get("directives") or [] if d.get("op") == "exempt"]
        changes = [{"item": r.get("item"), "findings": [str(x)[:200] for x in _listed(r.get("findings"))][:4]}
                   for r in board.get("reviews") or [] if r.get("verdict") == "changes"]
        lessons = [{"text": str(x.get("text") or "")[:240], "kind": x.get("kind"), "by": x.get("provenance")}
                   for x in team_board.live_lessons(board.get("lessons") or [])][-12:]
        teams.append({"run": run, "items": items, "escalations": escalations[-15:], "humanFeedback": human[-15:],
                      "exemptions": exemptions, "reviewChanges": changes[-10:], "lessons": lessons})
    return {"run": correlation_id, "verdict": verdict, "status": status, "report": str(report or "")[:2500],
            "teams": teams}


def prompt_context(ev: dict, *, languages: list[str], host_name: str, prior: str = "") -> dict:
    """The values of the ``retrospective`` prompt's placeholders."""
    import json
    return {"verdict": ev.get("verdict") or ev.get("status") or "unknown",
            "languages": ", ".join(languages) or "unknown", "host": host_name,
            "priorSection": f"Prior learnings already recorded:\n{prior}\n\n" if prior else "",
            "evidence": json.dumps(ev, default=str)[:30000]}


def prompt(ev: dict, *, languages: list[str], host_name: str, prior: str = "") -> str:
    from common import templating
    return templating.render_default("retrospective", prompt_context(
        ev, languages=languages, host_name=host_name, prior=prior))


SCHEMA = {
    "type": "object",
    "properties": {
        "learnings": {"type": "array", "items": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": list(SCOPES)},
            "language": {"type": "string"},
            "text": {"type": "string"},
            "evidence": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        }, "required": ["scope", "text", "evidence"]}},
        "summary": {"type": "string", "description": "at most three sentences"},
    },
    "required": ["summary", "learnings"],
}


def untangle(payload) -> dict:
    """Recover a retrospective whose StructuredOutput call was mangled — the properties emitted as
    tagged text inside ONE string, ``{"summary": "...</summary>\\n<learnings>[...]"}``, which the
    validator rejects for lacking 'learnings' (run a062184f: 8 attempts out of 9, then a placeholder
    to get past it). The agent runner salvages such a payload itself now (``structured_output``);
    this covers a payload that reached the recorder unsalvaged."""
    from common import structured_output
    return structured_output.untangle(payload, SCHEMA)


def substantive(learnings) -> list[dict]:
    """The learnings worth recording: a known scope and a text long enough to be actionable."""
    return [x for x in (_mapping(r) for r in _listed(learnings))
            if str(x.get("scope") or "") in SCOPES and len(str(x.get("text") or "").strip()) >= MIN_LEARNING_CHARS]


def salvage(candidates: list) -> dict:
    """The retrospective to record from everything the agent produced: the accepted structured output
    first, then every StructuredOutput payload the validator rejected (``structuredAttempts``), newest
    first, each untangled. The first candidate carrying a substantive learning wins — the accepted
    output when it has one, else the newest rejected payload that does — so the placeholder the model
    sent to satisfy the validator never replaces the real retrospective it wrote before it."""
    seen: list[dict] = []
    for raw in candidates:
        x = untangle(raw)
        if not isinstance(x.get("learnings"), list):
            continue
        if substantive(x["learnings"]):
            return x
        seen.append(x)
    return seen[0] if seen else {}


# ─────────────────────────────── store ────────────────────────────────────────
def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower()))


def record(learnings: list, *, repo: str, languages: list[str], host_name: str, run: str,
           backend=None) -> list[dict]:
    """Append each learning to the shared store under its scope key; skip exact repeats."""
    from common import blackboard as bb
    existing = {(_mapping(f.get("data")).get("scope"), _mapping(f.get("data")).get("key"),
                 _norm(_mapping(f.get("data")).get("text")))
                for f in bb.read_facts(LEARNINGS_RUN, kind=LEARNING_KIND, backend=backend)}
    written = []
    for x in substantive(learnings):
        scope, text = str(x.get("scope") or "").strip(), str(x.get("text") or "").strip()
        lang = str(x.get("language") or "").strip().lower() or (languages[0] if languages else "")
        key = {"repo": repo, "environment": host_name, "language": lang, "process": "*"}[scope]
        if scope == "language" and not key:
            continue
        sig = (scope, key, _norm(text))
        if sig in existing:
            continue
        existing.add(sig)
        data = {"scope": scope, "key": key, "text": text[:600], "evidence": str(x.get("evidence") or "")[:600],
                "confidence": str(x.get("confidence") or "medium"), "run": run}
        bb.post_fact(LEARNINGS_RUN, f"retrospective:{run}", LEARNING_KIND, data, backend=backend)
        written.append(data)
    return written


def retract(text: str, *, by: str, backend=None) -> str:
    """Withdraw a learning everywhere (a later run or a human found it wrong)."""
    from common import blackboard as bb
    return bb.post_fact(LEARNINGS_RUN, by, LEARNING_KIND, {"op": "retract", "text": str(text)}, backend=backend)


def everything(*, backend=None) -> list[dict]:
    """Every live learning, newest first — retracted and duplicate texts dropped. This is the learning
    board the console shows for ``LEARNINGS_RUN``; ``relevant`` narrows it to one run's scopes."""
    from common import blackboard as bb
    facts = [_mapping(f.get("data")) | {"ts": f.get("ts")}
             for f in bb.read_facts(LEARNINGS_RUN, kind=LEARNING_KIND, backend=backend)]
    retracted = {_norm(f.get("text")) for f in facts if f.get("op") == "retract"}
    seen, out = set(), []
    for f in sorted((f for f in facts if f.get("op") != "retract"), key=lambda f: -(f.get("ts") or 0)):
        norm = _norm(f.get("text"))
        if norm in retracted or norm in seen:
            continue
        seen.add(norm)
        out.append(f)
    return out


def relevant(*, repo: str, languages: list[str], host_name: str, limit: int = 20, backend=None) -> list[dict]:
    """The learnings that apply to a run on ``repo``: this repo's, this host's, its languages', and the
    global process ones — newest first per scope, retracted and duplicate ones dropped."""
    want = {"repo": {repo}, "environment": {host_name}, "language": {lang.lower() for lang in languages},
            "process": {"*"}}
    picked = {s: [] for s in SCOPES}
    for f in everything(backend=backend):
        scope = str(f.get("scope"))
        if scope in want and str(f.get("key")) in want[scope]:
            picked[scope].append(f)
    out = []
    for scope in SCOPES:  # repo first: the most specific knowledge leads
        out.extend(picked[scope])
    return out[:limit]


def label(learning: dict) -> str:
    """One line for a board or prompt: ``[scope:key] text`` (a process learning has no key)."""
    scope = str(learning.get("scope") or "")
    key = "" if scope == "process" else str(learning.get("key") or "")
    return f"[{scope}{(':' + key) if key else ''}] {learning.get('text')}"


def digest(learnings: list) -> str:
    return "\n".join(f"- [{x.get('scope')}{(':' + str(x.get('key'))) if x.get('scope') == 'language' else ''}] "
                     f"{x.get('text')}" for x in learnings)
