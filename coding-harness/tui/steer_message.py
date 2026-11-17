"""Plain-language steering (§4.8, H11/H16): a human just sends a message; this works out where it goes.

The human should not have to pick an item, a key, an urgency, or an action type. ``send`` reads the
run's board (items, constraints in force, open exceptions, child teams), asks Claude only for the
ROUTING — which board action(s), on which run/item/constraint — and applies each through the
console's ``apply_steer``, the same authored board write every other surface makes.

The human's words are never rewritten: every directive carries the message verbatim, so nothing is
lost or distorted by interpretation, and pushback/lift/replace keep referring to what the human
actually said. Routing is validated against the board (unknown items/constraints are dropped). When
interpretation is unavailable or yields nothing valid, the message still lands — as an advisory
directive to the whole team, which reaches every working agent at its next tool boundary and every
later dispatch. A message is never dropped.
"""

from __future__ import annotations

import json
import os

MODEL = os.environ.get("SWARM_STEER_MODEL") or "claude-opus-5-5"

ACTIONS = ("nudge", "stop", "reopen", "lift", "replace", "terminate", "answer", "none")

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions", "reply"],
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "run", "item", "directive"],
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "run": {"type": "string"},
                    "item": {"type": "string"},
                    "directive": {"type": "string"},
                },
            },
        },
        "reply": {"type": "string"},
    },
}

_SYSTEM = """You route a human's steering message to an AI coding team's shared board. You decide only
WHERE it goes and WHAT KIND of board action it is. You never rewrite it: the human's exact words are
delivered with every action you choose.

Actions (each names a run id from the context):
- nudge: advisory guidance. item = an item id, or "team" when it applies to everyone or the target is unclear.
  Agents working that item read it at their next tool call; later dispatches carry it.
- stop: blocking. Interrupts the agent working the item now and re-opens it with the message. Use only
  when the human wants in-flight work halted or redirected immediately ("stop", "halt", "wrong, redo").
  item = an item id, or "team" to stop everyone.
- reopen: send a finished or blocked item back for rework (item required).
- lift: retire a constraint in force (directive = its id) when the human withdraws it.
- replace: the human changes an existing constraint (directive = the id it replaces; item = that constraint's target).
- terminate: end the whole run (or one child team's run) and its agents, only when the human clearly
  says to kill, terminate, abort or shut down the run. item = "".
- answer: the human is ASKING about the state or progress ("where are we?", "what is the hashes team
  doing?", "why is store blocked?"). Post nothing; the question is answered from the live state.
- none: the message is not work for the coding agents -- a request they cannot carry out
  (run, start, deploy or demo the software, give access, anything beyond writing code and tests in the
  repo). Post nothing; reply tells the human it was not sent to the team and why. If such a message
  also implies a code change (e.g. "the default port should be 1234"), route that part as usual.

Rules: send the message to every item whose work it changes, and only those; match on each item's
work and files, not just its name. A change to how code behaves goes to the item that implements
that code. Send it to a test-writing item only when the human asks for tests, or when that item's
work includes writing the code; a test-only item cannot change the code and would have to push back. A child team's item goes to that child run. Use "team"
when it applies to everyone or no item clearly owns it. Several actions are fine when the message has several
parts. The team makes its own decisions (contract edits, pushback, quarantines) — the human only gives
feedback, so never treat a message as an approval. Never terminate on a guess. reply = one short sentence telling the
human what will happen."""


def _context(run: str, view: dict, children: dict) -> dict:
    def items(v):
        return [{"id": it.get("id"), "state": it.get("state"),
                 "work": (it.get("description") or it.get("title") or "")[:400],
                 "files": list(it.get("files") or [])[:12]} for it in v.get("items") or []]

    def constraints(v):
        return [{"id": d.get("id"), "target": d.get("target"), "text": (d.get("text") or "")[:120],
                 "by": d.get("by")} for d in v.get("constraints") or [] if d.get("id")]

    def exceptions(v):
        return [{"item": e.get("item"), "kind": e.get("kind"), "detail": str(e.get("detail") or "")[:120]}
                for e in v.get("exceptions") or []]

    ctx = {"run": run, "items": items(view), "constraints": constraints(view),
           "openExceptions": exceptions(view), "childTeams": []}
    for c in view.get("children") or []:
        cv = children.get(c.get("run")) or {}
        ctx["childTeams"].append({"run": c.get("run"), "subsystem": c.get("subsystem"), "items": items(cv),
                                  "constraints": constraints(cv), "openExceptions": exceptions(cv)})
    return ctx


def _ask_claude(message: str, ctx: dict, client=None) -> dict:
    """One structured-output call; returns ``{"actions": [...], "reply": str}``."""
    import anthropic
    client = client or anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
        system=_SYSTEM,
        messages=[{"role": "user", "content":
                   f"Board context:\n{json.dumps(ctx, default=str)}\n\n<message>\n{message}\n</message>"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("interpretation declined")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


def _valid(actions: list, run: str, ctx: dict) -> list[dict]:
    """Keep only actions that name a real run/item/constraint on the board."""
    boards = {run: ctx}
    for c in ctx["childTeams"]:
        boards[str(c["run"])] = c
    out = []
    for a in actions or []:
        act, r = str(a.get("action") or ""), str(a.get("run") or run)
        b = boards.get(r)
        if act not in ACTIONS or b is None:
            continue
        item, directive = str(a.get("item") or ""), str(a.get("directive") or "")
        items = {str(i["id"]) for i in b["items"]}
        dirs = {str(d["id"]): d for d in b["constraints"]}
        if act in ("nudge", "stop"):
            if item != "team" and item not in items:
                continue
        elif act == "reopen":
            if item not in items:
                continue
        elif act == "terminate":
            item = ""
        elif act in ("lift", "replace"):
            if directive not in dirs:
                continue
            item = item or str(dirs[directive].get("target") or "")
        out.append({"action": act, "run": r, "item": item, "directive": directive})
    return out


def send(run: str, message: str, *, author: str, blackboard_dir: str | None = None, client=None) -> dict:
    """Route and apply one plain-language steering message. Returns what was done, for the human."""
    try:
        from . import console as console_mod
    except ImportError:  # loaded as a top-level module next to console.py (the stdlib console server)
        import console as console_mod
    message = (message or "").strip()
    if not message:
        return {"ok": False, "error": "empty message"}
    view = console_mod.board_json(run, blackboard_dir=blackboard_dir)
    children = {c["run"]: console_mod.board_json(c["run"], blackboard_dir=blackboard_dir)
                for c in view.get("children") or [] if c.get("run")}
    ctx = _context(run, view, children)
    interpreted_by, reply, note = "claude", "", ""
    try:
        decided = _ask_claude(message, ctx, client)
        raw = decided.get("actions") or []
        reply = str(decided.get("reply") or "")
        if any(str(a.get("action")) == "answer" for a in raw):
            # A question about the state: answered from the live snapshot, nothing posted.
            try:
                from . import swarm_state
            except ImportError:
                import swarm_state
            got = swarm_state.ask(run, message, blackboard_dir=blackboard_dir, client=client)
            return {"ok": bool(got.get("ok")), "interpretedBy": "claude", "reply": got.get("answer") or "",
                    "note": "answered from the live state", "actions": [], "author": author,
                    "answer": got.get("answer") or "", **({"error": got["error"]} if got.get("error") else {})}
        if raw and all(str(a.get("action")) == "none" for a in raw):
            # A deliberate "not team work" decision: nothing is posted, the reply says why.
            return {"ok": True, "interpretedBy": "claude", "reply": reply, "note": "not sent to the team",
                    "actions": [], "author": author}
        actions = _valid([a for a in raw if str(a.get("action")) not in ("none", "answer")], run, ctx)
        if not actions:
            note = "no board action matched; sent to the whole team"
    except Exception as exc:  # noqa: BLE001 - any interpretation failure still delivers the message
        actions, note = [], f"interpretation unavailable ({type(exc).__name__}); sent to the whole team"
    if not actions:
        interpreted_by = "fallback"
        actions = [{"action": "nudge", "run": run, "item": "team", "directive": ""}]
        reply = reply if reply and not note else "Sent to the whole team as guidance."
    # Re-sending a message must not stack identical constraints on the same target.
    live = {(str(d.get("target")), str(d.get("text"))) for d in view.get("constraints") or []}
    for c in children.values():
        live |= {(str(d.get("target")), str(d.get("text"))) for d in c.get("constraints") or []}
    # A reopen alone records only that the item went back; the agent that picks it up must also see
    # WHAT to change, so the human's words travel with it as a directive on that item.
    expanded = []
    for a in actions:
        if a["action"] == "reopen":
            expanded.append({**a, "action": "nudge"})
        expanded.append(a)
    results = []
    for a in expanded:
        if a["action"] == "nudge" and (a["item"], message) in live:
            results.append({**a, "ok": True, "duplicate": True})
            continue
        res = console_mod.apply_steer({**a, "text": message}, blackboard_dir=blackboard_dir, author=author)
        results.append({**a, "ok": bool(res.get("ok")), **({"error": res.get("error")} if not res.get("ok") else {})})
    return {"ok": all(r["ok"] for r in results), "interpretedBy": interpreted_by, "reply": reply,
            "note": note, "actions": results, "author": author}


def describe(result: dict) -> str:
    """One line for the human: what happened to their message."""
    if not result.get("actions"):
        if result.get("ok"):
            return f"{result.get('reply') or ''} [{result.get('note') or 'nothing posted'}]".strip()
        return f"not sent: {result.get('error')}"
    parts = []
    for a in result["actions"]:
        where = a.get("item") or a.get("directive") or "run"
        parts.append(f"{a['action']} → {where}" + (" (already had it)" if a.get("duplicate") else "")
                     + ("" if a.get("ok") else f" (failed: {a.get('error')})"))
    tail = f" · {result['note']}" if result.get("note") else ""
    return f"{result.get('reply') or ''} [{', '.join(parts)}]{tail}".strip()
