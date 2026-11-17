"""SDLC stage workflows (roadmap §3): the stage front door and depth paths built on the team cycle.

Phase 4: ``sdlc_intake`` (G1) and ``bug_fix`` (G2). Importing the module registers its
``@worker_task`` functions on the fleet."""

from .tasks import (bootstrap_init, bootstrap_outcome, bug_adopt, bug_outcome,
                    bug_repro_gate, intake_route, program_outcome,
                    repo_memory_read, repo_memory_write, retro_evidence, retro_record)

__all__ = ["intake_route", "bug_adopt", "bug_repro_gate", "bug_outcome",
           "bootstrap_init", "bootstrap_outcome",
           "repo_memory_read", "repo_memory_write", "program_outcome", "retro_evidence", "retro_record"]
