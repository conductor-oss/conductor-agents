"""Salvage a structured output the agent submitted but the CLI's validator rejected.

A session that must return JSON (``output_format: json_schema``) ends ``error_max_structured_output_retries``
after five rejected StructuredOutput calls, and the task fails although the agent wrote the answer. Two
mangled shapes were seen live (runs a062184f and a33d7061, the retrospective and the program planner):

- the whole payload emitted as tagged text inside ONE property's string —
  ``{"summary": "...</summary>\\n<subtasks>[{...}]</subtasks>"}`` — so the required property is "missing";
- the raw tool input the CLI could not parse — ``{"__unparsedToolInput": {"raw": "{...}"}}``.

``salvage`` untangles each submitted payload (the runner keeps them whole as ``structured_attempts``) and
returns the newest one that validates against the schema, so the session's answer is kept instead of the
task failing or the agent sending a placeholder to get past the validator.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _mapping(v) -> dict:
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def _parse(text: str, prop_schema: dict) -> Any:
    """A tagged segment's value, typed by the property's schema."""
    text = text.strip()
    kind = prop_schema.get("type")
    if kind in ("array", "object", "number", "integer", "boolean") or (kind is None and text[:1] in "[{"):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def untangle(payload, schema) -> dict:
    """The payload with any property emitted as tagged text restored to a real property, and an
    unparsed raw tool input parsed. A payload needing neither is returned unchanged."""
    x = _mapping(payload)
    props = _mapping(_mapping(schema).get("properties"))
    unparsed = _mapping(x.get("__unparsedToolInput"))
    if unparsed and isinstance(unparsed.get("raw"), str):
        try:
            parsed = json.loads(unparsed["raw"])
        except ValueError:
            return x
        return _mapping(parsed) if isinstance(parsed, dict) else x
    names = [n for n in props]
    out = dict(x)
    for key, val in x.items():
        if not isinstance(val, str) or "<" not in val:
            continue
        found: dict[str, Any] = {}
        for name in names:
            if name == key or name in x:
                continue
            m = re.search(rf"<{re.escape(name)}>(.*?)(?:</{re.escape(name)}>|$)", val, flags=re.S)
            if m:
                found[name] = _parse(m.group(1), _mapping(props.get(name)))
        if not found:
            continue
        head = re.split(r"\s*</" + re.escape(key) + r">|\s*<(?:" + "|".join(re.escape(n) for n in found) + r")>",
                        val, maxsplit=1)[0]
        out[key] = _parse(re.sub(rf"^\s*<{re.escape(key)}>\s*", "", head), _mapping(props.get(key)))
        out.update(found)
    return out


def validates(obj, schema) -> bool:
    try:
        import jsonschema
    except ImportError:  # the shape checks that matter without the library
        s = _mapping(schema)
        return isinstance(obj, dict) and all(k in obj for k in s.get("required") or [])
    try:
        jsonschema.validate(obj, _mapping(schema))
        return True
    except jsonschema.ValidationError:
        return False


def salvage(attempts: list, schema) -> dict | None:
    """The newest submitted payload that, untangled, satisfies the schema; None when none does."""
    for raw in reversed(list(attempts or [])):
        fixed = untangle(raw, schema)
        if fixed and validates(fixed, schema):
            return fixed
    return None


PLACEHOLDER_RATIO = 4


def prefer_substantive(accepted, attempts: list, schema) -> dict | None:
    """The payload the validator rejected that the agent actually meant, when the one it accepted is a
    placeholder sent to get past it. The program planner wrote a six-subsystem plan twice as tagged text
    inside 'summary' (rejected: no 'subtasks' property), then sent ``{"subtasks": [{"id": "design",
    "description": "test", "acceptanceCriteria": ["ok"]}]}``, which validated; the program built one
    document and reported itself delivered (run b8dc259b). A rejected payload that untangles into a
    schema-valid one at least ``PLACEHOLDER_RATIO`` times the size of the accepted one is that answer;
    None when the accepted payload stands."""
    base = json.dumps(_mapping(accepted), sort_keys=True, default=str)
    best, best_len = None, 0
    for raw in attempts or []:
        if _mapping(raw) == _mapping(accepted):
            continue
        fixed = untangle(raw, schema)
        if not fixed or fixed == _mapping(accepted) or not validates(fixed, schema):
            continue
        size = len(json.dumps(fixed, sort_keys=True, default=str))
        if size > best_len:
            best, best_len = fixed, size
    if best is not None and best_len >= PLACEHOLDER_RATIO * len(base):
        return best
    return None
