"""The construction team cycle (roadmap Phase 2) — the collaborating-swarm engine.

A distinct worker package (parallel to ``campaign``) holding the ``team_build`` workflow's workers:
the Lead's standup, the continuous-rebase integrator with a mutable tracking board, mid-run peer
review, and the honest delivered/incomplete outcome. Importing the module registers its
``@worker_task`` functions on the fleet."""

from .tasks import (team_apply_reviews, team_integrate, team_monitor, team_outcome,
                    team_plan_init, team_review_build, team_route, team_standup)

__all__ = [
    "team_route",
    "team_plan_init",
    "team_standup",
    "team_integrate",
    "team_review_build",
    "team_apply_reviews",
    "team_monitor",
    "team_outcome",
]
