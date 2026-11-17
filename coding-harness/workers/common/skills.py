"""Skills for every coding agent, whatever its backend (Claude, Codex or Gemini).

A skill is a folder holding a ``SKILL.md`` (YAML front matter with ``name`` and ``description``, then
instructions) and whatever scripts or references it ships. Each CLI finds skills on its own, in its own
folders: Codex in ``~/.codex/skills`` and ``~/.agents/skills``, Gemini in ``~/.gemini/skills`` and
``~/.agents/skills``, and Claude, with only the project's settings loaded, in none. So the same run saw
different skills on different backends and different hosts.

The harness keeps one list instead:

* it finds the skills in the standard folders (the repository's own first, when its project settings
  are trusted, then ``CONDUCTOR_SKILL_DIRS``, then the user's), the first skill of a name winning;
* it keeps the ones the run asked for (``skills``: ``all``, ``none`` or names; the worker's default is
  ``CONDUCTOR_SKILLS``, else ``all``);
* it copies them into a folder named by their content (``~/.cache/conductor-harness/skills/<digest>``),
  so an agent never writes into the user's own skills and a changed skill is a new folder;
* it lists them in the prompt the same way for every backend (name, description, the SKILL.md path),
  and each backend is given read access to that folder and has its own listing turned off.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

USER_ROOTS = ("~/.agents/skills", "~/.claude/skills", "~/.codex/skills", "~/.gemini/skills")
REPO_ROOTS = (".agents/skills", ".claude/skills", ".codex/skills", ".gemini/skills")
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".DS_Store"}
MAX_SKILL_BYTES = 20 * 1024 * 1024
DESCRIPTION_CHARS = 400


def home() -> str:
    return os.environ.get("CONDUCTOR_SKILLS_HOME") or os.path.expanduser("~/.cache/conductor-harness/skills")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    source: str  # the skill's folder
    origin: str  # the folder it was found in, for the listing in a run's output


def front_matter(text: str) -> dict[str, Any]:
    """The YAML block between the leading ``---`` lines of a SKILL.md ({} when there is none)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    import yaml
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def roots(worktree: str | None = None, *, repo: bool = True) -> list[tuple[str, str]]:
    """(folder, label) in the order a name is settled: the repository's, the operator's, the user's."""
    out: list[tuple[str, str]] = []
    if repo and worktree:
        out += [(os.path.join(worktree, r), r) for r in REPO_ROOTS]
    out += [(os.path.expanduser(p), p) for p in os.environ.get("CONDUCTOR_SKILL_DIRS", "").split(os.pathsep) if p.strip()]
    out += [(os.path.expanduser(r), r) for r in USER_ROOTS]
    return out


def discover(worktree: str | None = None, *, repo: bool = True) -> list[Skill]:
    found: dict[str, Skill] = {}
    for root, label in roots(worktree, repo=repo):
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            folder = os.path.join(root, entry)
            manifest = os.path.join(folder, "SKILL.md")
            if entry.startswith(".") or not os.path.isfile(manifest):
                continue
            try:
                with open(manifest, encoding="utf-8", errors="replace") as fh:
                    meta = front_matter(fh.read())
            except OSError:
                continue
            name = str(meta.get("name") or entry).strip()
            if name and name not in found:
                found[name] = Skill(name, " ".join(str(meta.get("description") or "").split()),
                                    os.path.realpath(folder), label)
    return list(found.values())


def selection(value: Any) -> str | list[str]:
    """``"all"``, or the names asked for (``[]`` is none). Unset falls back to ``CONDUCTOR_SKILLS``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        value = os.environ.get("CONDUCTOR_SKILLS") or "all"
    if isinstance(value, str):
        text = value.strip()
        if text.lower() == "all":
            return "all"
        if text.lower() in ("none", "[]"):
            return []
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                value = text.strip("[]").split(",")
        else:
            value = text.split(",")
    names = [str(v).strip() for v in (value or []) if str(v).strip()]
    return "all" if any(n.lower() == "all" for n in names) else list(dict.fromkeys(names))


def choose(skills: list[Skill], value: Any) -> tuple[list[Skill], list[str]]:
    """The skills a run asked for, and the names asked for that no folder has."""
    sel = selection(value)
    if sel == "all":
        return skills, []
    by_name = {s.name: s for s in skills}
    return [by_name[n] for n in sel if n in by_name], [n for n in sel if n not in by_name]


def _files(folder: str) -> list[str]:
    out = []
    for base, dirs, files in os.walk(folder, followlinks=True):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        out += [os.path.relpath(os.path.join(base, f), folder) for f in sorted(files) if f not in SKIP]
    return out


def _digest(trees: list[tuple[str, str]]) -> str:
    """Content digest of (name, folder) trees: names, relative paths and bytes."""
    h = hashlib.sha256()
    for name, folder in trees:
        h.update(f"\0skill\0{name}\0".encode())
        for rel in _files(folder):
            h.update(f"\0{rel}\0".encode())
            try:
                with open(os.path.join(folder, rel), "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 16), b""):
                        h.update(chunk)
            except OSError:
                h.update(b"\0unreadable\0")
    return h.hexdigest()


def too_large(skill: Skill) -> bool:
    """Over ``MAX_SKILL_BYTES`` (without node_modules, .git and virtualenvs): left out, not copied."""
    return _size(skill.source) > MAX_SKILL_BYTES


def _size(folder: str) -> int:
    total = 0
    for rel in _files(folder):
        try:
            total += os.path.getsize(os.path.join(folder, rel))
        except OSError:
            pass
    return total


def bundle(skills: list[Skill]) -> str | None:
    """The folder holding a copy of these skills (one sub-folder per name), built once per content."""
    if not skills:
        return None
    trees = [(s.name, s.source) for s in sorted(skills, key=lambda s: s.name)]
    digest = _digest(trees)
    target = os.path.join(home(), digest[:20])
    if os.path.isdir(target):
        # An agent could have written into an earlier copy: a copy whose content no longer matches its
        # name is rebuilt.
        if _digest([(n, os.path.join(target, _folder(n))) for n, _ in trees]) == digest:
            return target
        shutil.rmtree(target, ignore_errors=True)
    os.makedirs(home(), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".build-", dir=home())
    try:
        for name, source in trees:
            shutil.copytree(source, os.path.join(tmp, _folder(name)), symlinks=False,
                            ignore=shutil.ignore_patterns(*SKIP), dirs_exist_ok=True)
        try:
            os.rename(tmp, target)
        except OSError:  # another session built the same copy first
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return target


def _folder(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in name) or "skill"


def listing(skills: list[Skill], folder: str) -> str:
    lines = []
    for s in sorted(skills, key=lambda s: s.name):
        desc = s.description if len(s.description) <= DESCRIPTION_CHARS else s.description[:DESCRIPTION_CHARS - 1] + "…"
        lines.append(f"- {s.name}: {desc or '(no description)'} (file: {os.path.join(folder, _folder(s.name), 'SKILL.md')})")
    return ("## Skills\n"
            "A skill is a folder of instructions (its SKILL.md) and the scripts or references it ships, for one "
            "kind of task. These skills are installed for you:\n" + "\n".join(lines) + "\n"
            "When a part of your task matches a skill's description, read that skill's whole SKILL.md before you "
            "do that part and follow it; paths in it are relative to the skill's folder. Leave alone the skills "
            "that do not fit your task. The skill folders are reference: never write into them. Your task's "
            "instructions and this repository's conventions come first when they disagree with a skill.")


def prepare(worktree: str | None, value: Any, *, repo: bool = True) -> dict[str, Any]:
    """Everything one session needs: ``{dir, names, missing, text, skipped}``. ``dir`` is None and
    ``text`` empty when no skill is chosen. A skill larger than ``MAX_SKILL_BYTES`` is left out and named
    in ``skipped`` rather than copied."""
    chosen, missing = choose(discover(worktree, repo=repo), value)
    kept, skipped = [], []
    for s in chosen:
        (skipped if too_large(s) else kept).append(s)
    folder = bundle(kept)
    return {"dir": folder, "names": [s.name for s in kept], "missing": missing,
            "skipped": [s.name for s in skipped], "text": listing(kept, folder) if folder else "",
            "origins": {s.name: s.origin for s in kept}}


def gemini_settings(base_path: str | None = None) -> str:
    """A Gemini system-settings file with Gemini's own skill listing off: the operator's system
    settings (``GEMINI_CLI_SYSTEM_SETTINGS_PATH``, else the platform's) with ``skills.enabled`` false."""
    base_path = base_path or os.environ.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH") or (
        "/Library/Application Support/GeminiCli/settings.json" if os.uname().sysname == "Darwin"
        else "/etc/gemini-cli/settings.json")
    data: dict[str, Any] = {}
    try:
        with open(base_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        data = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    data["skills"] = {**(data.get("skills") if isinstance(data.get("skills"), dict) else {}), "enabled": False}
    text = json.dumps(data, indent=1, sort_keys=True)
    path = os.path.join(home(), f"gemini-system-settings-{hashlib.sha256(text.encode()).hexdigest()[:12]}.json")
    if not os.path.isfile(path):
        os.makedirs(home(), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=home(), prefix=".gemini-settings-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    return path
