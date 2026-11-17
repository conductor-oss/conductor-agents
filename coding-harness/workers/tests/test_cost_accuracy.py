"""Spend is what the sessions used, priced from the run's policy (run c5fbbe5b reported $4.50 for $19.84):
the most specific price entry wins, the policy's table is the one used, and a Codex session's sub-agents
(their own session logs) are in its total."""
import json
import os
import time

from common import codex, cost


def test_the_most_specific_price_entry_wins_and_the_runs_policy_prices_are_used():
    assert cost._price_rates("gpt-5.6-terra") == cost.PRICING["gpt-5.6-terra"]  # not "gpt-5"'s
    assert cost._price_rates("gpt-5.6-sol") == cost.PRICING["gpt-5.6-sol"]
    assert cost._price_rates("gpt-5") == cost.PRICING["gpt-5"]
    with cost.using_prices({"gpt-5.6-terra": {"input": 7, "output": 70}, "claude-opus-5-5": {"input": 4, "output": 20}}):
        assert cost._price_rates("gpt-5.6-terra") == (7.0, 70.0)
        assert cost._price_rates("claude-opus-5-5") == (4.0, 20.0)
    assert cost._price_rates("gpt-5.6-terra") == cost.PRICING["gpt-5.6-terra"]  # scoped to the block


def _rollout(folder, name, meta, model, usage):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
        fh.write(json.dumps({"type": "turn_context", "payload": {"model": model}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": usage}}}) + "\n")


def test_a_codex_sessions_sub_agents_at_any_depth_are_counted_in_its_tokens_and_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    folder = os.path.join(str(tmp_path), "sessions", time.strftime("%Y/%m/%d"))
    use = {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 0}
    spawn = lambda parent: {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}  # noqa: E731
    _rollout(folder, "rollout-a.jsonl", {"id": "parent", "source": "vscode"}, "gpt-5.6-sol", use)
    _rollout(folder, "rollout-b.jsonl", {"id": "child", "source": spawn("parent")}, "gpt-5.6-sol", use)
    _rollout(folder, "rollout-c.jsonl", {"id": "grandchild", "source": spawn("child")}, "gpt-5.6-terra", use)
    _rollout(folder, "rollout-d.jsonl", {"id": "stranger", "source": spawn("someone-else")}, "gpt-5.6-sol", use)
    res = codex.with_subagents({"session_id": "parent", "tokens": 1_000_000, "cost_usd": 5.0}, since=time.time() - 10)
    assert sorted(s["thread"] for s in res["subagents"]) == ["child", "grandchild"]
    assert res["tokens"] == 3_000_000 and res["cost_usd"] == 5.0 + 5.0 + 2.5
    assert codex.with_subagents({"session_id": "lonely", "cost_usd": 1.0}, since=time.time() - 10) == {
        "session_id": "lonely", "cost_usd": 1.0}
