"""Safety and capability policy regression tests for unattended coding sessions."""

import asyncio
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
