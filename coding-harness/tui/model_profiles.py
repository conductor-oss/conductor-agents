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
        # The TUI owns one user policy (/models).  Its default profile is the
        # policy for every TUI-originated run unless an explicit or scoped
        # selection above wins.  Keeping the complete policy as an inline
        # snapshot makes queued runs and schedules immune to later edits.
        return profiles[0] if len(profiles) == 1 else None
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


def apply_profile_snapshot(workflow: str, inputs: dict, *, repo: str = "",
                           profiles: list[Profile] | None = None) -> dict:
    """Attach the selected user-policy snapshot without flattening its roles.

    An explicit profile wins over scoped selection; a scoped selection wins over
    the single global /models policy.  Existing envelope values are retained so
    re-runs and schedule edits preserve their original durable snapshot.
    """
    result = dict(inputs)
    explicit = str(result.get("modelProfile") or "").strip()
    target_repo = repo or str(result.get("repo") or result.get("repoPath") or "")
    profile = choose_profile(workflow, target_repo, explicit=explicit, profiles=profiles)
    if profile is None:
        return result
    variant = explicit if explicit in profile.data.get("profiles", {}) else str(
        profile.data.get("defaultProfile") or "standard")
    for key, value in snapshot_inputs(profile, profile_variant=variant).items():
        if not result.get(key):
            result[key] = value
    return result


def snapshot_summary(inputs: dict) -> str:
    """Short operator-facing provenance for a launch or schedule confirmation."""
    profile = str(inputs.get("modelProfile") or "default")
    source = str(inputs.get("modelPolicySource") or "bundled")
    return f"model profile: {profile} ({source})"


def write_profile(path: Path, data: dict) -> None:
    validate_profile(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ─────────────────────────── editing (TUI /models screen and the web console) ───────────────────────────
def _worker_validate(data: dict) -> None:
    """The workers' own policy validator (roles, backends, backend/model agreement), so a policy the
    editor accepts is one every workflow accepts; the TUI's lighter checks run too."""
    import sys
    workers = Path(__file__).resolve().parents[1] / "workers"
    if str(workers) not in sys.path:
        sys.path.insert(0, str(workers))
    try:
        from common.model_policy import ModelPolicyError, validate_policy
    except Exception:  # noqa: BLE001 - without the workers checkout only the TUI checks apply
        return
    try:
        validate_policy(data)
    except ModelPolicyError as exc:
        raise ProfileError(str(exc)) from exc


def check_policy(data: Any) -> list[str]:
    """Every problem with a policy, in words, or [] when it can be saved."""
    problems = []
    try:
        validate_profile(data)
        _worker_validate(data)
    except ProfileError as exc:
        problems.append(str(exc))
    if not problems:
        names = set((data.get("profiles") or {}))
        default = str(data.get("defaultProfile") or "")
        if names and default not in names:
            problems.append(f"defaultProfile {default!r} is not one of the profiles: {', '.join(sorted(names))}")
    return problems


def user_policy(root: Path | None = None) -> dict:
    """The one user-owned policy (seeded on first use): path, data, sha and any problem with it."""
    path = bootstrap_profiles(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(path), "data": None, "sha256": "", "problems": [f"{path} is not valid JSON: {exc}"],
                "text": path.read_text(encoding="utf-8") if path.exists() else ""}
    return {"path": str(path), "data": data, "sha256": _hash(data), "problems": check_policy(data)}


def save_user_policy(data: Any, *, expected_sha256: str = "", root: Path | None = None) -> dict:
    """Validate and write the user policy atomically. ``expected_sha256`` (what the editor loaded)
    refuses a save over a policy someone changed meanwhile, instead of silently discarding it."""
    problems = check_policy(data)
    if problems:
        raise ProfileError("; ".join(problems))
    current = user_policy(root)
    if expected_sha256 and current["sha256"] and current["sha256"] != expected_sha256:
        raise ProfileError("the model policy changed since you opened it; reload and apply your edit again")
    path = Path(current["path"])
    fd, temporary = tempfile.mkstemp(prefix="models.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, path)
    except Exception:
        try: os.unlink(temporary)
        except OSError: pass
        raise
    return user_policy(root)


def role_rows(data: dict) -> list[dict]:
    """A flat view of a policy for editors: one row per profile × role × tier."""
    rows = []
    for name, profile in sorted((data.get("profiles") or {}).items()):
        for role, cfg in sorted(((profile or {}).get("roles") or {}).items()):
            tiers = cfg.get("tiers") if isinstance(cfg.get("tiers"), list) else None
            for i, tier in enumerate(tiers if tiers is not None else [cfg]):
                rows.append({"profile": name, "role": role, "tier": i if tiers is not None else None,
                             "agent": str(tier.get("agent") or ""), "model": str(tier.get("model") or "")})
    return rows


def set_role(data: dict, profile: str, role: str, tier: int | None, *, agent: str, model: str) -> dict:
    """A copy of ``data`` with one role (or one tier of it) set to ``agent``/``model``."""
    out = json.loads(json.dumps(data))
    cfg = out.setdefault("profiles", {}).setdefault(profile, {}).setdefault("roles", {}).setdefault(role, {})
    if tier is None:
        cfg.update({"agent": agent, "model": model})
    else:
        tiers = cfg.setdefault("tiers", [])
        while len(tiers) <= tier:
            tiers.append({})
        tiers[tier] = {**tiers[tier], "agent": agent, "model": model}
    return out
