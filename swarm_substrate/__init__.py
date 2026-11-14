"""swarm_substrate — the generic, harness-agnostic swarm substrate.

A library for harness swarms. It exposes the append-only
blackboard (fact store, artifact store, lock-free budget keys, message bus); harness-specific read
models and orchestration stay in each harness.
"""

from . import blackboard

__all__ = ["blackboard"]
