"""The Architect's worker tasks: definition of done, check runs, evaluation, the convergence ladder.
Importing the module registers its ``@worker_task`` functions on the fleet."""

from .tasks import (architect_fold, contract_fold, contract_review_fold, contract_run, opinion_fold,
                    check_scout_build)

__all__ = ["check_scout_build", "contract_fold", "contract_review_fold", "contract_run", "architect_fold",
           "opinion_fold"]
