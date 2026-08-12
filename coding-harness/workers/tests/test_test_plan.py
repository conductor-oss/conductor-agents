"""Unit tests for the logic that used to live in test_cycle's jq expressions.

The point of moving it here is that every function is now directly reachable by
a test. The property that matters most is totality: each one accepts whatever a
degraded upstream task returned and still produces a well-formed result, because
a throwing expression used to fail the entire workflow.
"""

from __future__ import annotations

import itertools

import pytest

from common import test_plan

# The shapes a degraded or hostile upstream task can actually hand over.
JUNK = (None, "", "  ", 0, -1, 3.5, True, False, [], {}, [None], [[]], [{}],
        {"commands": None}, {"commands": "x"}, [{"argv": None}], [{"argv": []}],
        [{"argv": ["  "]}], [{"argv": [None]}], "a string", {"nested": {"deep": 1}})


@pytest.mark.parametrize("value", JUNK)
def test_command_normalizers_never_raise(value):
    assert isinstance(test_plan.clean_commands(value), list)
    assert isinstance(test_plan.user_commands(value), list)
    assert isinstance(test_plan.repair_scope(value, value), list)
    assert isinstance(test_plan.template_commands(value), list)


@pytest.mark.parametrize("value", JUNK)
def test_resolve_plan_never_raises(value):
    result = test_plan.resolve_plan(discovered=value, prior=value, user=value,
                                    discovery_outcome=value, discovery_reason=value,
                                    selection=value, template=value)
    assert isinstance(result["commands"], list)
    assert result["commandSource"] in {"user_template", "repository_guide",
                                       "repository_config", "build_system_inference", "none"}


@pytest.mark.parametrize("value", JUNK)
def test_repair_and_progress_never_raise(value):
    repair = test_plan.record_repair(current_candidate=value, agent=value, commit=value)
    assert set(repair) == {"candidate", "report"}
    progress = test_plan.record_progress(reports=value, report=value,
                                         verification=value, runs=value)
    assert isinstance(progress["reports"], list)
    assert isinstance(progress["attempts"], int)
    assert isinstance(progress["runCount"], (int, float))


@pytest.mark.parametrize("value", JUNK)
def test_cycle_outcome_never_raises_and_always_names_a_state(value):
    result = test_plan.cycle_outcome(outcome=value, candidate=value, advanced=value,
                                     attempts=value, runs=value, source=value,
                                     verification=value, mode=value)
    assert result["testCycleState"]
    assert isinstance(result["testsPassed"], bool)
    assert isinstance(result["testReport"], str)


def test_only_fully_formed_argv_survives():
    # A blank or non-string element makes the whole command unusable: it would
    # otherwise reach the verifier as a malformed argv.
    assert test_plan.clean_commands([{"argv": ["pytest", "a.py"]}, {"argv": ["pytest", ""]},
                                     {"argv": ["pytest", None]}, {"argv": []}]) == \
        [{"argv": ["pytest", "a.py"]}]


def test_duplicate_commands_collapse():
    same = {"argv": ["pytest", "a.py"]}
    assert len(test_plan.clean_commands([same, dict(same), same])) == 1


def test_user_override_accepts_a_bare_argv_or_a_command_object():
    lifted = test_plan.user_commands([["pytest", "a.py"], {"argv": ["go", "test", "./x"]}])
    assert [c["argv"] for c in lifted] == [["pytest", "a.py"], ["go", "test", "./x"]]
    assert {c["kind"] for c in lifted} == {"user"}
    assert {c["source"] for c in lifted} == {"user-template"}


def test_an_operator_override_unblocks_a_repository_discovery_cannot_map():
    # Discovery failing is not a reason to refuse a command the operator named.
    resolved = test_plan.resolve_plan(
        discovered=[], prior=None, user=[["pytest", "a.py"]],
        discovery_outcome="configuration_blocked", discovery_reason="unsupported build graph",
        selection=None)
    assert resolved["discoveryOutcome"] == "discovered"
    assert resolved["commandSource"] == "user_template"


def test_template_commands_parses_an_inline_json_argv_list():
    lifted = test_plan.template_commands('[["pytest", "a.py"]]')
    assert [c["argv"] for c in lifted] == [["pytest", "a.py"]]
    assert {c["kind"] for c in lifted} == {"user"}


def test_template_commands_rejects_the_at_file_convention():
    # `@repo/path` is templating.py's "resolve this from a file in the
    # checkout" convention. A prompt a model reads may safely use it; argv a
    # worker executes must not, because the candidate controls that checkout.
    assert test_plan.template_commands("@repo/tests.json") == []
    assert test_plan.template_commands('@[["pytest", "a.py"]]') == []


def test_template_commands_ignores_malformed_json_rather_than_raising():
    assert test_plan.template_commands("not json") == []
    assert test_plan.template_commands("{not even an array}") == []
    assert test_plan.template_commands("") == []
    assert test_plan.template_commands(None) == []


def test_structured_user_commands_win_over_an_inline_template():
    # testCommands is structured input; it cannot suffer a JSON-parsing
    # surprise the way a hand-typed template string can, so it wins when both
    # are supplied.
    resolved = test_plan.resolve_plan(
        discovered=[], prior=None, user=[["pytest", "structured.py"]],
        template='[["pytest", "template.py"]]',
        discovery_outcome="discovered", discovery_reason=None, selection=None)
    assert [c["argv"] for c in resolved["commands"]] == [["pytest", "structured.py"]]
    assert resolved["commandSource"] == "user_template"


def test_template_is_used_when_no_structured_commands_are_supplied():
    resolved = test_plan.resolve_plan(
        discovered=[], prior=None, user=None,
        template='[["pytest", "template.py"]]',
        discovery_outcome="configuration_blocked", discovery_reason="unmapped",
        selection=None)
    assert [c["argv"] for c in resolved["commands"]] == [["pytest", "template.py"]]
    assert resolved["commandSource"] == "user_template"
    assert resolved["discoveryOutcome"] == "discovered"


def test_template_still_defers_to_discovery_when_it_is_at_prefixed():
    resolved = test_plan.resolve_plan(
        discovered=[{"argv": ["pytest", "discovered.py"]}], prior=None, user=None,
        template="@repo/.conductor-code/tests.json",
        discovery_outcome="discovered", discovery_reason=None, selection="changed-scope")
    assert [c["argv"] for c in resolved["commands"]] == [["pytest", "discovered.py"]]
    assert resolved["commandSource"] == "build_system_inference"


def test_agent_proposal_is_used_only_when_discovery_and_carried_obligations_are_both_empty():
    resolved = test_plan.resolve_plan(
        discovered=[], prior=None, user=None,
        agent=[{"argv": ["pytest", "agent.py"], "source": "agent-proposal", "kind": "agent"}],
        discovery_outcome="configuration_blocked", discovery_reason="unmapped", selection=None)
    assert [c["argv"] for c in resolved["commands"]] == [["pytest", "agent.py"]]
    assert resolved["commandSource"] == "agent_proposal"
    assert resolved["discoveryOutcome"] == "discovered"


def test_agent_proposal_never_outranks_a_deterministic_or_operator_source():
    # Deterministic discovery present -> the agent proposal is not even consulted.
    resolved = test_plan.resolve_plan(
        discovered=[{"argv": ["go", "test", "./x"], "source": "build-metadata:go"}],
        prior=None, user=None, agent=[{"argv": ["pytest", "agent.py"]}],
        discovery_outcome="discovered", discovery_reason=None, selection="changed-scope")
    assert resolved["commandSource"] == "build_system_inference"

    # An operator override present -> same, the agent proposal is not consulted.
    resolved = test_plan.resolve_plan(
        discovered=[], prior=None, user=[["pytest", "operator.py"]],
        agent=[{"argv": ["pytest", "agent.py"]}],
        discovery_outcome="configuration_blocked", discovery_reason=None, selection=None)
    assert resolved["commandSource"] == "user_template"
    assert [c["argv"] for c in resolved["commands"]] == [["pytest", "operator.py"]]


def test_an_empty_or_malformed_agent_proposal_never_raises_and_leaves_the_plan_blocked():
    for junk in (None, [], "not a list", [None], [{}]):
        resolved = test_plan.resolve_plan(
            discovered=[], prior=None, user=None, agent=junk,
            discovery_outcome="configuration_blocked", discovery_reason="unmapped", selection=None)
        assert resolved["commands"] == []
        assert resolved["commandSource"] == "none"
        assert resolved["discoveryOutcome"] == "configuration_blocked"


def test_carried_obligations_merge_with_discovery_and_dedupe():
    resolved = test_plan.resolve_plan(
        discovered=[{"argv": ["pytest", "a.py"]}],
        prior={"commands": [{"argv": ["pytest", "a.py"]}, {"argv": ["pytest", "b.py"]}]},
        user=None, discovery_outcome="discovered", discovery_reason=None,
        selection="changed-scope")
    assert [c["argv"] for c in resolved["commands"]] == [["pytest", "a.py"], ["pytest", "b.py"]]
    assert resolved["requiredCount"] == 2


def test_a_declared_scope_outranks_the_discovered_one():
    assert test_plan.repair_scope(["src"], ["other"]) == ["src"]
    assert test_plan.repair_scope([], ["other"]) == ["other"]
    assert test_plan.repair_scope(["   ", None, 7], ["other"]) == ["other"]


def test_progress_reports_a_stalled_fix():
    stalled = test_plan.record_progress(
        reports=[], report={"persisted": False, "candidateAfter": "old"},
        verification={"executionOutcome": "code_failed"}, runs=1)
    assert stalled["candidateAdvanced"] is False
    assert stalled["runCount"] == 2
    assert stalled["reports"][0]["executionOutcome"] == "code_failed"


def test_a_degraded_verifier_is_recorded_as_blocked_not_as_a_pass():
    entry = test_plan.record_progress(reports=None, report={"candidateAfter": "abc"},
                                      verification=None, runs=None)["reports"][0]
    assert entry["verificationState"] == "blocked"
    assert entry["executionOutcome"] == "infra_blocked"


def test_run_count_reports_executions_not_attempts():
    ran = test_plan.cycle_outcome(outcome="passed", candidate="a", advanced=True, attempts=1,
                                  runs=2, source="none", verification={"commands": []},
                                  mode="targeted")
    blocked = test_plan.cycle_outcome(outcome="configuration_blocked", candidate="a",
                                      advanced=True, attempts=1, runs=2, source="none",
                                      verification={"commands": []}, mode="targeted")
    assert ran["testRunCount"] == 2
    # Nothing executed, so a count would overstate what was proven -- and the
    # prose must agree with the field.
    assert blocked["testRunCount"] == 0
    assert "0 test run(s)" in blocked["testReport"]


def test_every_outcome_and_flag_combination_yields_a_declared_state():
    declared = {"tests_passed", "no_tests_required", "tests_failed_after_fix_budget",
                "tests_failed_fix_unavailable", "command_discovery_blocked",
                "runtime_unavailable", "candidate_commit_missing",
                "verifier_worker_unavailable"}
    produced = {
        test_plan.cycle_outcome(outcome=outcome, candidate=candidate, advanced=advanced,
                                attempts=0, runs=1, source="none",
                                verification={"commands": []}, mode="targeted")["testCycleState"]
        for outcome, candidate, advanced in itertools.product(
            ("passed", "no_tests_required", "code_failed", "configuration_blocked",
             "infra_blocked", "cancelled", "pending", None),
            ("abc", "", None), (True, False, None))
    }
    assert produced == declared
