"""TUI-side model profile discovery and durable launch snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProfileError(ValueError):
    pass


DEFAULT_MODEL_PROFILES = {
    "version": 1,
    "name": "Model profiles",
    "description": "Editable current Anthropic and OpenAI model profiles seeded by the Conductor TUI.",
    "defaultProfile": "anthropic-standard",
    "profiles": {
        "anthropic-standard": {"roles": {"design": {"agent": "claude", "model": "claude-opus-4-8"}, "plan": {"agent": "claude", "model": "claude-opus-4-8"}, "code": {"tiers": [{"agent": "claude", "model": "claude-sonnet-5"}, {"agent": "claude", "model": "claude-opus-4-8"}]}, "scribe": {"agent": "claude", "model": "claude-haiku-4-5"}}},
        "openai-standard": {"roles": {"design": {"agent": "codex", "model": "gpt-5.6-sol"}, "plan": {"agent": "codex", "model": "gpt-5.6-sol"}, "code": {"tiers": [{"agent": "codex", "model": "gpt-5.6-terra"}, {"agent": "codex", "model": "gpt-5.6-sol"}]}, "review": {"agent": "codex", "model": "gpt-5.6-terra"}, "judge": {"agent": "codex", "model": "gpt-5.6-terra"}, "scribe": {"agent": "codex", "model": "gpt-5.6-luna"}}}
    },
    "prices": {"claude-opus-4-8": {"input": 5, "output": 25}, "claude-sonnet-5": {"input": 3, "output": 15}, "claude-haiku-4-5": {"input": 1, "output": 5}, "gpt-5.6-sol": {"input": 5, "output": 30}, "gpt-5.6-terra": {"input": 2.5, "output": 15}, "gpt-5.6-luna": {"input": 1, "output": 6}}
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def profile_root() -> Path:
    return Path(os.environ.get("CONDUCTOR_HARNESS_HOME", "~/.conductor-harness")).expanduser() / "model-profiles"


def bootstrap_profiles(root: Path | None = None) -> Path:
    """Create the user-owned directory and a non-destructive editable starter policy."""
    root = root or profile_root()
    root.mkdir(parents=True, exist_ok=True)
    starter = root / "models.json"
    if not starter.exists(): write_profile(starter, DEFAULT_MODEL_PROFILES)
    return starter


@dataclass(frozen=True)
class Profile:
    path: Path
    data: dict

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.path.stem)

    @property
    def label(self) -> str:
        return f"{self.name} / {self.data.get('defaultProfile', 'standard')}"

    @property
    def sha256(self) -> str:
        return _hash(self.data)


def validate_profile(data: Any) -> None:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ProfileError("model profile must be a version 1 JSON object")
    forbidden = {"commands", "env", "environment", "credentials", "secrets"} & set(data)
    if forbidden:
        raise ProfileError("model profiles are declarative; forbidden fields: " + ", ".join(sorted(forbidden)))
    if not isinstance(data.get("profiles", {}), dict):
        raise ProfileError("profiles must be an object")


def load_profiles(root: Path | None = None) -> list[Profile]:
    root = root or profile_root()
    path = root / "models.json"
    if not path.exists(): return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_profile(data)
        return [Profile(path, data)]
    except (OSError, json.JSONDecodeError, ProfileError) as exc:
        raise ProfileError(f"invalid user model profile {path}: {exc}") from exc


def _matches(values: Any, candidate: str) -> bool:
    values = values if isinstance(values, list) else [values] if values else []
    return any(str(value) == candidate for value in values)


def choose_profile(workflow: str, repo: str = "", *, explicit: str = "", profiles: list[Profile] | None = None) -> Profile | None:
    profiles = profiles if profiles is not None else load_profiles()
    if explicit:
        found = [p for p in profiles if explicit in p.data.get("profiles", {}) or p.name == explicit]
        if len(found) != 1:
            raise ProfileError(f"model profile selection {explicit!r} was not found uniquely")
        return found[0]
    scored: list[tuple[int, Profile]] = []
    for profile in profiles:
        wf, rp = _matches(profile.data.get("workflows"), workflow), _matches(profile.data.get("repos"), repo)
        if wf or rp:
            scored.append(((2 if rp else 0) + (1 if wf else 0), profile))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    winners = [profile for score, profile in scored if score == best]
    if len(winners) != 1:
        raise ProfileError("ambiguous equally-specific model policies; choose one explicitly")
    return winners[0]


def snapshot_inputs(profile: Profile | None, *, profile_variant: str = "") -> dict:
    if profile is None:
        return {"modelProfile": profile_variant or "", "modelPolicy": {}, "modelPolicySource": ""}
    return {"modelProfile": profile_variant or str(profile.data.get("defaultProfile") or "standard"), "modelPolicy": profile.data,
            "modelPolicySource": f"user:{profile.path}", "modelPolicySha256": profile.sha256}


def write_profile(path: Path, data: dict) -> None:
    validate_profile(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def replace_repository_policy(worktree: str, data: dict) -> Path:
    validate_profile(data)
    root = Path(worktree).resolve()
    if not (root / ".git").exists():
        raise ProfileError("repository policy editing requires a validated local checkout")
    target = root / ".conductor-code" / "models.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="models.", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, target)
    except Exception:
        try: os.unlink(temporary)
        except OSError: pass
        raise
    return target
