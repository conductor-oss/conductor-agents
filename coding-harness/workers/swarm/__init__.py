"""The simple swarm: an architect plans and judges, a team of agents builds in one shared tree.

The harness only provisions (a private clone, sessions, the board), fans out, measures the
architect's checks, and loops. Everything that needs judgment (splitting work, committing and
combining it, git refusals, port or file conflicts, wrong checks) is the agents' to reason about."""

from .tasks import (swarm_authoring, swarm_assemble, swarm_design_dispatch, swarm_design_fold, swarm_dispatch, swarm_gap_dispatch, swarm_finish, swarm_formal, swarm_gate, swarm_gate_apply,  # noqa: F401
                    swarm_item_prep, swarm_item_release, swarm_item_reviewing, swarm_verify_fork,
                    swarm_measure, swarm_round, swarm_setup)
from .link import swarm_ci, swarm_intake, swarm_pr_ready, swarm_publish  # noqa: F401,E402
from .approval import swarm_permissions  # noqa: F401,E402
