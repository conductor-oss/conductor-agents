"""Every site that embeds test_cycle must survive its two failure modes.

1. The fix loop creates commits, so a site whose downstream head guard still
   compares against the pre-test SHA reports drift and silently blocks
   publication. Nothing errors; the push just stops.
2. Every call is ``optional: true``, so a degraded child returns nulls. A bare
   ``SET_VARIABLE candidateCommit = ${child.output.candidateCommit}`` would
   overwrite a good SHA with null, which is worse than not testing at all.
"""

from __future__ import annotations

import json
from pathlib import Path

WF = Path(__file__).resolve().parents[1] / "workflows"

EMBEDDINGS = {
    "code_subtask": ("subtask_tests", "targeted"),
    "code_parallel": ("post_merge_tests", "targeted"),
    "address_pr": ("verify", "targeted"),
    "feature_campaign": ("campaign_tests", "full"),
    "issue_to_pr": ("delivery_verification", "full"),
    "github_demo": ("verify", "full"),
    "address_pr_repair": ("repair", "targeted"),
}


def _load(name: str) -> dict:
    return json.loads((WF / f"{name}.json").read_text())


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _children(workflow: dict) -> list[dict]:
    return [task for task in _walk(workflow)
            if task.get("subWorkflowParam", {}).get("name") == "test_cycle"]


def test_every_embedding_is_optional_pinned_and_in_the_expected_mode():
    for name, (ref, mode) in EMBEDDINGS.items():
        children = _children(_load(name))
        assert len(children) == 1, f"{name}: expected exactly one test_cycle child"
        child = children[0]
        assert child["taskReferenceName"] == ref, name
        assert child["subWorkflowParam"]["version"] == 1, name
        # A failing or unavailable verifier must degrade the caller, not fail it.
        assert child.get("optional") is True, name
        assert child["inputParameters"]["testMode"] == mode, name


def test_every_embedding_forwards_the_policy_envelope():
    envelope = {"modelProfile", "modelPolicy", "modelPolicySource",
                "modelPolicySha256", "modelsConfig", "modelOverrides"}
    for name in EMBEDDINGS:
        child = _children(_load(name))[0]
        assert envelope <= set(child["inputParameters"]), name


def test_no_embedding_references_an_input_its_caller_does_not_declare():
    for name in EMBEDDINGS:
        workflow = _load(name)
        declared = set(workflow["inputParameters"])
        for key, value in _children(workflow)[0]["inputParameters"].items():
            if isinstance(value, str) and value.startswith("${workflow.input."):
                referenced = value[len("${workflow.input."):-1]
                assert referenced in declared, f"{name}.{key} -> {referenced}"


def test_full_mode_sites_opt_into_heavy_suites_and_targeted_sites_do_not():
    for name, (_, mode) in EMBEDDINGS.items():
        params = _children(_load(name))[0]["inputParameters"]
        if mode == "full":
            # A pre-publication gate that silently skips the integration suite
            # is not the full run its caller asked for.
            assert params.get("allowHeavySuites") is True, name
        else:
            assert "allowHeavySuites" not in params, name


def test_a_post_test_commit_reaches_every_downstream_head_guard():
    # code_parallel: source_handoff compares the checkout head to the variable.
    code_parallel = _load("code_parallel")
    refs = [task["taskReferenceName"] for task in code_parallel["tasks"]]
    assert refs.index("post_merge_tests") < refs.index("verification_candidate_final") \
        < refs.index("source_handoff")
    final = next(task for task in code_parallel["tasks"]
                 if task["taskReferenceName"] == "verification_candidate_final")
    assert final["inputParameters"]["candidateCommit"] == \
        "${verification_outcome.output.candidateCommit}"

    # feature_campaign: git_push guards on an exact SHA.
    campaign = _load("feature_campaign")
    campaign_refs = [task["taskReferenceName"] for task in campaign["tasks"]]
    assert campaign_refs.index("campaign_commit") < campaign_refs.index("campaign_tests") \
        < campaign_refs.index("campaign_publication_plan") \
        < campaign_refs.index("campaign_publish_gate")
    push = next(task for task in _walk(campaign) if task.get("name") == "git_push")
    assert push["inputParameters"]["expectedHead"] == \
        "${campaign_publication_plan.output.head}"

    # code_subtask: the merge picks up whatever the subtask branch ends on.
    subtask = _load("code_subtask")
    subtask_refs = [task["taskReferenceName"] for task in subtask["tasks"]]
    assert subtask_refs.index("cmt") < subtask_refs.index("subtask_tests") \
        < subtask_refs.index("delivery_outcome")
    outcome = next(task for task in subtask["tasks"]
                   if task["taskReferenceName"] == "delivery_outcome")
    assert outcome["inputParameters"]["tested"] == "${subtask_tests.output.candidateCommit}"


def test_the_tested_commit_is_never_adopted_by_a_bare_set_variable():
    # A SET_VARIABLE cannot express "keep the old value when the new one is
    # null", so adopting an optional child's SHA directly is how a good commit
    # gets clobbered. Every site folds it through jq first.
    for name in ("code_parallel", "code_subtask", "feature_campaign"):
        for task in _walk(_load(name)):
            if task.get("type") != "SET_VARIABLE":
                continue
            for key, value in task.get("inputParameters", {}).items():
                assert not (isinstance(value, str)
                            and value.endswith(".output.candidateCommit}")
                            and any(ref in value for ref in
                                    ("subtask_tests", "post_merge_tests", "campaign_tests"))), \
                    f"{name}.{task['taskReferenceName']}.{key} adopts a nullable SHA directly"


def test_callers_surface_the_verdict_so_a_gate_can_read_it():
    for name in ("code_parallel", "code_subtask", "feature_campaign", "openspec_development"):
        outputs = _load(name)["outputParameters"]
        assert "testCycleState" in outputs, name
        assert "testsPassed" in outputs, name


def test_address_pr_forwards_the_agent_fallback_flags_to_both_repair_paths():
    # A PR-comment fix that touches a file with no directly-matching test (a
    # shared helper, for example) must not be permanently configuration_blocked
    # in either verification pass -- confirmed live (case
    # tests/integration/retry_helpers.py) before this wiring existed.
    # Both default true here (unlike test_cycle/test_agent_fallback's own
    # default of false): address_pr is the interactive PR-feedback loop,
    # where leaving a change permanently configuration_blocked on a missing
    # test-file mapping is worse than trying the agent fallback first.
    address_pr = _load("address_pr")
    assert "allowAgentTestPlan" in address_pr["inputParameters"]
    assert "allowAgentAuthoredTests" in address_pr["inputParameters"]
    assert address_pr["inputTemplate"]["allowAgentTestPlan"] is True
    assert address_pr["inputTemplate"]["allowAgentAuthoredTests"] is True

    verify = _children(address_pr)[0]
    assert verify["inputParameters"]["allowAgentTestPlan"] == "${workflow.input.allowAgentTestPlan}"
    assert verify["inputParameters"]["allowAgentAuthoredTests"] == "${workflow.input.allowAgentAuthoredTests}"

    repair_call = next(task for task in _walk(address_pr)
                       if task.get("subWorkflowParam", {}).get("name") == "address_pr_repair")
    assert repair_call["inputParameters"]["allowAgentTestPlan"] == "${workflow.input.allowAgentTestPlan}"
    assert repair_call["inputParameters"]["allowAgentAuthoredTests"] == "${workflow.input.allowAgentAuthoredTests}"

    address_pr_repair = _load("address_pr_repair")
    assert "allowAgentTestPlan" in address_pr_repair["inputParameters"]
    assert "allowAgentAuthoredTests" in address_pr_repair["inputParameters"]
    inner_test_cycle = _children(address_pr_repair)[0]
    assert inner_test_cycle["inputParameters"]["allowAgentTestPlan"] == "${workflow.input.allowAgentTestPlan}"
    assert inner_test_cycle["inputParameters"]["allowAgentAuthoredTests"] == "${workflow.input.allowAgentAuthoredTests}"
