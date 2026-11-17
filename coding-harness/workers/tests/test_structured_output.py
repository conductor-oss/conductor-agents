"""Salvaging a rejected structured output (runs a062184f, a33d7061): the agent wrote the answer, the
CLI's validator rejected its mangled tool input, and the task failed or recorded a placeholder."""
import common  # noqa: F401
from common import structured_output as so

PLAN_SCHEMA = {"type": "object", "properties": {
    "subtasks": {"type": "array", "items": {"type": "object"}},
    "contractPaths": {"type": "array", "items": {"type": "string"}},
    "summary": {"type": "string"}}, "required": ["subtasks"]}

RETRO_SCHEMA = {"type": "object", "properties": {
    "learnings": {"type": "array", "items": {"type": "object", "properties": {
        "scope": {"type": "string", "enum": ["repo", "process"]}, "text": {"type": "string"}},
        "required": ["scope", "text"]}},
    "summary": {"type": "string"}}, "required": ["summary", "learnings"]}


def test_untangles_properties_emitted_as_tagged_text_inside_one_string():
    tagged = ('Five subsystems in a DAG rooted at design-docs.</summary>\n'
              '<subtasks>[{"id": "design-docs", "files": ["docs/design/architecture.md"]}, {"id": "resp", "dependsOn": ["design-docs"]}]</subtasks>\n'
              '<contractPaths>["go.mod"]</contractPaths>')
    fixed = so.untangle({"summary": tagged}, PLAN_SCHEMA)
    assert fixed["summary"] == "Five subsystems in a DAG rooted at design-docs."
    assert [s["id"] for s in fixed["subtasks"]] == ["design-docs", "resp"] and fixed["contractPaths"] == ["go.mod"]
    assert so.validates(fixed, PLAN_SCHEMA)
    # The retrospective shape: no closing tag on the array, a leading tag on the first property.
    retro = {"summary": "<summary>resp blocked.</summary>\n<learnings>[{\"scope\":\"repo\",\"text\":\"no value.go\"}]"}
    fixed = so.untangle(retro, RETRO_SCHEMA)
    assert fixed == {"summary": "resp blocked.", "learnings": [{"scope": "repo", "text": "no value.go"}]}
    # A payload needing nothing, and a string with a stray '<' in it, are left alone.
    plain = {"summary": "a < b", "learnings": []}
    assert so.untangle(plain, RETRO_SCHEMA) == plain
    assert so.untangle({"summary": "x"}, RETRO_SCHEMA) == {"summary": "x"}


def test_parses_the_raw_tool_input_the_cli_could_not():
    raw = '{"subtasks": [{"id": "design-docs"}], "summary": "ok"}'
    assert so.untangle({"__unparsedToolInput": {"raw": raw}}, PLAN_SCHEMA) == {
        "subtasks": [{"id": "design-docs"}], "summary": "ok"}
    truncated = {"__unparsedToolInput": {"raw": '{"subtasks": [{"id": "des'}}
    assert so.untangle(truncated, PLAN_SCHEMA) == truncated


def test_salvage_returns_the_newest_attempt_that_validates():
    good = {"summary": "s", "learnings": [{"scope": "repo", "text": "keep tests green"}]}
    bad_enum = {"summary": "s", "learnings": [{"scope": "harness", "text": "x"}]}
    tagged = {"summary": 'older.</summary>\n<learnings>[{"scope":"process","text":"slice by file"}]'}
    assert so.salvage([tagged, good, bad_enum], RETRO_SCHEMA) == good
    assert so.salvage([tagged, bad_enum], RETRO_SCHEMA) == {
        "summary": "older.", "learnings": [{"scope": "process", "text": "slice by file"}]}
    assert so.salvage([bad_enum, {"file_path": "dummy"}], RETRO_SCHEMA) is None
    assert so.salvage([], RETRO_SCHEMA) is None


def test_prefer_substantive_replaces_a_placeholder_the_validator_accepted():
    """The program planner wrote a six-subsystem plan twice as tagged text inside 'summary' (rejected: no
    'subtasks' property), then sent a one-item "test" plan that validated; the program built one document
    and reported itself delivered (run b8dc259b). The rejected payload that untangles into a far larger
    schema-valid one is the agent's answer."""
    from common.structured_output import prefer_substantive
    schema = {"type": "object", "properties": {"subtasks": {"type": "array"}, "summary": {"type": "string"},
                                               "contractPaths": {"type": "array"}}, "required": ["subtasks"]}
    rich = ('Bare scaffold; six subsystems forming a DAG.</summary>\n<contractPaths>["go.mod", "Makefile"]'
            '</contractPaths>\n<subtasks>[{"id": "design", "description": "author the architecture document that fixes the file layout, the shared types and the error strings every team builds against", "deliverable": "docs", "files": ["docs/design/architecture.md"],'
            ' "acceptanceCriteria": ["architecture.md fixes the file layout and shared types"]},'
            ' {"id": "resp", "description": "a streaming RESP2 reader and writer with size bounds, inline commands and pipelining", "deliverable": "code", "files": ["resp/reader.go", "resp/writer.go"], "dependsOn": ["design"],'
            ' "acceptanceCriteria": ["a streaming RESP2 reader and writer"]}]')
    placeholder = {"subtasks": [{"id": "design", "description": "test", "files": ["docs/design/architecture.md"],
                                 "acceptanceCriteria": ["ok"]}]}
    attempts = [{"summary": rich}, {"summary": rich}, placeholder]
    best = prefer_substantive(placeholder, attempts, schema)
    assert best is not None and [s["id"] for s in best["subtasks"]] == ["design", "resp"]
    assert best["contractPaths"] == ["go.mod", "Makefile"] and best["summary"].startswith("Bare scaffold")
    # The accepted payload stands when it is itself substantive, or when nothing rejected validates.
    assert prefer_substantive(best, attempts, schema) is None
    assert prefer_substantive(placeholder, [{"summary": "no tags here"}, placeholder], schema) is None
    assert prefer_substantive(placeholder, [], schema) is None
