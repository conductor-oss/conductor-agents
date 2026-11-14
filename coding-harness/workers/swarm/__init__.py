"""The simple swarm: an architect plans and judges, a team of agents builds in one shared tree.

The harness only provisions (a private clone, sessions, the board), fans out, measures the
architect's checks, and loops. Everything that needs judgment (splitting work, committing and
combining it, git refusals, port or file conflicts, wrong checks) is the agents' to reason about."""

from .tasks import (swarm_dispatch, swarm_finish, swarm_item_prep, swarm_item_release, swarm_item_reviewing,  # noqa: F401
                    swarm_measure, swarm_round, swarm_setup)
