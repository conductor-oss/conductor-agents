"""Board-aware TUI layer (roadmap Phase 2, §4.8) — a view + editor over the team's shared boards.

The TUI is otherwise a read-mostly Conductor REST client, but the team cycle's shared state does not
live in Conductor — it lives on the append-only blackboard (``common.blackboard``). §4.8's principle is
that *the boards are the single source of truth and every UI is a view + editor over them*: a human
action in any surface is a **board write, the exact write a monitor or agent makes**. So this module
reuses the workers' pure board read-model (``common.team_board``) rather than reimplementing it, and
steering is literally ``post_directive`` / ``set_state`` authored by ``human``.

Kept free of Textual so the projection and steering logic is unit-testable headlessly; the Textual
screen in ``tui/screens/board.py`` is a thin renderer over ``project_board`` + the steering helpers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The board read-model and blackboard live in the workers package. Add it to the path, so the TUI
# reuses exactly the fold the Lead/monitor use — no divergent second implementation.
_WORKERS = Path(__file__).resolve().parents[1] / "workers"
if str(_WORKERS) not in sys.path:
    sys.path.insert(0, str(_WORKERS))

from common import actors, budget_gate, file_ownership, team_board  # noqa: E402


def current_author() -> str:
    """Who is steering from this TUI (H16): the registered actor for ``SWARM_ACTOR_TOKEN``, stamped as
    ``human:<email>``. Without a registered token the write is still recorded, but as plain ``human`` —
    which workers rank as an agent, never as a human with override authority."""
    actor = actors.resolve(os.environ.get("SWARM_ACTOR_TOKEN"))
    return actor["author"] if actor else "human"


def _default_blackboard_dir() -> str:
    return os.environ.get("SWARM_BLACKBOARD_DIR") or str(_WORKERS / "state" / "blackboard")


def _use_board_dir(blackboard_dir: str | None) -> None:
    os.environ["SWARM_BLACKBOARD_DIR"] = blackboard_dir or _default_blackboard_dir()


# ─────────────────────────────── projection (read) ────────────────────────────
def project_board(correlation_id: str, *, blackboard_dir: str | None = None) -> dict:
    """Fold a run's board into a human-facing view: the rollup (H7), items sorted for a kanban, the
    learning/review boards, shared-file owners, and aggregate spend. Pure read over the blackboard."""
    _use_board_dir(blackboard_dir)
    board = team_board.fold_board(correlation_id)
    rollup = team_board.board_rollup(board)
    order = {team_board.IN_PROGRESS: 0, team_board.REVIEW: 1, team_board.REOPENED: 2,
             team_board.READY: 3, team_board.BACKLOG: 4, team_board.BLOCKED: 5, team_board.DONE: 6}
    items = sorted(board["items"].values(),
                   key=lambda it: (order.get(it.get("state"), 9),
                                   -float(it.get("priority") or 0), str(it.get("id"))))
    return {
        "correlationId": correlation_id,
        "rollup": rollup,
        "items": items,
        "lessons": board["lessons"],
        "reviews": board["reviews"],
        "directives": board["directives"],
        "owners": file_ownership.owner_map(correlation_id),
        "spend": budget_gate.read_spend(correlation_id),
    }


# ─────────────────────────────── steering (write) ─────────────────────────────
# Every steering action is an authored board write — the same wire an agent/monitor writes on (§4.4).
# The human author carries override authority (H16); it is recorded, never a side channel.
def nudge_item(correlation_id: str, item_id: str, text: str, *, author: str | None = None,
               sticky: bool = True, urgency: str = "advisory", blackboard_dir: str | None = None) -> str:
    """Post a steering directive/constraint to one item. An agent already working the item takes it at
    its next tool boundary (advisory) or is stopped by it (blocking); otherwise the next standup injects
    it into whoever works the item. A sticky directive persists until lifted or superseded."""
    _use_board_dir(blackboard_dir)
    author = author or current_author()
    return team_board.post_directive(correlation_id, author, item_id, text,
                                     authority="human" if author.startswith("human") else "agent",
                                     sticky=sticky, urgency=urgency)


def reprioritize(correlation_id: str, item_id: str, priority: float, *, author: str | None = None,
                 blackboard_dir: str | None = None) -> str:
    """Re-prioritize an item by appending an updated item fact (the fold takes the latest). The Lead's
    next standup orders ready work by this priority."""
    _use_board_dir(blackboard_dir)
    author = author or current_author()
    cur = team_board.fold_board(correlation_id)["items"].get(item_id, {"id": item_id})
    return team_board.post_item(correlation_id, author, {**cur, "priority": priority})


def reopen_item(correlation_id: str, item_id: str, reason: str = "human reopened", *,
                author: str | None = None, blackboard_dir: str | None = None) -> str:
    """Reopen an item so the next standup re-dispatches it (e.g., after human review)."""
    _use_board_dir(blackboard_dir)
    author = author or current_author()
    return team_board.set_state(correlation_id, author, item_id, team_board.REOPENED, reason=reason)


def terminate_run(run_id: str, reason: str = "", *, author: str | None = None,
                  server_url: str | None = None, opener=None) -> dict:
    """Terminate a run (a team, or a whole program with its child teams) through the Conductor API — the
    human's hard stop — then start its retrospective. Humans are passive observers of the swarm: they
    watch, give feedback, and can terminate; they never approve.

    The retrospective is started here because Conductor runs a workflow's ``failureWorkflow`` only when
    the run FAILS, never when it is terminated through the API (measured on this server, with and without
    ``triggerFailureWorkflow``). ``opener`` is injectable for tests."""
    import json as _json
    import urllib.parse as _up
    import urllib.request as _rq
    base = (server_url or os.environ.get("CONDUCTOR_SERVER_URL") or "http://localhost:8080/api").rstrip("/")
    token = os.environ.get("CONDUCTOR_AUTH_TOKEN", "")
    headers = {"Content-Type": "application/json", **({"X-Authorization": token} if token else {})}
    open_ = opener or _rq.urlopen
    author = author or current_author()
    why = f"terminated by {author}" + (f": {reason}" if reason else "")
    with open_(_rq.Request(f"{base}/workflow/{run_id}?includeTasks=false", headers=headers)) as resp:  # noqa: S310
        raw = resp.read() if hasattr(resp, "read") else b"{}"
    wf_input = (_json.loads(raw or b"{}") or {}).get("input") or {}
    with open_(_rq.Request(f"{base}/workflow/{run_id}?reason={_up.quote(why)}", method="DELETE",
                           headers=headers)):  # noqa: S310 - operator-configured Conductor URL
        pass
    retro = ""
    if not wf_input.get("parentCorrelationId"):
        body = _json.dumps({"workflowId": run_id, "repoPath": wf_input.get("repoPath") or "",
                            "verdict": "TERMINATED", "report": why}).encode()
        with open_(_rq.Request(f"{base}/workflow/run_retrospective", data=body, method="POST",
                               headers=headers)) as resp:  # noqa: S310
            retro = (resp.read() if hasattr(resp, "read") else b"").decode().strip().strip('"')
    return {"terminated": True, "run": run_id, "reason": why, "retrospective": retro}


def lift_constraint(correlation_id: str, directive_id: str, reason: str = "", *, author: str | None = None,
                    blackboard_dir: str | None = None) -> str:
    """Lift a sticky constraint by id (H16 lifecycle) — it stops reaching any agent from the next tool
    boundary / standup on."""
    _use_board_dir(blackboard_dir)
    return team_board.lift_directive(correlation_id, author or current_author(), directive_id, reason)


def replace_constraint(correlation_id: str, directive_id: str, item_id: str, text: str, *,
                       author: str | None = None, blackboard_dir: str | None = None) -> str:
    """Replace a constraint: a new directive that supersedes the old one (H16 lifecycle)."""
    _use_board_dir(blackboard_dir)
    author = author or current_author()
    return team_board.post_directive(correlation_id, author, item_id, text,
                                     authority="human" if author.startswith("human") else "agent",
                                     supersedes=directive_id)
