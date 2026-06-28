"""safety worker package: the independent safety governor.

The planner and executor must not be able to override the safety governor
(spec section 14). It lives in its own module/task so a campaign re-checks
authorization-window expiry, the operator kill-switch, and any accumulated
halt request at every pass boundary, and terminates the campaign if any trip.
Importing this module registers the @worker_task(s)."""

from . import tasks  # noqa: F401
