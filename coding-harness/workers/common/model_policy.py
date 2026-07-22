"""Declarative model-policy loading and resolution.

Policies are deliberately data-only.  This module is used by workers and the TUI;
it validates every source before any agent or git operation is started.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

ROLES = {"design", "plan", "code", "review", "judge", "scribe"}
BACKENDS = {"claude", "codex", "gemini"}
_ALLOWED = {"version", "name", "description", "workflows", "repos", "defaultProfile",
            "profiles", "roles", "reviewLoop", "budgets", "prices"}
_ROLE_ALLOWED = {"agent", "model", "tiers", "maxTurns", "maxBudgetUsd", "fallbackTiers"}


class ModelPolicyError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _merge(base: dict, extra: dict) -> dict:
    out = deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelPolicyError(f"invalid model policy {path}: {exc}") from exc
    validate_policy(data)
    return data


def validate_policy(policy: Any) -> None:
    if not isinstance(policy, dict):
        raise ModelPolicyError("model policy must be a JSON object")
    unknown = set(policy) - _ALLOWED
    if unknown:
        raise ModelPolicyError("policy contains unsupported fields: " + ", ".join(sorted(unknown)))
    if policy.get("version") != 1:
        raise ModelPolicyError("unsupported model policy version; only version 1 is supported")
    profiles = policy.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ModelPolicyError("profiles must be an object")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ModelPolicyError(f"profile {name!r} must be an object")
        _validate_roles(profile.get("roles", {}))
    _validate_roles(policy.get("roles", {}))


def _validate_roles(roles: Any) -> None:
    if not isinstance(roles, dict):
        raise ModelPolicyError("roles must be an object")
    for role, cfg in roles.items():
        if role not in ROLES:
            raise ModelPolicyError(f"unsupported role: {role}")
        if not isinstance(cfg, dict) or set(cfg) - _ROLE_ALLOWED:
            raise ModelPolicyError(f"invalid declarative role configuration for {role}")
        for tier in cfg.get("tiers", []):
            if not isinstance(tier, dict) or set(tier) - _ROLE_ALLOWED - {"priceKey"}:
                raise ModelPolicyError(f"invalid tier for {role}")
            _validate_backend(tier.get("agent"), tier.get("model"))
        _validate_backend(cfg.get("agent"), cfg.get("model"))


def _backend_for_model(model: str) -> str | None:
    m = model.lower()
    if m.startswith(("claude-", "claude_")):
        return "claude"
    if m.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "codex"
    if m.startswith("gemini"):
        return "gemini"
    return None


def _validate_backend(agent: Any, model: Any) -> None:
    if agent not in (None, "") and str(agent).lower() not in BACKENDS:
        raise ModelPolicyError(f"unsupported backend: {agent}")
    inferred = _backend_for_model(str(model or ""))
    if agent and inferred and str(agent).lower() != inferred:
        raise ModelPolicyError(
            f"backend/model mismatch: backend {agent!r} cannot run model {model!r}; "
            f"use backend {inferred!r} or choose a compatible model")


def normalized_models_config(worktree: str, value: str) -> Path:
    raw = str(value or "").strip()
    if not raw or os.path.isabs(raw):
        raise ModelPolicyError("modelsConfig must be a non-empty relative path inside the worktree")
    root = Path(worktree).resolve()
    path = (root / raw).resolve()
    if path != root and root not in path.parents:
        raise ModelPolicyError("modelsConfig escapes the worktree")
    return path


def backend_availability(agent: str) -> str:
    """Presence/CLI probe only; this intentionally never claims authentication works."""
    agent = (agent or "").lower()
    if agent == "claude":
        return "available" if (os.environ.get("ANTHROPIC_API_KEY") or shutil.which("claude")) else "unknown"
    if agent == "gemini":
        return "available" if (os.environ.get("GEMINI_API_KEY") or shutil.which("gemini")) else "unknown"
    if agent == "codex":
        if shutil.which("codex") or os.path.exists(os.path.expanduser("~/.codex/auth.json")):
            return "available"
        return "unknown"
    return "unavailable"


def _legacy_roles(inputs: dict) -> dict:
    mapping = {"design": ("designAgent", "designModel"), "plan": ("planAgent", "planModel"),
               "code": ("codeAgent", "codeModel"), "review": ("agent", "model"),
               "judge": ("judgeAgent", "judgeModel"), "scribe": ("scribeAgent", "scribeModel")}
    result = {}
    for role, (agent_key, model_key) in mapping.items():
        agent, model = str(inputs.get(agent_key) or "").strip(), str(inputs.get(model_key) or "").strip()
        if agent or model:
            _validate_backend(agent, model)
            result[role] = {k: v for k, v in (("agent", agent), ("model", model)) if v}
    # CLI callers may set one generic model without learning every role-specific
    # field. It deliberately applies to producer roles only; reviewers retain the
    # policy's diversity unless explicitly overridden.
    generic_model = str(inputs.get("model") or "").strip()
    generic_agent = str(inputs.get("agent") or _backend_for_model(generic_model) or "").strip()
    if generic_model:
        _validate_backend(generic_agent, generic_model)
        for role in ("plan", "code"):
            result[role] = {"agent": generic_agent, "model": generic_model}
    return result


def resolve_model_policy(inputs: dict | None = None, *, worktree: str | None = None) -> dict:
    inputs = inputs or {}
    here = Path(__file__).resolve().parents[1]
    bundled_path = here / "defaults" / "models.defaults.json"
    bundled = _read(bundled_path)
    merged = bundled
    sources = [{"source": "bundled", "path": str(bundled_path), "sha256": canonical_hash(bundled)}]
    if worktree:
        repo_path = Path(worktree).resolve() / ".conductor-code" / "models.json"
        config_value = inputs.get("modelsConfig")
        if config_value:
            repo_path = normalized_models_config(str(worktree), str(config_value))
        if repo_path.exists():
            policy = _read(repo_path)
            merged = _merge(merged, policy)
            sources.append({"source": "repository", "path": str(repo_path), "sha256": canonical_hash(policy)})
    inline = inputs.get("modelPolicy")
    if inline:
        if isinstance(inline, str):
            try: inline = json.loads(inline)
            except json.JSONDecodeError as exc: raise ModelPolicyError(f"invalid inline modelPolicy: {exc}") from exc
        validate_policy(inline)
        requested_hash = str(inputs.get("modelPolicySha256") or "").strip()
        actual_hash = canonical_hash(inline)
        if requested_hash and requested_hash != actual_hash:
            raise ModelPolicyError("modelPolicySha256 does not match the supplied modelPolicy")
        merged = _merge(merged, inline)
        sources.append({"source": str(inputs.get("modelPolicySource") or "inline"), "sha256": actual_hash})
    selected = str(inputs.get("modelProfile") or merged.get("defaultProfile") or "standard")
    if selected not in merged.get("profiles", {}):
        raise ModelPolicyError(f"unknown model profile {selected!r}")
    resolved = _merge({"roles": merged.get("roles", {}), "reviewLoop": merged.get("reviewLoop", {}),
                       "prices": merged.get("prices", {})}, merged["profiles"][selected])
    resolved["roles"] = _merge(resolved.get("roles", {}), _legacy_roles(inputs))
    overrides = inputs.get("modelOverrides") or {}
    if isinstance(overrides, str):
        try: overrides = json.loads(overrides)
        except json.JSONDecodeError as exc: raise ModelPolicyError(f"invalid modelOverrides: {exc}") from exc
    _validate_roles(overrides)
    resolved["roles"] = _merge(resolved["roles"], overrides)
    global_turns = inputs.get("maxTurns")
    global_budget = inputs.get("maxBudgetUsd")
    roles = {}
    warnings = []
    for role in sorted(ROLES):
        cfg = deepcopy(resolved["roles"].get(role, {}))
        tiers = cfg.pop("tiers", None) or [cfg]
        normal = []
        for tier in tiers:
            tier = deepcopy(tier)
            agent = str(tier.get("agent") or "claude").lower()
            model = str(tier.get("model") or "")
            _validate_backend(agent, model)
            if global_turns not in (None, ""):
                tier["maxTurns"] = min(int(global_turns), int(tier.get("maxTurns") or global_turns))
            if global_budget not in (None, ""):
                tier["maxBudgetUsd"] = min(float(global_budget), float(tier.get("maxBudgetUsd") or global_budget))
            tier["agent"], tier["model"] = agent, model
            tier["priceKey"] = tier.get("priceKey") or ("codex:default" if agent == "codex" and not model else model)
            tier["availability"] = backend_availability(agent)
            normal.append(tier)
        if role in {"review", "judge"} and normal and normal[0]["agent"] == "codex" and normal[0]["availability"] != "available":
            if not cfg.get("fallbackTiers"):
                warnings.append(f"{role}: Codex availability is {normal[0]['availability']}; falling back to Opus reduces reviewer diversity")
                normal.append({"agent": "claude", "model": "claude-opus-4-8", "priceKey": "claude-opus-4-8", "availability": backend_availability("claude"), "fallback": True})
        roles[role] = {"tiers": normal}
    return {"version": 1, "profile": selected, "roles": roles, "reviewLoop": resolved.get("reviewLoop", {}),
            "prices": resolved.get("prices", {}), "sources": sources, "warnings": warnings,
            "canonicalSha256": canonical_hash({"profile": selected, "roles": roles, "reviewLoop": resolved.get("reviewLoop", {})})}


def select_role_tier(inputs: dict, *, role: str, worktree: str | None = None) -> tuple[dict, dict]:
    """Return one validated effective tier and its complete resolution.

    A workflow normally supplies ``modelResolution`` from the preflight task.  Directly
    started internal workflows may omit it; resolving here keeps the worker fail-closed.
    Task-local nonblank ``agent`` / ``model`` values are explicit overrides for this
    one role, while blank values retain the selected policy tier.
    """
    if role not in ROLES:
        raise ModelPolicyError(f"unsupported model role: {role}")
    resolution = inputs.get("modelResolution")
    # The workflow preflight may run before a remote repository has been cloned.
    # Once a coding task owns its worktree, resolve again when that checkout carries
    # a repository policy (or an explicit modelsConfig).  This preserves the
    # immutable inline snapshot while allowing the checked-out revision's policy to
    # participate in the documented precedence chain.
    repository_policy = False
    if worktree:
        try:
            root = Path(worktree).resolve()
            repository_policy = bool(inputs.get("modelsConfig")) or (root / ".conductor-code" / "models.json").is_file()
        except OSError:
            repository_policy = bool(inputs.get("modelsConfig"))
    if (not isinstance(resolution, dict) or not isinstance(resolution.get("roles"), dict)
            or repository_policy):
        resolution = resolve_model_policy(inputs, worktree=worktree)
    tiers = ((resolution.get("roles", {}).get(role) or {}).get("tiers") or [])
    if not tiers or not isinstance(tiers[0], dict):
        raise ModelPolicyError(f"model resolution has no tier for role {role!r}")
    tier = deepcopy(tiers[0])
    agent = str(inputs.get("agent") or tier.get("agent") or "").strip().lower()
    model = str(inputs.get("model") or tier.get("model") or "").strip()
    _validate_backend(agent, model)
    if not agent:
        raise ModelPolicyError(f"model resolution has no backend for role {role!r}")
    tier["agent"], tier["model"] = agent, model
    for field, caster in (("maxTurns", int), ("maxBudgetUsd", float)):
        explicit = inputs.get(field)
        policy_limit = tier.get(field)
        if explicit not in (None, "") and policy_limit not in (None, ""):
            tier[field] = min(caster(explicit), caster(policy_limit))
        elif explicit not in (None, ""):
            tier[field] = caster(explicit)
    return tier, resolution
