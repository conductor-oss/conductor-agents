"""Keep the published workflow input contract aligned with the JSON definitions."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "workers" / "workflows"
REFERENCE = ROOT / "docs" / "workflow-inputs.md"
SHARED_OPTIONAL_INPUTS = {
    "modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256",
    "modelsConfig", "modelOverrides",
}


def _section(document: str, workflow_name: str) -> str:
    match = re.search(
        rf"^## `{re.escape(workflow_name)}`(?: \(internal\))?\n(.*?)(?=^## |\Z)",
        document,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"{workflow_name} has no input-reference section"
    return match.group(1)


def _required_fields(line: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z][A-Za-z0-9]*)`", line))


def _optional_fields(line: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z][A-Za-z0-9]*)`\s*=", line))


def test_workflow_input_reference_matches_required_and_optional_contracts():
    document = REFERENCE.read_text()
    assert "optional policy envelope is available on **every** workflow" in document
    for path in sorted(WORKFLOWS.glob("*.json")):
        workflow = json.loads(path.read_text())
        defaults = workflow.get("inputTemplate", {})
        inputs = set(workflow.get("inputParameters", []))
        required = {name for name in inputs if name not in defaults}
        optional = inputs - required

        section = _section(document, workflow["name"])
        required_line = next(line for line in section.splitlines() if line.startswith("Required:"))
        optional_line = next(line for line in section.splitlines() if "Optional:" in line)

        assert _required_fields(required_line) == required
        assert _optional_fields(optional_line) == optional - SHARED_OPTIONAL_INPUTS
        assert SHARED_OPTIONAL_INPUTS <= optional
