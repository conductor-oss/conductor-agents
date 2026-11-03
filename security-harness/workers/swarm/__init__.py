"""Swarm orchestration workers: seed, standup, reconcile, scribe, and the fact-post helper.

These wire the pure-logic engines (common.blackboard, common.allocator, common.chaining) into
Conductor tasks so the round-based collaboration loop (design/SWARM.md) runs as a durable workflow.
"""
from . import tasks  # noqa: F401  (importing registers the @worker_task functions)
