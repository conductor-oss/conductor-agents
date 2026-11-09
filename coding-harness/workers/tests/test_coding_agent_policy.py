"""Safety and capability policy regression tests for unattended coding sessions."""

import asyncio
import json
import inspect
import os
import shutil

import pytest

from common import coding_agent as coding_agent_module
from common.coding_agent import SCOPE_BOUNDARY, VERIFICATION_BOUNDARY, _external_context_paths
from common.tool_policy import DEFAULT_ALLOWED_TOOLS, DEFAULT_DISALLOWED_TOOLS, denied_without_changes
from coding_agent.tasks import (
    _append_context_paths,
    _claim_task_flight,
    _finish_task_flight,
    _live_context_paths,
    _remove_context_snapshot,
    _snapshot_external_context_paths,
    _task_flight_result,
    _within_write_roots,
    coding_agent,
)


def test_cargo_commands_are_available_to_unattended_agents():
    assert "Bash(cargo *)" in DEFAULT_ALLOWED_TOOLS


@pytest.mark.parametrize("rule", [
    "Bash(./gradlew *)", "Bash(gradle *)", "Bash(./mvnw *)", "Bash(mvn *)",
    "Bash(ant *)", "Bash(sbt *)", "Bash(mill *)", "Bash(bazel *)", "Bash(bazelisk *)", "Bash(java *)",
    "Bash(pip *)", "Bash(pip3 *)", "Bash(uv *)", "Bash(poetry *)", "Bash(pdm *)", "Bash(tox *)", "Bash(nox *)", "Bash(hatch *)", "Bash(coverage *)", "Bash(ruff *)",
    "Bash(yarn *)", "Bash(pnpm *)", "Bash(bun *)", "Bash(deno *)", "Bash(corepack *)", "Bash(tsc *)",
    "Bash(gofmt *)", "Bash(golangci-lint *)", "Bash(rustc *)", "Bash(rustfmt *)", "Bash(clippy-driver *)",
    "Bash(ruby *)", "Bash(bundle *)", "Bash(rake *)", "Bash(rspec *)", "Bash(dotnet *)", "Bash(nuget *)",
    "Bash(make *)", "Bash(cmake *)", "Bash(ctest *)", "Bash(ninja *)", "Bash(meson *)", "Bash(./configure *)",
    "Bash(swift *)", "Bash(xcodebuild *)", "Bash(php *)", "Bash(composer *)", "Bash(mix *)", "Bash(rebar3 *)",
    "Bash(cabal *)", "Bash(stack *)", "Bash(dart *)", "Bash(flutter *)", "Bash(clojure *)", "Bash(lein *)",
    "Bash(lua *)", "Bash(luarocks *)", "Bash(perl *)", "Bash(prove *)", "Bash(cpanm *)", "Bash(Rscript *)", "Bash(zig *)",
])
def test_supported_build_and_test_entrypoints_are_scoped(rule):
    assert rule in DEFAULT_ALLOWED_TOOLS


def test_gradle_wrapper_supports_normal_build_and_version_calls():
    rule = "Bash(./gradlew *)"
    assert rule in DEFAULT_ALLOWED_TOOLS
    assert "./gradlew build".startswith("./gradlew ")
    assert "./gradlew --version".startswith("./gradlew ")


@pytest.mark.parametrize("tool", [
    "Bash", "Bash(sh *)", "Bash(bash *)", "Bash(env *)", "Bash(sudo *)",
    "Bash(rm -rf *)", "Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)",
    "WebSearch", "WebFetch",
])
def test_generic_shells_and_sensitive_controls_remain_blocked(tool):
    assert tool not in DEFAULT_ALLOWED_TOOLS
    if tool in {"WebSearch", "WebFetch", "Bash(sudo *)", "Bash(rm -rf *)",
                "Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)"}:
        assert tool in DEFAULT_DISALLOWED_TOOLS


def test_denials_with_no_changes_fail_closed():
    assert denied_without_changes([], ["Bash(cargo test) denied"]) is True
    assert denied_without_changes(["test_output.txt"], ["unrelated denial"]) is False
    assert denied_without_changes([], []) is False


def test_all_backends_receive_the_sandbox_verification_boundary():
    assert "not a project-code failure" in VERIFICATION_BOUNDARY
    assert "file-lock listener" in VERIFICATION_BOUNDARY
    assert "do not create or commit cache artifacts" in VERIFICATION_BOUNDARY


def test_all_backends_receive_the_workflow_file_scope_boundary():
    # An unrequested .github/workflows/*.yml edit doesn't fail loudly -- it
    # silently blocks the push (missing OAuth `workflow` scope) and the PR
    # never opens. This must reach every coding_agent call, not just one
    # template, so it lives in the same non-displaceable boundary appended
    # in common.coding_agent.run_coding_agent regardless of promptTemplate.
    assert ".github/workflows/" in SCOPE_BOUNDARY
    assert "unless the task explicitly asks" in SCOPE_BOUNDARY
    source = inspect.getsource(__import__("common.coding_agent", fromlist=["run_coding_agent"]))
    assert "prompt += VERIFICATION_BOUNDARY + SCOPE_BOUNDARY" in source


def test_claude_sandbox_allows_binding_a_local_test_server(monkeypatch, tmp_path):
    # Confirmed live: a test binding HTTPServer(("127.0.0.1", 0), ...) failed
    # with PermissionError under the sandbox's default network policy -- a
    # sandbox artifact, not a project-code failure. allowedDomains (egress to
    # named remote hosts) is a separate, caller-controlled axis and is
    # untouched by this.
    captured = {}

    async def fake_drive(_prompt, options, on_turn=None):
        captured["options"] = options
        return {"result": "ok"}

    monkeypatch.setattr(coding_agent_module, "_drive", fake_drive)

    asyncio.run(coding_agent_module.run_coding_agent("do the thing", worktree=str(tmp_path)))

    network = captured["options"].sandbox["network"]
    assert network["allowLocalBinding"] is True
    assert network["allowedDomains"] == []


def test_drive_keeps_every_structured_output_payload_whole(monkeypatch):
    """The turn log caps each command at 300 chars; the StructuredOutput payloads the agent submitted
    — including the ones the validator rejected — travel whole so a consumer can salvage a mangled one
    (the retrospective of run a062184f) instead of recording the placeholder sent to get past it."""
    import claude_agent_sdk as sdk
    import common.coding_agent as m
    big = {"summary": "x" * 400 + '</summary>\n<learnings>[{"scope":"repo","text":"real"}]'}
    small = {"summary": "test", "learnings": []}

    async def fake_query(prompt, options):
        yield sdk.SystemMessage(subtype="init", data={"session_id": "s1"})
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="t1", name="StructuredOutput", input=big)], model="m")
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="t2", name="Read", input={"file_path": "a"})], model="m")
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="t3", name="StructuredOutput", input=small)], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=3,
                                session_id="s1", total_cost_usd=0.1, result="", structured_output=small)

    monkeypatch.setattr(m.sdk, "query", fake_query)
    out = asyncio.run(m._drive("p", sdk.ClaudeAgentOptions()))
    assert out["structured_attempts"] == [big, small] and out["structured"] == small
    assert out["turns"][0]["commands"][0].endswith("…") and len(out["turns"][0]["commands"][0]) < 400


def test_rejected_structured_output_is_salvaged_instead_of_failing_the_session(monkeypatch, tmp_path):
    """Run a33d7061: the program planner's five StructuredOutput payloads were rejected (the plan as
    tagged text inside 'summary'; raw input the CLI could not parse) and the task failed although the
    plan was written. The runner keeps the newest payload that satisfies the schema."""
    import common.coding_agent as m
    schema = {"type": "object", "properties": {"subtasks": {"type": "array"}, "summary": {"type": "string"}},
              "required": ["subtasks"]}
    attempts = [{"summary": 'Five subsystems.</summary>\n<subtasks>[{"id": "design-docs"}, {"id": "resp"}]</subtasks>'},
                {"__unparsedToolInput": {"raw": '{"subtasks": [{"id": "des'}}]

    async def fake_drive(_prompt, options, on_turn=None):
        return {"ok": False, "status": "error_max_structured_output_retries", "result": "", "structured": None,
                "structured_attempts": attempts, "session_id": "s", "num_turns": 11, "turns": [], "tokens": 1,
                "cost_usd": 1.1, "denials": [], "denied_writes": [], "turn_log": [], "error": None}

    monkeypatch.setattr(m, "_drive", fake_drive)
    out = asyncio.run(m.run_coding_agent("plan", worktree=str(tmp_path), output_schema=schema))
    assert out["ok"] is True and out["status"] == "success" and out["structured_salvaged"] is True
    assert out["structured"] == {"summary": "Five subsystems.", "subtasks": [{"id": "design-docs"}, {"id": "resp"}]}
    assert json.loads(out["result"]) == out["structured"]
    # Without a schema, or when nothing salvageable was submitted, the failure stands.
    out2 = asyncio.run(m.run_coding_agent("plan", worktree=str(tmp_path)))
    assert out2["ok"] is False and out2["status"] == "error_max_structured_output_retries"
    attempts[:] = [{"file_path": "dummy"}]
    out3 = asyncio.run(m.run_coding_agent("plan", worktree=str(tmp_path), output_schema=schema))
    assert out3["ok"] is False and "structured_salvaged" not in out3


def test_campaign_write_roots_only_tighten_the_worktree():
    assert _within_write_roots("src/api/handler.py", ["src/api"])
    assert _within_write_roots("README.md", ["README.md"])
    assert not _within_write_roots("src/ui/app.py", ["src/api"])
    assert _within_write_roots("anything", None)
    assert _within_write_roots(".github/workflows/ci.yml", [".github/workflows/ci.yml"])
    assert not _within_write_roots(".env", ["env"])
    assert not _within_write_roots("../outside.py", ["outside.py"])
    assert not _within_write_roots("/tmp/outside.py", ["tmp"])


def test_coding_worker_uses_the_claiming_sync_runner_not_async_repolling():
    assert not inspect.iscoroutinefunction(coding_agent)


def test_duplicate_task_delivery_joins_the_primary_agent_session():
    task_id = "duplicate-task-delivery-test"
    owner, event, cached = _claim_task_flight(task_id)
    duplicate, duplicate_event, duplicate_cached = _claim_task_flight(task_id)

    assert owner is True
    assert cached is None
    assert duplicate is False
    assert duplicate_cached is None
    assert duplicate_event is event

    result = object()
    _finish_task_flight(task_id, event, result)

    assert event.is_set()
    assert _task_flight_result(task_id) is result
    owner, _, cached = _claim_task_flight(task_id)
    assert owner is False
    assert cached is result


def test_live_context_references_do_not_inline_file_contents(tmp_path):
    doc = tmp_path / "design.md"
    doc.write_text("secret implementation detail")
    entries = _live_context_paths([str(doc)])
    prompt = _append_context_paths("implement feature", entries)
    assert str(doc.resolve()) in prompt
    assert "secret implementation detail" not in prompt
    assert entries[0]["kind"] == "file"


def test_live_context_references_allow_docs_inside_the_target_checkout(tmp_path):
    """A user may point at a design document already stored in repoPath."""
    doc = tmp_path / "docs" / "design" / "feature.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("authoritative design")

    entries = _live_context_paths([str(doc)])

    assert entries == [
        {
            "path": str(doc.resolve()), "kind": "file", "device": doc.stat().st_dev,
            "inode": doc.stat().st_ino, "mtimeNs": doc.stat().st_mtime_ns,
            "size": len("authoritative design"),
        }
    ]


def test_only_context_outside_the_worktree_requires_an_extra_read_root(tmp_path):
    repo = tmp_path / "repo"
    doc = repo / "docs" / "design.md"
    outside = tmp_path / "brief.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("in repo")
    outside.write_text("external")

    assert _external_context_paths(str(repo), [str(doc)]) == []
    assert _external_context_paths(str(repo), [str(doc), str(outside)]) == [str(outside)]


def test_external_context_is_snapshotted_inside_a_parallel_worktree(tmp_path):
    repo = tmp_path / "repo"
    child = repo / ".cc-worktrees" / "slice"
    doc = repo / "docs" / "design.md"
    child.mkdir(parents=True)
    doc.parent.mkdir(parents=True)
    doc.write_text("authoritative design")
    entries = _live_context_paths([str(doc)])

    effective, snapshot_root = _snapshot_external_context_paths(entries, str(child))

    assert snapshot_root == str(child / ".cc-context")
    assert effective[0]["sourcePath"] == str(doc.resolve())
    assert effective[0]["snapshot"] is True
    assert effective[0]["path"].startswith(snapshot_root + os.sep)
    assert open(effective[0]["path"], encoding="utf-8").read() == "authoritative design"
    _remove_context_snapshot(snapshot_root)
    assert not os.path.exists(snapshot_root)


def test_context_directory_snapshot_stays_inside_worktree_and_preserves_files(tmp_path):
    worktree = tmp_path / "repo" / ".cc-worktrees" / "slice"
    source = tmp_path / "external-design"
    worktree.mkdir(parents=True)
    (source / "nested").mkdir(parents=True)
    (source / "architecture.md").write_text("design")
    (source / "nested" / "api.md").write_text("contract")
    entries = _live_context_paths([str(source)])

    effective, snapshot_root = _snapshot_external_context_paths(entries, str(worktree))

    assert effective[0]["kind"] == "directory"
    assert effective[0]["path"].startswith(snapshot_root + os.sep)
    assert open(os.path.join(effective[0]["path"], "nested", "api.md"), encoding="utf-8").read() == "contract"
    _remove_context_snapshot(snapshot_root)
    assert not os.path.exists(snapshot_root)


def test_in_worktree_context_is_not_snapshotted(tmp_path):
    worktree = tmp_path / "repo"
    doc = worktree / "docs" / "design.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("design")
    entries = _live_context_paths([str(doc)])

    effective, snapshot_root = _snapshot_external_context_paths(entries, str(worktree))

    assert effective == entries
    assert snapshot_root is None


def test_snapshot_prompt_exposes_source_and_effective_path_without_contents(tmp_path):
    worktree = tmp_path / "repo"
    source = tmp_path / "external.md"
    worktree.mkdir()
    source.write_text("never inline this")
    entries = _live_context_paths([str(source)])
    effective, snapshot_root = _snapshot_external_context_paths(entries, str(worktree))

    prompt = _append_context_paths("implement", effective)

    assert str(source.resolve()) in prompt
    assert effective[0]["path"] in prompt
    assert "never inline this" not in prompt
    _remove_context_snapshot(snapshot_root)
    assert not os.path.exists(snapshot_root)


def test_denied_write_paths_are_repo_relative_and_only_outside_the_roots(tmp_path):
    """The worktree guard's refusals become the Lead's evidence for widening an item's write scope
    (st-lru / store.go, run fc6467b7): in-worktree paths outside the roots, repo-relative, deduplicated.
    An escape of the worktree is a different denial and is left out; with no roots nothing is refused
    on scope."""
    from coding_agent.tasks import denied_write_paths
    wt = str(tmp_path / "wt")
    os.makedirs(os.path.join(wt, "store"))
    refused = [os.path.join(wt, "store", "store.go"), os.path.join(wt, "store", "lru.go"),
               "/etc/hosts", os.path.join(wt, "..", "outside.go"), os.path.join(wt, "store", "store.go")]
    roots = ["store/lru.go", "tests/acceptance/test_accept_st_lru.py"]
    assert denied_write_paths(refused, wt, roots) == ["store/store.go"]
    assert denied_write_paths(refused, wt, []) == []
    assert denied_write_paths(["store/value.go"], wt, roots) == ["store/value.go"]  # relative input


def test_a_validator_accepted_placeholder_is_replaced_by_the_substantive_rejected_payload(monkeypatch, tmp_path):
    """Run b8dc259b: the planner's real plan was rejected twice (tagged text inside 'summary'); its third
    payload — one "test" item — validated, the session succeeded, and the program built that one item.
    The runner now keeps the substantive payload the validator rejected."""
    import common.coding_agent as m
    schema = {"type": "object", "properties": {"subtasks": {"type": "array"}, "summary": {"type": "string"}},
              "required": ["subtasks"]}
    rich = {"summary": 'Six subsystems.</summary>\n<subtasks>[{"id": "design", "description": "author the architecture document that fixes the file layout and shared types", "deliverable": "docs", "files": ["docs/a.md"], '
                       '"acceptanceCriteria": ["the architecture is fixed in one document"]}, {"id": "resp", "description": "a streaming RESP2 reader and writer with size bounds", "deliverable": '
                       '"code", "files": ["resp/reader.go", "resp/writer.go"], "acceptanceCriteria": ["reader and writer"]}]'}
    placeholder = {"subtasks": [{"id": "design", "description": "test", "acceptanceCriteria": ["ok"]}]}

    async def fake_drive(_prompt, options, on_turn=None):
        return {"ok": True, "status": "success", "result": json.dumps(placeholder), "structured": placeholder,
                "structured_attempts": [rich, placeholder], "session_id": "s", "num_turns": 11, "turns": [],
                "tokens": 1, "cost_usd": 1.1, "denials": [], "denied_writes": [], "turn_log": [], "error": None}

    monkeypatch.setattr(m, "_drive", fake_drive)
    out = asyncio.run(m.run_coding_agent("plan", worktree=str(tmp_path), output_schema=schema))
    assert out["ok"] is True and out["structured_salvaged"] == "placeholder"
    assert [s["id"] for s in out["structured"]["subtasks"]] == ["design", "resp"]
    assert json.loads(out["result"]) == out["structured"]


def test_ensure_repo_initializes_only_a_directory_without_a_head(tmp_path):
    """Run edc1c893 started on an empty directory and failed four times on `git rev-parse HEAD`."""
    from common import git
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "notes.txt").write_text("mine\n")
    assert git.ensure_repo(str(fresh)) is True
    assert git.git(str(fresh), "rev-parse", "--verify", "HEAD", check=False).code == 0
    assert (fresh / "notes.txt").read_text() == "mine\n"
    assert "notes.txt" in git.git(str(fresh), "status", "--porcelain").stdout  # never committed for the user
    assert git.ensure_repo(str(fresh)) is False  # idempotent
    # `git init` done by hand but no commit yet: still has no HEAD, so it gets the base commit.
    bare = tmp_path / "inited"
    bare.mkdir()
    git.git(str(bare), "init")
    assert git.ensure_repo(str(bare)) is True and git.head(str(bare))
    # A directory inside an existing repository is that repository's: left alone.
    sub = fresh / "pkg"
    sub.mkdir()
    head = git.head(str(fresh))
    assert git.ensure_repo(str(sub)) is False and git.head(str(fresh)) == head
    assert not (sub / ".git").exists()
    # A path that does not exist is never created here.
    assert git.ensure_repo(str(tmp_path / "typo")) is False and not (tmp_path / "typo").exists()


def test_coding_agent_worker_initializes_a_fresh_directory_before_reading_head(monkeypatch, tmp_path,
                                                                               fake_task_input):
    """A planner or classifier is often the first step to touch a fresh project directory; the worker
    makes it a repository before it reads HEAD, and says so."""
    import coding_agent.tasks as worker
    from common import git
    fresh = tmp_path / "pg2"
    fresh.mkdir()

    async def fake_run(prompt, **kw):
        return {"ok": True, "status": "success", "result": "planned", "structured": None, "session_id": "s",
                "num_turns": 1, "turns": [], "tokens": 1, "cost_usd": 0.01, "denials": [], "denied_writes": [],
                "turn_log": [], "error": None, "model": "m"}
    monkeypatch.setattr(worker, "run_coding_agent", fake_run)
    out = worker.coding_agent(fake_task_input(worktreePath=str(fresh), prompt="plan the program",
                                              tools=["Read"], modelRole="plan"))
    data = out.output_data
    assert data.get("initializedRepo") is True, data
    assert git.git(str(fresh), "rev-parse", "--verify", "HEAD", check=False).code == 0
    assert any("was not a git repository: initialized it" in str(getattr(line, "log", line)) for line in out.logs)
    typo = fake_task_input(worktreePath=str(tmp_path / "typo"), prompt="x")
    typo.task_id = "test-task-typo"  # the worker de-duplicates deliveries by task id
    missing = worker.coding_agent(typo)
    assert not (tmp_path / "typo").exists()
    assert "is not a directory" in str(missing.reason_for_incompletion or missing.output_data)
