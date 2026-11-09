"""In-session feedback and commit-or-lose checkpoints (roadmap H11 + H17, closure plan WP2).

**Feedback reaches an agent while it works, not at the next standup.** A running coding session is a
single Conductor task, opaque to the workflow. The SDK's hooks are not: after every tool call a
``PostToolUse`` hook polls the board for directives addressed to this agent's item (or the team, or
``agent:<item>``) that are newer than the agent's prompt, and

  * **advisory** feedback (the default) is injected into the running session as ``additionalContext``
    — the model reads it before its next step, so latency is one tool call, inside the same item;
  * **blocking** feedback (``urgency: blocking`` — a human "stop / change direction now") ends the
    session (``continue: false``); the item comes back ``interrupted`` and is re-opened with the
    directive attached.

This answers roadmap §7 open question #3: advisory = next tool boundary, blocking = stop; the author
states urgency, no judge decides it.

**Checkpoints (H17).** Every ``checkpoint_every`` tool calls the same hook snapshots the worktree —
including new, untracked files — into ``refs/cc-checkpoints/<branch>`` as a commit built through a
private index, without touching the agent's branch, index, or working tree. A session that starts in a
clean worktree whose branch head is a checkpoint's parent restores that checkpoint first, so a lost
worker costs at most ``checkpoint_every`` tool calls of work, not the item.

The selection logic is pure; ``FeedbackWatcher`` holds the per-session state and the git helpers use
``common.git`` (``common/exec.py``) for every child process.
"""

from __future__ import annotations

import os
import tempfile
import time

TARGET_PREFIX_AGENT = "agent:"
CHECKPOINT_REF = "refs/cc-checkpoints/{branch}"


def _mapping(v) -> dict:
    return {str(k): val for k, val in v.items()} if isinstance(v, dict) else {}


def select(facts: list, item: str, *, since_ts: float, seen: set) -> dict:
    """Pure: new directives for ``item`` written after ``since_ts`` and not yet delivered (``seen`` holds
    fact keys already injected). Returns ``{advisory, blocking, latest_ts}``, each entry
    ``{key, text, authority, by, ts}``; human authority sorts first."""
    targets = {str(item), "team", f"{TARGET_PREFIX_AGENT}{item}"}
    advisory, blocking = [], []
    latest = float(since_ts or 0)
    for fact in facts or []:
        fact = _mapping(fact)
        d = _mapping(fact.get("data"))
        ts = float(fact.get("ts") or 0)
        key = f"{fact.get('author')}@{ts}"
        if ts <= float(since_ts or 0) or key in seen or str(d.get("target")) not in targets:
            continue
        if d.get("op") == "lift":
            continue
        from common import actors
        rec = {"key": key, "text": str(d.get("text") or ""),
               "authority": actors.authority_of(str(d.get("by") or fact.get("author") or "")),
               "by": str(d.get("by") or fact.get("author") or ""), "ts": ts}
        (blocking if str(d.get("urgency")) == "blocking" else advisory).append(rec)
        latest = max(latest, ts)
    rank = {"human": 0, "monitor": 1, "agent": 2}
    for lst in (advisory, blocking):
        lst.sort(key=lambda r: (rank.get(r["authority"], 3), r["ts"]))
    return {"advisory": advisory, "blocking": blocking, "latest_ts": latest}


def render(advisory: list, blocking: list) -> str:
    lines = ["NEW FEEDBACK arrived on the team board while you were working. It applies to your "
             "current task now; adjust course accordingly:"]
    for r in blocking + advisory:
        lines.append(f"- ({r['authority']}, {r['by']}) {r['text']}")
    return "\n".join(lines)


class FeedbackWatcher:
    """Per-session state for the PostToolUse hook: what was already injected, how many tool calls ran,
    which checkpoints were taken. ``read`` is injectable (tests); it returns directive facts."""

    def __init__(self, correlation_id: str, item: str, *, since_ts: float | None = None,
                 every: int = 1, checkpoint_every: int = 0, worktree: str = "", read=None):
        self.cid = str(correlation_id or "")
        self.item = str(item or "")
        self.since = float(since_ts) if since_ts else time.time()
        self.every = max(int(every or 0), 0)
        self.checkpoint_every = max(int(checkpoint_every or 0), 0)
        self.worktree = worktree
        self.calls = 0
        self.seen: set = set()
        self.injections: list[dict] = []
        self.checkpoints: list[dict] = []
        self.interrupted_by: dict | None = None
        self.stop_blocks = 0
        self._read = read

    @property
    def active(self) -> bool:
        return bool((self.cid and self.item and self.every) or (self.checkpoint_every and self.worktree))

    def _facts(self) -> list:
        if self._read is not None:
            return self._read(self.cid)
        from common import blackboard as bb
        return bb.read_facts(self.cid, kind="directive", since=self.since)

    # Live activity (state on demand): what this agent is doing right now, posted to the board at most
    # this often so a human, the UI, the API or another agent can ask for the current state at any time.
    ACTIVITY_INTERVAL_S = 10.0

    def _post_activity(self, tool: str) -> None:
        now = time.time()
        if not (self.cid and self.item) or now - getattr(self, "_activity_ts", 0.0) < self.ACTIVITY_INTERVAL_S:
            return
        self._activity_ts = now
        try:
            from common import blackboard as bb
            bb.post_fact(self.cid, f"implementer:{self.item}", "activity",
                         {"item": self.item, "toolCalls": self.calls, "tool": str(tool)[:200]})
        except Exception:  # noqa: BLE001 - a missed heartbeat never costs the session
            pass

    def on_tool_call(self, tool: str = "") -> dict:
        """Called after each tool call (synchronously, off the event loop). Returns the hook output."""
        self.calls += 1
        self._post_activity(tool)
        if self.checkpoint_every and self.worktree and self.calls % self.checkpoint_every == 0:
            snap = checkpoint(self.worktree, label=f"tool call {self.calls}")
            if snap:
                self.checkpoints.append({"call": self.calls, **snap})
        if not (self.cid and self.item and self.every) or self.calls % self.every:
            return {}
        picked = select(self._facts(), self.item, since_ts=self.since, seen=self.seen)
        if not picked["advisory"] and not picked["blocking"]:
            return {}
        now = time.time()
        for r in picked["advisory"] + picked["blocking"]:
            self.seen.add(r["key"])
            self.injections.append({"call": self.calls, "key": r["key"], "text": r["text"],
                                    "authority": r["authority"], "postedTs": r["ts"], "injectedTs": now,
                                    "latencySec": round(now - r["ts"], 3),
                                    "urgency": "blocking" if r in picked["blocking"] else "advisory"})
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": render(picked["advisory"], picked["blocking"])}}
        if picked["blocking"]:
            first = picked["blocking"][0]
            self.interrupted_by = {"key": first["key"], "text": first["text"], "by": first["by"]}
            if self.worktree:
                snap = checkpoint(self.worktree, label="interrupted")
                if snap:
                    self.checkpoints.append({"call": self.calls, **snap})
            out["continue_"] = False
            out["stopReason"] = f"stopped by blocking feedback from {first['by']}: {first['text']}"
        return out

    MAX_STOP_BLOCKS = 3

    def on_stop(self) -> dict:
        """Called when the agent tries to finish (Stop hook). Feedback that arrived during its final,
        tool-free turn would otherwise be missed (measured live: run 87381848). New advisory feedback
        keeps the session going (``decision: block`` + the feedback as the reason), at most
        ``MAX_STOP_BLOCKS`` times; blocking feedback lets it stop and marks the session interrupted."""
        if not (self.cid and self.item and self.every):
            return {}
        picked = select(self._facts(), self.item, since_ts=self.since, seen=self.seen)
        if not picked["advisory"] and not picked["blocking"]:
            return {}
        now = time.time()
        for r in picked["advisory"] + picked["blocking"]:
            self.seen.add(r["key"])
            self.injections.append({"call": self.calls, "key": r["key"], "text": r["text"],
                                    "authority": r["authority"], "postedTs": r["ts"], "injectedTs": now,
                                    "latencySec": round(now - r["ts"], 3), "at": "stop",
                                    "urgency": "blocking" if r in picked["blocking"] else "advisory"})
        if picked["blocking"]:
            first = picked["blocking"][0]
            self.interrupted_by = {"key": first["key"], "text": first["text"], "by": first["by"]}
            return {}
        if self.stop_blocks >= self.MAX_STOP_BLOCKS:
            return {}
        self.stop_blocks += 1
        return {"decision": "block", "reason": render(picked["advisory"], [])}

    def report(self) -> dict:
        return {"feedbackInjections": self.injections, "interruptedBy": self.interrupted_by,
                "checkpoints": self.checkpoints, "toolCalls": self.calls}


# ─────────────────────────────── checkpoints (git) ────────────────────────────
def _branch(worktree: str) -> str:
    from common import git
    return git.git(worktree, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()


def checkpoint(worktree: str, *, label: str = "") -> dict | None:
    """Snapshot the worktree (tracked + untracked, honoring .gitignore) as a commit whose parent is the
    branch head, recorded under ``refs/cc-checkpoints/<branch>`` (reflog keeps every snapshot). Uses a
    private index file, so the agent's index, working tree and branch are untouched. Returns
    ``{sha, ref}`` or None when the worktree has no change."""
    from common import git
    if not worktree or not os.path.isdir(worktree):
        return None
    status = git.git(worktree, "status", "--porcelain", check=False).stdout
    if not status.strip():
        return None
    branch = _branch(worktree)
    head = git.git(worktree, "rev-parse", "HEAD", check=False).stdout.strip()
    fd, index = tempfile.mkstemp(prefix="cc-ckpt-index-")
    os.close(fd)
    os.unlink(index)
    env = {"GIT_INDEX_FILE": index}
    try:
        git.git(worktree, "read-tree", "HEAD", env=env)
        git.git(worktree, "add", "-A", env=env)
        tree = git.git(worktree, "write-tree", env=env).stdout.strip()
    finally:
        if os.path.exists(index):
            os.unlink(index)
    sha = git.git(worktree, "-c", "user.name=conductor-checkpoint", "-c", "user.email=checkpoint@conductor.local",
                  "commit-tree", tree, "-p", head, "-m", f"checkpoint: {label}").stdout.strip()
    ref = CHECKPOINT_REF.format(branch=branch)
    git.git(worktree, "update-ref", "--create-reflog", "-m", label or "checkpoint", ref, sha)
    return {"sha": sha, "ref": ref}


def restore(worktree: str) -> dict | None:
    """If the worktree is clean and its branch head is the parent of the latest checkpoint, restore the
    checkpoint's files into the working tree (the branch head is not moved). Returns ``{sha, files}``."""
    from common import git
    if not worktree or not os.path.isdir(worktree):
        return None
    if git.git(worktree, "status", "--porcelain", check=False).stdout.strip():
        return None
    ref = CHECKPOINT_REF.format(branch=_branch(worktree))
    sha = git.git(worktree, "rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
    head = git.git(worktree, "rev-parse", "HEAD", check=False).stdout.strip()
    if not sha or git.git(worktree, "rev-parse", f"{sha}^1", check=False).stdout.strip() != head:
        return None
    files = [f for f in git.git(worktree, "diff", "--name-only", head, sha).stdout.splitlines() if f.strip()]
    git.git(worktree, "checkout", sha, "--", ".")
    git.git(worktree, "reset", "-q")  # keep the branch index at HEAD; restored files are working changes
    return {"sha": sha, "files": files}
