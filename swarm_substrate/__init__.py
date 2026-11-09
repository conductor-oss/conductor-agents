"""swarm_substrate — the generic, harness-agnostic swarm substrate.

A shared library for harness swarms, imported today by ``coding-harness``. It exposes the append-only
blackboard (fact store, artifact store, lock-free budget keys, message bus); harness-specific read
models and orchestration stay in each harness.
"""

from . import blackboard

__all__ = ["blackboard"]
