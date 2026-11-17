"""Workers of the ``swarm`` workflow. Each is plumbing: no policy, no classification of failures."""
from __future__ import annotations

import json
import os
import re

from conductor.client.worker.worker_task import worker_task

from common import git
from common.results import fail, ok

CLONES = ".cc-worktrees"
STALL_AFTER_S = 300

# Swarm agents own their git: they commit, rebase and resolve conflicts themselves. They never push,
# never reach the web, never escalate privileges.
SWARM_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite"]
SWARM_DISALLOWED_TOOLS = ["WebSearch", "WebFetch", "Bash(git push*)", "Bash(sudo *)"]

ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"}, "brief": {"type": "string"},
            "kind": {"type": "string"}, "rung": {"type": "integer"},
            "dependsOn": {"type": "array", "items": {"type": "string"}},
            # The paths this item may write beyond its own code: a design item's document and stubs, or one
            # of the lead's files the lead hands to this item (swarm/team.py).
            "owns": {"type": "array", "items": {"type": "string"}},
            # The earlier items this one redoes or fixes: their reviews accepted work that was not finished.
            "replaces": {"type": "array", "items": {"type": "string"}},
            # The one small item that writes a compiled project's module file and shared core types: every other
            # build item of its round waits for it (_dispatch).
            "core": {"type": "boolean"}},
            "required": ["id", "title", "brief", "kind", "rung", "dependsOn"]}},
    },
    "required": ["items"],
}
from common.permissions import SCHEMA as _PERMISSIONS_SCHEMA  # noqa: E402

WORK_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "done": {"type": "boolean"},
                   "files": {"type": "array", "items": {"type": "string"}},
                   "blockers": {"type": "array", "items": {"type": "string"}},
                   "lessons": {"type": "array", "items": {"type": "string"}},
                   # What the agent needed and was refused (docs/PERMISSIONS.md), instead of retrying it.
                   "permissions": _PERMISSIONS_SCHEMA},
    "required": ["summary", "done", "files"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"accepted": {"type": "boolean"}, "notes": {"type": "string"},
                   "fixed": {"type": "array", "items": {"type": "string"}}},
    "required": ["accepted", "notes", "fixed"],
}
ROUND_KIND, REPORT_KIND, CLAIM_KIND = "swarm_round", "swarm_report", "swarm_claim"
GATE_KIND = "swarm_gate"
SMOKE_KIND = "swarm_smoke"
FIXER_KIND = "smoke-fix"
MAX_FIXERS_PER_ROUND = 2
# A build item its review (or the smoke check) does not accept goes straight back to an agent, one rung up, up to
# this many times in its round, instead of waiting for the round review (run f1115a84: items sat stuck 7-9 min).
MAX_ITEM_REWORKS = 2
# An item its agent could not finish because another item of the round has not landed yet is waiting, not stuck: it
# restarts on the same rung the moment that item is accepted (run bf3f5879: four of five climbs were agents waiting on
# a sibling's code, and the stronger rung did nothing the landed sibling did not).
WAIT_KIND, WAKE_KIND = "swarm_wait", "swarm_wake"
MAX_ITEM_WAITS = 2
SMOKE_STALL_S, SMOKE_EXPECTED_S, SMOKE_RETRY_AFTER_S = 60, 180, 20
MAX_SMOKE_CHECKS = 2
AUTHOR_MIN_USD = 1.5
# Integrating a round is the same job whatever the repository: the round review's ladder learns across
# all of them (every run of a request in a fresh folder would otherwise start from nothing).
ROUND_REVIEW_REPO = "*any repository*"
# The architect's plan: the items, and the acceptance checks it decided on, each written afterwards by
# its own author session (planning took 17+ minutes when one session also wrote every check).
from common import stacks as _stacks_mod

_STACKS_FIELD = {"type": "array", "items": {"type": "string", "enum": _stacks_mod.ids()}}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        **ITEMS_SCHEMA["properties"],
        "checks": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "file": {"type": "string"}, "command": {"type": "string"},
            "scenario": {"type": "string"},
            # A SMOKE check (start the program, answer one request): the harness runs it as each item lands.
            "smoke": {"type": "boolean"},
            # The OpenSpec scenarios this check proves (by name); empty without an OpenSpec spec.
            "covers": {"type": "array", "items": {"type": "string"}}},
            "required": ["id", "file", "command", "scenario", "covers"]}},
        # Checks the repository already has that this request makes wrong or obsolete.
        "retireChecks": {"type": "array", "items": {"type": "string"}},
        "spec": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["tla", "openspec", "none"]},
                                                  "why": {"type": "string"}}, "required": ["kind", "why"]},
        "design": {"type": "object", "properties": {"docs": {"type": "array", "items": {"type": "string"}},
                                                    "why": {"type": "string"}}, "required": ["docs", "why"]},
        # The platform packs this request uses (common/stacks.py): their caches, hosts, tools and test limits.
        "stacks": _STACKS_FIELD,
        # Which independent verifiers look at the build once it is green (swarm/verify.py).
        "verification": {"type": "object", "properties": {"focuses": {"type": "array", "items": {"type": "string"}},
                                                          "why": {"type": "string"}}, "required": ["focuses", "why"]},
    },
    "required": ["items", "checks", "retireChecks", "spec", "design", "stacks", "verification"],
}
# The lead's kickoff: the parts of the system, one architect each (swarm/team.py); none for a small change.
KICKOFF_SCHEMA = {
    "type": "object",
    "properties": {"team": {"type": "boolean"}, "why": {"type": "string"},
                   "spec": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["tla", "openspec", "none"]},
                                                             "why": {"type": "string"}}, "required": ["kind", "why"]},
                   "stacks": _STACKS_FIELD,
                   "parts": {"type": "array", "items": {"type": "object", "properties": {
                       "id": {"type": "string"}, "title": {"type": "string"}, "brief": {"type": "string"},
                       "owns": {"type": "string"}},
                       "required": ["id", "title", "brief", "owns"]}}},
    "required": ["team", "why", "spec", "stacks", "parts"],
}
# What one part's architect reports: its design, and what it proposes the lead plan for its part.
DESIGN_SCHEMA = {
    "type": "object",
    "properties": {**WORK_SCHEMA["properties"],
                   "provides": {"type": "string"}, "requires": {"type": "string"},
                   "proposedItems": ITEMS_SCHEMA["properties"]["items"],
                   "proposedChecks": PLAN_SCHEMA["properties"]["checks"]},
    "required": ["summary", "done", "files", "provides", "requires", "proposedItems", "proposedChecks"],
}
# The lead's integration of a design team: decisions and CHANGES to the team's proposals, never a re-typed plan
# (integration took 9.4 of its minutes re-typing 41 item briefs and 39 checks the architects had proposed, run 3b832808).
INTEGRATE_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {"type": "string"},
        # Missing pieces, one gap-filler architect each, non-overlapping (the lead's review finds them).
        "gaps": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"}, "brief": {"type": "string"}, "owns": {"type": "string"}},
            "required": ["id", "title", "brief", "owns"]}},
        "smokeChecks": {"type": "array", "items": {"type": "string"}},
        "dropItems": {"type": "array", "items": {"type": "string"}},
        "dropChecks": {"type": "array", "items": {"type": "string"}},
        "items": ITEMS_SCHEMA["properties"]["items"],
        "checks": PLAN_SCHEMA["properties"]["checks"],
        **{k: PLAN_SCHEMA["properties"][k] for k in ("retireChecks", "spec", "design", "stacks", "verification")},
    },
    "required": ["decisions", "gaps", "dropItems", "dropChecks", "items", "checks", "retireChecks", "spec", "design",
                 "verification"],
}
EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {"delivered": {"type": "boolean"}, "assessment": {"type": "string"},
                   **ITEMS_SCHEMA["properties"],
                   # The requests the architect judges necessary; the run waits for each to be decided.
                   "permissions": _PERMISSIONS_SCHEMA},
    "required": ["delivered", "assessment", "items"],
}


def _mapping(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _list(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def _ladder_top(resolution, role: str = "code") -> int:
    """The highest rung of a role's ladder in the resolved model profile (0 when it has one tier)."""
    tiers = ((_mapping(resolution).get("roles") or {}).get(role) or {}).get("tiers") or []
    return max(len(tiers) - 1, 0)


def _ladder_text(resolution, role: str = "code") -> str:
    tiers = ((_mapping(resolution).get("roles") or {}).get(role) or {}).get("tiers") or []
    return "; ".join(f"rung {n}: {t.get('model')} at {t.get('effort') or 'default'} effort"
                     for n, t in enumerate(tiers) if isinstance(t, dict)) or "one rung"


def _history_text(repo: str) -> str:
    """What earlier runs on this repository measured, per kind of item and rung: done or not."""
    from common import rung_policy
    from common import blackboard as bb
    key = rung_policy.repo_key(repo)
    tally: dict[tuple[str, int], list[int]] = {}
    for f in bb.read_facts(rung_policy.OUTCOME_RUN, kind=rung_policy.OUTCOME_KIND):
        d = _mapping(f.get("data"))
        if d.get("repo") == key and d.get("role") == "code" and rung_policy.agent_fixable(d.get("cause")):
            done, total = tally.setdefault((str(d.get("kind")), int(d.get("rung") or 0)), [0, 0])
            tally[(str(d.get("kind")), int(d.get("rung") or 0))] = [done + bool(d.get("ok")), total + 1]
    if not tally:
        return "No earlier runs on this repository."
    return "\n".join(f"- {kind} items on rung {rung}: {done} of {total} done in one session"
                     for (kind, rung), (done, total) in sorted(tally.items()))


def _verify_history_text(repo: str) -> str:
    """How the lead's rulings went for each verifier on this repository: findings fixed of those raised.
    A verifier whose findings this repository keeps rejecting is noise here; the lead skips it."""
    from common import rung_policy
    from common import blackboard as bb
    key = rung_policy.repo_key(repo)
    tally: dict[str, list[int]] = {}
    for f in bb.read_facts(rung_policy.OUTCOME_RUN, kind=rung_policy.OUTCOME_KIND):
        d = _mapping(f.get("data"))
        if d.get("repo") == key and d.get("role") == "verify":
            fixed, total = tally.get(str(d.get("kind")), [0, 0])
            tally[str(d.get("kind"))] = [fixed + bool(d.get("ok")), total + 1]
    if not tally:
        return "No verifier has looked at this repository before."
    return "\n".join(f"- {focus}: {fixed} of {total} standing findings were fixed (the rest deferred or rejected)"
                     for focus, (fixed, total) in sorted(tally.items()))


def _domains(granted: list, work: str = "") -> list[str]:
    """The hosts a session may reach: the harness's defaults, the package hosts of the run's platform packs
    (common/stacks.py) and what an approver granted."""
    from common import stacks
    from common.allowed_domains import DEFAULT_ALLOWED_DOMAINS
    packs = stacks.hosts(stacks.chosen(work)) if work else []
    return sorted(set(DEFAULT_ALLOWED_DOMAINS) | set(packs) | set(granted or []))


def _granted(run: str, work: str = "") -> dict:
    """What approvers and the policy granted the run, for the round review's session."""
    from common import permissions
    g = permissions.grants(run) if run else {"writableRoots": [], "allowedDomains": [], "text": "(none)"}
    return {"writableRoots": g["writableRoots"], "allowedDomains": _domains(g["allowedDomains"], work),
            "permissions": g["text"]}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "work"


def _ref(text: str) -> str:
    """A task-name part for an id: its slug, and when that had to be cut, a short hash of the whole id, so two ids
    sharing their first 40 characters never get one name (Conductor refuses a fork that repeats one: two check
    authors `author_check_primary-dashboard-design-primary-dashboa`)."""
    full = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "work"
    if len(full) <= 40:
        return full
    import hashlib
    return full[:33].rstrip("-") + "-" + hashlib.sha1(str(text).encode()).hexdigest()[:6]


def _restore_harness_files(work: str, repo: str, ref: str) -> None:
    """A follow-up on a pull request starts from the pull request's head, which carries no harness files
    (publishing leaves them out); bring back the previous run's ``.cc/`` and repository memory from the
    full commit it kept (``restoreHarnessFrom``), so its spec, checks and adapter carry on."""
    if not ref:
        return
    git.git(work, "fetch", "--quiet", "--no-tags", repo, ref)
    for path in HARNESS_PATHS:
        if git.git(work, "cat-file", "-e", f"FETCH_HEAD:{path}", check=False).code == 0:
            git.git(work, "checkout", "FETCH_HEAD", "--", path)


# The coding factory's own files in a repository: specs, checks, adapters, the brief, repository memory.
HARNESS_PATHS = (".cc", ".conductor-code")


@worker_task(task_definition_name="swarm_setup")
def swarm_setup(task):
    """A private clone of the repository for the run, so agents can commit inside their sandbox."""
    i = task.input_data or {}
    try:
        repo = os.path.abspath(str(i["repoPath"]))
        run_id = str(i.get("runId") or task.workflow_instance_id)
        git.ensure_ready(repo)
        git.exclude_path(repo, f"/{CLONES}/")
        work = os.path.join(repo, CLONES, f"swarm-{run_id}")
        # The console names the branch after the request (branchName); the run id keeps runs apart.
        named = str(i.get("branchName") or "").strip().strip("/")
        branch = f"{named}-{run_id[:8]}" if named else f"swarm/{_slug(i.get('description') or 'work')}-{run_id[:8]}"
        original_branch = git.git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
        original_head = git.head(repo)
        from swarm import spec as spec_mod
        specs, design = spec_mod.spec_mode(i.get("specs")), spec_mod.design_mode(i.get("design"))
        if not os.path.isdir(os.path.join(work, ".git")):
            git.git(repo, "clone", "--quiet", "--no-local", repo, work)
            # startFrom names a ref in the repository the run starts from (a base branch's tip or a pull
            # request's head, fetched by intake); without it the run starts from the checkout's HEAD.
            start = str(i.get("startFrom") or "").strip()
            if start:
                git.git(work, "fetch", "--quiet", "--no-tags", repo, start)
                git.git(work, "switch", "-c", branch, "FETCH_HEAD")
            else:
                git.git(work, "switch", "-c", branch)
            _restore_harness_files(work, repo, str(i.get("restoreHarnessFrom") or "").strip())
        migrated = spec_mod.migrate_legacy(work)
        # The run's skills, settled once: every agent of the run is offered the same ones. A name nobody
        # installed stops the run here rather than quietly running without it.
        from common import skills as skills_mod
        chosen, missing = skills_mod.choose(skills_mod.discover(work), i.get("skills"))
        if missing:
            installed = ", ".join(sorted(s.name for s in skills_mod.discover(work))) or "none"
            raise ValueError(f"skill(s) not installed on this worker: {', '.join(missing)} (installed: {installed})")
        left_out = [s.name for s in chosen if skills_mod.too_large(s)]
        if left_out and skills_mod.selection(i.get("skills")) != "all":
            raise ValueError(f"skill(s) over {skills_mod.MAX_SKILL_BYTES // 2**20} MB, not copied: {', '.join(left_out)}")
        chosen = [s for s in chosen if s.name not in left_out]
        from swarm import team as team_mod
        from swarm import verify as verify_mod
        architects, verifiers = team_mod.architects_mode(i.get("architects")), verify_mod.verifiers_mode(i.get("verifiers"))
        have = spec_mod.existing_kind(work)
        guide = spec_mod.plan_guide(specs, design, have, spec_mod.read_checks(work), spec_mod.design_docs(work))
        resume = _resume_phase(work, i)
        for key in ("user.name", "user.email"):
            value = git.git(repo, "config", key, check=False).stdout.strip()
            git.git(work, "config", key, value or ("conductor-swarm" if key == "user.name" else "swarm@localhost"))
        resolution = i.get("modelResolution") or {}
        return ok(task, {"workdir": work, "branch": branch, "base": git.head(work),
                         "originalBranch": original_branch, "originalHead": original_head,
                         "ladder": _ladder_text(resolution), "maxRung": _ladder_top(resolution),
                         "history": _history_text(repo), "specs": specs, "design": design,
                         "existingSpec": have, "migratedSpec": migrated, **guide,
                         "skills": [s.name for s in chosen], "skillsLeftOut": left_out,
                         "architects": architects, "verifiers": verifiers, "teamGuide": team_mod.team_guide(architects), "kickoffGuide": team_mod.kickoff_guide(architects),
                         "stacksGuide": _stacks_mod.guide(_stacks_mod.detect(work)),
                         "specChoice": ({"tla": 'The person asked for a TLA+ spec: return spec.kind "tla".',
                                         "openspec": 'The person asked for an OpenSpec spec: return spec.kind "openspec".',
                                         "none": 'The person asked for no spec: return spec.kind "none".'}.get(specs)
                                        or ('Choose the spec: "openspec" (requirements and scenarios, each proven by an '
                                            'acceptance check) for a system whose behaviour a user observes command by '
                                            'command; "tla" (a state machine the program is replayed against) for '
                                            'concurrency, ordering or recovery the checks cannot exhaust; "none" for a '
                                            'small change the checks state completely.')),
                         "verifyGuide": verify_mod.plan_guide(verifiers), "verifyHistory": _verify_history_text(repo),
                         "precedence": team_mod.PRECEDENCE, "schemas": {
            "items": ITEMS_SCHEMA, "plan": PLAN_SCHEMA, "work": WORK_SCHEMA, "evaluate": EVALUATE_SCHEMA,
            "rule": _rule_schema(), "kickoff": KICKOFF_SCHEMA, "integrate": INTEGRATE_SCHEMA},
            # No design team when the person asked for no design (its whole output is design documents) or one architect.
            "team": "solo" if team_mod.designer_cap(architects) == 0 or design == "none" or resume == "plan" else "team",
            "resume": resume, "resumeNote": RESUME_NOTE if resume == "build" else "",
            "allowedTools": SWARM_ALLOWED_TOOLS, "disallowedTools": SWARM_DISALLOWED_TOOLS},
            [f"[swarm_setup] {work} on {branch}; architects: {architects}; verifiers: {verifiers}; "
             f"skills: {', '.join(s.name for s in chosen) or 'none'}"
             + (f"; left out (over {skills_mod.MAX_SKILL_BYTES // 2**20} MB): {', '.join(left_out)}" if left_out else "")])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_setup", e)


def _start_order(items: list[dict], deps: dict, team_mod) -> list[dict]:
    """The order items take free slots in: design items, then the items most others wait on (directly or not), then
    the plan's order."""
    waiting_on: dict[str, int] = {}
    for iid in deps:
        seen, frontier = set(), set(deps[iid])
        while frontier:
            seen |= frontier
            frontier = {d for f in frontier for d in deps.get(f, [])} - seen
        for d in seen:
            waiting_on[d] = waiting_on.get(d, 0) + 1
    rank = {it["id"]: n for n, it in enumerate(items)}
    return sorted(items, key=lambda x: (not team_mod.is_design(x), -waiting_on.get(x["id"], 0), rank[x["id"]]))


def _graph(items: list[dict], board_items: dict) -> tuple[dict, dict]:
    """``(deps, unstartable)``: each item's dependencies inside the round, and the items that can never
    start this round, with why (an unknown id, a cycle, or depending on one of those). A dependency on an
    item from an earlier round counts as met when that item is done on the board."""
    ids = {it["id"] for it in items}
    deps, unstartable = {}, {}
    for it in items:
        mine = []
        for d in (str(x).strip() for x in _list(it.get("dependsOn"))):
            if not d or d == it["id"]:
                continue
            if d in ids:
                mine.append(d)
            elif d not in board_items:
                unstartable[it["id"]] = f"depends on '{d}', which is not an item of this run"
            elif board_items[d].get("state") != "done":
                unstartable[it["id"]] = f"depends on '{d}' from an earlier round, which is not done"
        deps[it["id"]] = mine
    # A cycle never resolves: every item on one waits forever (Kahn's algorithm leaves them behind).
    indegree = {i: len(d) for i, d in deps.items()}
    order = [i for i, n in indegree.items() if n == 0]
    for i in order:
        for j, d in deps.items():
            if i in d:
                indegree[j] -= 1
                if indegree[j] == 0:
                    order.append(j)
    left = {i for i, n in indegree.items() if n > 0}

    def reaches_itself(start: str) -> bool:
        seen, stack = set(), list(deps[start])
        while stack:
            x = stack.pop()
            if x == start:
                return True
            if x in left and x not in seen:
                seen.add(x)
                stack.extend(deps[x])
        return False
    # Only the items ON a cycle are named as such; what merely depends on one is caught below.
    cycle = sorted(i for i in left if reaches_itself(i))
    for i in cycle:
        unstartable.setdefault(i, "is on a dependency cycle (" + ", ".join(cycle) + ")")
    # Whatever depends on an unstartable item cannot start either.
    changed = True
    while changed:
        changed = False
        for i, d in deps.items():
            if i not in unstartable and any(x in unstartable for x in d):
                unstartable[i] = "depends on " + ", ".join(x for x in d if x in unstartable) + ", which cannot start"
                changed = True
    return deps, unstartable


def _miss_cause(out: dict) -> str:
    """Why an item's attempt was not accepted, in rung_policy's terms: ``interrupted`` (the run stopped),
    ``needs_permission`` (the sandbox refused something the item needs: an approver decides, not a stronger
    model), ``agent_unavailable`` (the session never ran or its stream broke) or ``cap_reached`` (its share of the
    ceiling ran out) do not move the ladder; ``not_accepted`` (a rejected review, an item its agent did not
    finish, a broken smoke check, the turn limit) is the agent's, and the next attempt runs a rung up."""
    status = str(_mapping(out).get("status") or "")
    if status == "interrupted":
        return "interrupted"
    if _list(_mapping(_mapping(out).get("structured")).get("permissions")):
        return "needs_permission"
    if not _mapping(out) or status in ("sdk_error", "bad_worktree", "unsupported_context_paths", "no_result"):
        return "agent_unavailable"
    if status == "error_max_budget_usd":
        return "cap_reached"
    return "not_accepted"


def _waits_on(it: dict, s: dict, items, accepted_ids: set) -> list[str]:
    """The items of the round, not accepted yet, that an unfinished item's agent names as what it is waiting for."""
    text = " ".join([str(b) for b in _list(s.get("blockers"))] + [str(s.get("summary") or "")])
    return sorted(x for x in items if x and x != it.get("id") and x not in accepted_ids
                  and re.search(rf"(?<![\w-]){re.escape(str(x))}(?![\w-])", text))


def _item_flow(it: dict, ctx: dict) -> tuple[dict, dict]:
    """The dynamic-fork entry starting one item's sub-workflow, and its input."""
    ref = f"item_{_ref(it['id'])}_r{ctx['round']}"
    return ({"name": "swarm_item", "taskReferenceName": ref, "type": "SUB_WORKFLOW", "optional": True,
             "subWorkflowParam": {"name": "swarm_item"}},
            {**ctx, "item": it})


_CTX_KEYS = ("round", "runId", "workdir", "branch", "description", "repoPath", "ceilingUsd", "specBrief", "skills",
             "modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256", "modelsConfig", "modelOverrides")


@worker_task(task_definition_name="swarm_dispatch")
def swarm_dispatch(task):
    """Start a round: record its item graph, mark which items wait on which, and start every item with
    nothing to wait for as its own ``swarm_item`` sub-workflow. Each item, once reviewed, starts the
    items that were waiting on it (``swarm_item_release``): dependency chains of any depth run as nested
    sub-workflows, every item starting the moment its last dependency is accepted."""
    i = task.input_data or {}
    try:
        out, line = _dispatch(i, [_mapping(x) for x in _list(i.get("items"))], int(i.get("round") or 0), protect=True)
        return ok(task, out, [f"[swarm_dispatch] {line}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_dispatch", e)


@worker_task(task_definition_name="swarm_design_dispatch")
def swarm_design_dispatch(task):
    """The design team, right after the lead's plan: every design item starts at once as its own swarm_item
    (an architect designing one part, then its review), beside the check authors. The build items wait for
    round 1, where each starts the moment its part's design is accepted (its `dependsOn` is met on the board).
    The lead's files are not snapshotted here: the check authors are writing them at the same time."""
    i = task.input_data or {}
    try:
        from swarm import team as team_mod
        kick = _mapping(_mapping(i.get("kickoff")).get("structured"))
        from swarm import spec as spec_mod
        mode = spec_mod.spec_mode(i.get("specs"))
        kind = mode if mode in ("tla", "openspec", "none") else str(_mapping(kick.get("spec")).get("kind") or "none")
        design = team_mod.design_items(kick, team_mod.architects_mode(i.get("architects")), spec_kind=kind)
        out, line = _dispatch(i, design, 0, protect=False)
        return ok(task, {**out, "designers": [x.get("id") for x in design]},
                  [f"[swarm_design_dispatch] {len(design)} architect(s) designing together: "
                   f"{', '.join(str(x.get('id')) for x in design) or 'none'} ({line})"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_design_dispatch", e)


def _dispatch(i: dict, items: list[dict], rnd: int, *, protect: bool) -> tuple[dict, str]:
    """Record a round's item graph on the board and the dynamic-fork entries of the items that start now."""
    from common import team_board
    items = [{**x, "id": str(x["id"]).strip()} for x in items if str(x.get("id") or "").strip()]
    from swarm import team as team_mod
    # `maxWidth` is how many items run AT ONCE, never how many the round has: the rest wait for a free slot and
    # start as items finish (swarm_item_release). Cutting the list instead silently dropped 24 of a plan's 35 items,
    # the foundation that builds `check.sh` among them, and round 1 ended with 1 of 31 checks green (run 09a229e8).
    width = max(int(i.get("maxWidth") or 10), 1)
    run = str(i.get("runId") or "")
    board_items = team_board.fold_board(run)["items"] if run else {}
    # In a compiled language nothing builds before the module file and the shared core types exist: the plan's core
    # item goes first and every other build item of the round waits for it (run 8b40c25c: 9 of 42 items started
    # with no go.mod or internal/core, could not compile, and were reported unfinished).
    cores = [x["id"] for x in items if x.get("core") is True and not team_mod.is_design(x)]
    if cores:
        items = [x if x["id"] in cores or team_mod.is_design(x)
                 else {**x, "dependsOn": list(dict.fromkeys([str(d) for d in _list(x.get("dependsOn"))] + cores))}
                 for x in items]
    deps, unstartable = _graph(items, board_items)
    items = _start_order(items, deps, team_mod)
    ctx = {k: i.get(k) for k in _CTX_KEYS}
    ctx["round"] = rnd
    # The lead's files as the round starts: what an item changes in them is put back when the round's
    # work ends (swarm_round), unless the lead handed that file to the item in its `owns`.
    work = str(i.get("workdir") or "")
    protected = team_mod.snapshot(work, rnd) if protect and work and os.path.isdir(work) else {}
    if run:
        from common import blackboard as bb
        import time as _time
        bb.post_fact(run, "harness", ROUND_KIND, {"round": rnd, "items": items, "deps": deps, "width": width,
                                                   "unstartable": unstartable, "startedAt": _time.time()})
        for it in items:
            if it["id"] not in board_items:
                team_board.post_item(run, "architect", {"id": it["id"], "title": it.get("title") or it["id"],
                                                        "description": it.get("brief") or "",
                                                        "kind": it.get("kind") or "feature",
                                                        "dependsOn": deps[it["id"]]})
            if it["id"] in unstartable:
                team_board.set_state(run, "harness", it["id"], team_board.BLOCKED, attempt=rnd,
                                     reason=f"cannot start in round {rnd}: it {unstartable[it['id']]}")
            elif deps[it["id"]]:
                team_board.set_state(run, "harness", it["id"], team_board.BACKLOG, attempt=rnd,
                                     reason="waits for " + ", ".join(deps[it["id"]]))
    ready = [it for it in items if not deps[it["id"]] and it["id"] not in unstartable]
    roots, queued = ready[:width], ready[width:]
    if run:
        for it in queued:
            team_board.set_state(run, "harness", it["id"], team_board.BACKLOG, attempt=rnd,
                                 reason=f"waits for a free slot ({width} items run at once)")
    tasks, inputs = [], {}
    for it in roots:
        if run:
            from common import blackboard as bb
            bb.post_fact(run, "harness", CLAIM_KIND, {"round": rnd, "item": it["id"]})
        t, inp = _item_flow(it, ctx)
        tasks.append(t)
        inputs[t["taskReferenceName"]] = inp
    return ({"dynamicTasks": tasks, "dynamicTasksInput": inputs, "count": len(tasks),
             "waiting": {k: v for k, v in deps.items() if v and k not in unstartable},
             "unstartable": unstartable},
            f"round {rnd}: starting {', '.join(t['id'] for t in roots) or 'nothing'}; "
            f"{sum(team_mod.is_design(x) for x in items)} design item(s); {len(protected)} lead file(s) kept; "
            f"{sum(1 for k, v in deps.items() if v and k not in unstartable)} wait on dependencies; "
            f"{len(unstartable)} cannot start")


def _round_facts(run: str, rnd: int, kind: str) -> list[dict]:
    from common import blackboard as bb
    # Round 0 (the design team) is a round like any other: `round or -1` once read it as -1, and the integrating
    # lead got none of the team's work (run 18fd6f2e).
    def round_of(d: dict) -> int:
        try:
            return int(d.get("round"))
        except (TypeError, ValueError):
            return -1
    return [_mapping(f.get("data")) for f in bb.read_facts(run, kind=kind) if round_of(_mapping(f.get("data"))) == int(rnd)]


@worker_task(task_definition_name="swarm_item_prep")
def swarm_item_prep(task):
    """Everything one item's two sessions need: its implementer on the rung the architect chose, its
    reviewer on the review ladder's rung that history says gets this kind of item reviewed, and each
    session's share of what is left of the ceiling."""
    i = task.input_data or {}
    try:
        from common import budget_gate, rung_policy, team_board
        from common import blackboard as bb
        from swarm import team as team_mod
        it = _mapping(i.get("item"))
        run, rnd = str(i.get("runId") or ""), int(i.get("round") or 0)
        resolution = i.get("modelResolution")
        design = team_mod.is_design(it)
        # A design item runs on the designer's rung (team.DESIGNER_ROLE/RUNG), not the cheapest code rung.
        role = team_mod.DESIGNER_ROLE if design else "code"
        top = _ladder_top(resolution, role)
        rung = min(max(int(it.get("rung") or 0), 0), top)
        rung_reason = "chosen by the architect"
        if not design:
            # Hill climbing over the code ladder (common/rung_policy): an item starts at least on the cheapest rung
            # that has got this kind of item done here. The architect may start it higher, never lower.
            floor, why = rung_policy.start_rung(rung_policy.repo_key(str(i.get("repoPath") or "")), role="code",
                                                kind=str(it.get("kind") or "item"), top=top)
            if floor > rung:
                rung, rung_reason = floor, f"raised from the architect's rung {rung}: {why}"
            elif int(it.get("attempt") or 1) > 1:
                rung_reason = f"attempt {it.get('attempt')}: one rung up from the attempt its review did not accept"
            else:
                rung_reason = f"chosen by the architect ({why})"
        review_rung, review_reason = rung_policy.start_rung(
            rung_policy.repo_key(str(i.get("repoPath") or "")), role="review", kind=str(it.get("kind") or "item"),
            top=_ladder_top(resolution, "review"))
        # Every session gets its share of what is left of the ceiling: the items of the round not yet
        # started, each with an implementer and a reviewer (a round with nothing between its sessions
        # and the ceiling once spent $23.67, run be8fc00e).
        cap = None
        ceiling = float(i.get("ceilingUsd") or 0)
        if ceiling > 0 and run:
            spent = budget_gate.read_spend(run)["usd"]
            graph = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1]
            reported = {r.get("item") for r in _round_facts(run, rnd, REPORT_KIND)}
            left = [x for x in _list(graph.get("items")) if _mapping(x).get("id") not in reported] or [it]
            cap = round(max((ceiling - spent) / (2 * max(len(left), 1)), 0.5), 2)
        if run:
            team_board.set_state(run, "harness", it["id"], team_board.IN_PROGRESS, attempt=rnd,
                                 reason=f"round {rnd}, rung {rung}")
            # The same record the old dispatch wrote, so the board shows the item's rung and why.
            bb.post_fact(run, "harness", "dispatch", {"item": it["id"], "round": rnd, "rung": rung,
                                                       "kind": it.get("kind") or "item",
                                                       "rungReason": rung_reason, "attempt": int(it.get("attempt") or 1),
                                                       "reviewRung": review_rung, "reviewRungReason": review_reason,
                                                       "capUsd": cap})
        from common import permissions
        granted = permissions.grants(run) if run else {"writableRoots": [], "allowedDomains": [], "text": "(none)"}
        item_text = json.dumps(it, indent=1)
        graph = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1] if run else {}
        guide, review_focus = _item_guides(it, [_mapping(x) for x in _list(graph.get("items"))] or [it], team_mod)
        return ok(task, {
            "modelRole": role, "writeRoots": team_mod.owns(it) if design else [],
            # A designer has its own prompt: it designs one part and builds nothing, and the run page shows it as
            # an architect of the design team.
            "templateKey": "swarm_design" if design else "swarm_work",
            "writableRoots": granted["writableRoots"], "allowedDomains": _domains(granted["allowedDomains"], str(i.get("workdir") or "")),
            "itemId": it["id"], "rung": rung, "maxRung": top, "rungReason": rung_reason, "reviewRung": review_rung, "reviewRungReason": review_reason,
            "capUsd": cap, "agentAuthor": f"agent:{it['id']}", "reviewAuthor": f"review:{it['id']}",
            "workSchema": DESIGN_SCHEMA if design else WORK_SCHEMA, "reviewSchema": REVIEW_SCHEMA,
            "allowedTools": SWARM_ALLOWED_TOOLS, "disallowedTools": SWARM_DISALLOWED_TOOLS,
            "promptContext": {"request": i.get("description") or "", "branch": i.get("branch") or "",
                              "round": str(rnd), "item": item_text, "permissions": granted["text"],
                              "spec": i.get("specBrief") or "", "itemGuide": guide,
                              "precedence": team_mod.PRECEDENCE,
                              "team": team_mod.team_roster([_mapping(x) for x in _list(graph.get("items"))], it)
                              if design else "",
                              "specTask": team_mod.spec_task(it) if design else ""},
            "reviewContext": {"request": i.get("description") or "", "branch": i.get("branch") or "",
                              "round": str(rnd), "item": item_text, "spec": i.get("specBrief") or "",
                              "reviewFocus": review_focus, "precedence": team_mod.PRECEDENCE},
        }, [f"[swarm_item_prep] {it['id']}: {'designer (design role)' if design else 'implementer'} rung {rung} ({rung_reason}), "
            f"reviewer rung {review_rung} ({review_reason}), "
            f"cap ${cap}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_item_prep", e)


def _smoke_checks(work: str) -> list[dict]:
    """The run's SMOKE checks: those marked `smoke`, else a check whose id says it starts the program."""
    from swarm import spec as spec_mod
    checks = spec_mod.read_checks(work)
    named = [c for c in checks if re.search(r"(startup|smoke|boot)$", str(c.get("id") or ""))]
    marked = [c for c in checks if c.get("smoke")]
    # At most two, and a boot check first: a smoke run is on every release's path, and a lead that marked six
    # feature checks smoke made each run 24 s (run 4317f3aa).
    return ([c for c in marked if c in named] + [c for c in marked if c not in named] or named)[:MAX_SMOKE_CHECKS]


def _run_smoke(work: str, since: float = 0.0) -> dict:
    """Run the smoke checks on the shared tree, one run at a time: ``{"ok": bool|None, "output": str}`` (None:
    there is no smoke check). A failure is run again once after a short wait, so a teammate's half-written file
    never blames the item being released. A run that started after ``since`` (when this item's change was already
    on disk) answers for it too: releases waiting on the lock share one run instead of queueing one each (44 runs
    back to back held run 4317f3aa's releases up to 6 minutes)."""
    import fcntl
    import time as _time
    from common import architect, check_execution, test_budget
    from common.coding_agent import _tool_cache_env
    checks = _smoke_checks(work)
    if not checks:
        return {"ok": None, "output": ""}
    env = {**check_execution.inherited_environment(_tool_cache_env(work)), "CONDUCTOR_HARNESS_RUN": "1"}
    os.makedirs(os.path.join(work, ".cc-cache"), exist_ok=True)
    last_path = os.path.join(work, ".cc-cache", "smoke-last.json")
    with open(os.path.join(work, ".cc-cache", "smoke.lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(last_path, encoding="utf-8") as fh:
                last = json.load(fh)
            if since and float(last.get("started") or 0) >= since:
                return {"ok": last.get("ok"), "output": str(last.get("output") or ""), "shared": True}
        except (OSError, ValueError):
            pass
        started = _time.time()
        for attempt in (0, 1):
            outputs, ok = [], True
            for c in checks:
                try:
                    r = check_execution.execute(architect.check_argv(str(c.get("command") or "")), cwd=work, env=env,
                                                stall_after_s=SMOKE_STALL_S, expected_s=SMOKE_EXPECTED_S)
                    if r.exit_code != 0:
                        ok = False
                        outputs.append(f"`{c.get('command')}` exit {r.exit_code}:\n" + ((r.stdout or "") + (r.stderr or ""))[-1500:])
                except (ValueError, OSError) as e:
                    ok = False
                    outputs.append(f"`{c.get('command')}`: {e}")
            if ok or attempt:
                with open(last_path, "w", encoding="utf-8") as fh:
                    json.dump({"started": started, "ok": ok, "output": "\n".join(outputs)}, fh)
                return {"ok": ok, "output": "\n".join(outputs)}
            _time.sleep(SMOKE_RETRY_AFTER_S)
    return {"ok": False, "output": ""}


def _item_guides(it: dict, round_items: list[dict], team_mod) -> tuple[str, str]:
    """``(itemGuide, reviewFocus)``: what the item's agent and its reviewer are told about who owns what.
    A designer writes its part's document and nothing of the lead's; a build item reads its own part's
    document, the architecture and the seams, not every other part's."""
    by_id = {str(x.get("id")): x for x in round_items}
    if team_mod.is_design(it):
        mine = ", ".join(f"`{p}`" for p in team_mod.owns(it)) or "(none listed)"
        waiting = [x.get("id") for x in round_items if it.get("id") in (x.get("dependsOn") or [])]
        guide = (f"Your document: {mine}. You may write only that." + (
            f" Build items already waiting for your design: {', '.join(map(str, waiting))}; each must be buildable "
            "from your document alone." if waiting else ""))
        focus = ("THIS IS A DESIGN ITEM of a design team: judge this ONE document, not code and not the other parts. "
                 "Accept it if (1) it agrees with `design/architecture.md`, (2) its proposed items could each be built "
                 f"from it alone, and (3) it changed only the files it owns ({mine}). Do NOT reject it for what is not "
                 "this architect's to settle: a conflict or gap with another part's document (post it with `board "
                 "decide`; the lead settles every seam right after the design), or `.cc/design.md` and `.cc/checks/` "
                 "being absent (the lead writes them after the design). Do not run anything.")
        return guide, focus
    docs = []
    for d in (str(x) for x in it.get("dependsOn") or []):
        dep = by_id.get(d) or {}
        if team_mod.is_design(dep):
            docs += [p for p in team_mod.owns(dep) if p.startswith("design/") and p.endswith(".md")]
    granted = [p for p in team_mod.owns(it) if team_mod.is_protected(p)]
    guide = ""
    if docs:
        guide = ("YOUR PART'S DESIGN: " + ", ".join(f"`{p}`" for p in docs) + ", with `design/architecture.md` and "
                 "the seams in `.cc/design.md`. Other parts' design documents are not yours to read or follow; their "
                 "seams are in `.cc/design.md`.")
    if granted:
        guide += ("\nThe lead architect handed you its file(s) " + ", ".join(f"`{p}`" for p in granted)
                  + " for this item; change them only as the brief says.")
    guide += ("\nThe lead architect's files (`design/architecture.md`, `.cc/design.md`, the spec, `.cc/checks*`) are "
              "not yours" + (" beyond what it handed you" if granted else "")
              + ": the harness puts back any change to them when the round's work ends. When one is wrong, post "
              "`board decide` with the evidence.")
    return guide.strip(), ""


def _team_reports(run: str) -> list[dict]:
    """Each part's last design report, with its proposals' ids prefixed by the part (two architects naming an
    item `parser` must not collide) and its own dependencies renamed to match."""
    latest: dict = {}
    for r in (_round_facts(run, 0, REPORT_KIND) if run else []):
        latest[r.get("item")] = {**r, "attempts": (latest.get(r.get("item")) or {}).get("attempts", 0) + 1}
    out = []
    for r in latest.values():
        part = str(r.get("item") or "").removeprefix("design-")
        def norm(x: str) -> str:
            x = _slug(x) or "x"
            return x if x.startswith(part) else f"{part}-{x}"
        items = [_mapping(x) for x in _list(r.get("proposedItems")) if str(_mapping(x).get("id") or "").strip()]
        names = {str(x["id"]).strip(): norm(str(x["id"])) for x in items}
        r["proposedItems"] = [{**x, "id": names[str(x["id"]).strip()], "kind": x.get("kind") or "feature",
                               "rung": int(x.get("rung") or 0), "title": x.get("title") or x["id"],
                               "brief": x.get("brief") or "",
                               "dependsOn": [names.get(str(d).strip(), str(d).strip()) for d in _list(x.get("dependsOn"))]}
                              for x in items]
        r["proposedChecks"] = [{**_mapping(c), "id": norm(str(_mapping(c).get("id"))),
                                "covers": _list(_mapping(c).get("covers"))}
                               for c in _list(r.get("proposedChecks")) if _mapping(c).get("id") and _mapping(c).get("command")]
        out.append(r)
    return out


def assemble_plan(reports: list[dict], integ: dict) -> dict:
    """The run's plan from the design team's proposals and the lead's changes to them: the lead drops, adds and
    replaces (same id); it never re-types what it keeps."""
    drop_items = {str(x).strip() for x in _list(integ.get("dropItems"))}
    drop_checks = {str(x).strip() for x in _list(integ.get("dropChecks"))}
    items: dict = {}
    checks: dict = {}
    for r in reports:
        for x in r.get("proposedItems") or []:
            if x["id"] not in drop_items:
                items[x["id"]] = x
        for c in r.get("proposedChecks") or []:
            if c["id"] not in drop_checks:
                checks[c["id"]] = c
    for x in (_mapping(v) for v in _list(integ.get("items"))):
        if str(x.get("id") or "").strip():
            items[str(x["id"]).strip()] = {**items.get(str(x["id"]).strip(), {}), **x, "id": str(x["id"]).strip()}
    for c in (_mapping(v) for v in _list(integ.get("checks"))):
        if str(c.get("id") or "").strip():
            checks[str(c["id"]).strip()] = {**checks.get(str(c["id"]).strip(), {}), **c, "id": str(c["id"]).strip()}
    for cid in _list(integ.get("smokeChecks")):
        if str(cid).strip() in checks:
            checks[str(cid).strip()] = {**checks[str(cid).strip()], "smoke": True}
    files: set = set()
    kept_checks = []
    for c in checks.values():  # two checks can never share a script
        if c.get("file") in files:
            continue
        files.add(c.get("file"))
        kept_checks.append(c)
    known = set(items)
    for x in items.values():  # a dependency on something dropped or never proposed is not a dependency
        x["dependsOn"] = [d for d in _list(x.get("dependsOn")) if d in known and d != x["id"]]
    return {"items": list(items.values()), "checks": kept_checks, "retireChecks": _list(integ.get("retireChecks")),
            "spec": _mapping(integ.get("spec")), "design": _mapping(integ.get("design")),
            "verification": _mapping(integ.get("verification")), "decisions": str(integ.get("decisions") or "")}


@worker_task(task_definition_name="swarm_gap_dispatch")
def swarm_gap_dispatch(task):
    """The lead's review found pieces missing: one gap-filler architect each, all at once, like the design team
    (none, and nothing to wait for, when the review found no gap)."""
    i = task.input_data or {}
    try:
        from swarm import team as team_mod
        review = _mapping(_mapping(i.get("review")).get("structured"))
        from swarm import spec as spec_mod
        kind = str(_mapping(review.get("spec")).get("kind") or spec_mod.spec_mode(i.get("specs")) or "none")
        gaps = team_mod.design_items({"team": True, "parts": [{**_mapping(g), "id": "gap-" + str(_mapping(g).get("id") or "")}
                                                              for g in _list(review.get("gaps"))]},
                                     "auto", spec_kind=kind)
        out, line = _dispatch(i, gaps, 0, protect=False)
        return ok(task, {**out, "gaps": [g["id"] for g in gaps]},
                  [f"[swarm_gap_dispatch] {len(gaps)} gap-filler(s): {', '.join(g['id'] for g in gaps) or 'none'}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_gap_dispatch", e)


@worker_task(task_definition_name="swarm_assemble")
def swarm_assemble(task):
    """The plan of a design team's run: the architects' proposals with the integrating lead's changes."""
    i = task.input_data or {}
    try:
        reports = _team_reports(str(i.get("runId") or ""))
        integ = _mapping(_mapping(i.get("integrate")).get("structured"))
        plan = assemble_plan(reports, integ)
        proposed = sum(len(r.get("proposedItems") or []) for r in reports)
        return ok(task, {"structured": plan},
                  [f"[swarm_assemble] {len(plan['items'])} item(s) and {len(plan['checks'])} check(s) from {len(reports)} "
                   f"part design(s) ({proposed} item(s) proposed; the lead dropped {len(_list(integ.get('dropItems')))}, "
                   f"added or replaced {len(_list(integ.get('items')))})"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_assemble", e)


@worker_task(task_definition_name="swarm_design_fold")
def swarm_design_fold(task):
    """After the design team: what each part's architect designed, provides, requires and proposes, with its
    review's verdict and the board's open questions and conflicts, as the text the integrating lead reads."""
    i = task.input_data or {}
    try:
        from common import team_board
        run = str(i.get("runId") or "")
        reports = _team_reports(run)
        if not reports:
            # The lead's kickoff chose no team (a hello-world): the lead plans alone. Reviewing zero designs called the
            # whole program a gap and an architect designed it (run b7d640bd).
            return ok(task, {"text": "", "parts": 0, "specTask": "", "team": "solo"}, ["[swarm_design_fold] no design team"])
        kind = str(_mapping(_mapping(_mapping(i.get("kickoff")).get("structured")).get("spec")).get("kind") or "none")
        spec_task = {"openspec": "THE SPEC (OpenSpec): each architect wrote its part's capability in "
                                 "`.cc/openspec/specs/<part>/spec.md`. Fix only conflicts between them; every scenario "
                                 "must stay proven by a check (`covers`). Return spec.kind \"openspec\".",
                     "tla": "THE SPEC (TLA+): written by the design-spec architect against the seams and proven by "
                            "TLC when it finished. Do not edit it. Return spec.kind \"tla\".",
                     "none": "THE SPEC: none for this run; return spec.kind \"none\"."}.get(kind, "")
        board = team_board.fold_board(run)
        parts = []
        for r in reports:
            parts.append(
                f"### {r.get('item')} ({'accepted' if r.get('accepted') else 'STILL NOT ACCEPTED after its reworks: you must settle it'} "
                f"by its review after {r['attempts']} attempt(s): {r.get('reviewNotes')})\nDocument(s): {', '.join(_list(r.get('files'))) or 'none'}\n"
                f"Summary: {r.get('summary')}\nProvides: {r.get('provides') or '-'}\nRequires: {r.get('requires') or '-'}\n"
                f"Blockers (seams it could not satisfy): {json.dumps(_list(r.get('blockers'))) if r.get('blockers') else 'none'}\n"
                f"Proposed items: {json.dumps(_list(r.get('proposedItems')))}\n"
                f"Proposed checks: {json.dumps(_list(r.get('proposedChecks')))}")
        lessons = [x.get("text") for x in board.get("lessons") or []][-40:]
        open_q = [x.get("text") for x in board.get("directives") or []][-40:]
        text = ("THE ARCHITECTS have designed the subsystems you split the system into, each on its own. Their work:\n\n"
                + "\n\n".join(parts)
                + ("\n\nWhat they agreed on the board:\n" + "\n".join(f"- {x}" for x in lessons) if lessons else "")
                + ("\n\nQuestions and conflicts still open for you:\n" + "\n".join(f"- {x}" for x in open_q) if open_q else ""))
        return ok(task, {"text": text, "parts": len(reports), "specTask": spec_task, "specKind": kind, "team": "team",
                         "accepted": sum(1 for r in reports if r.get("accepted"))},
                  [f"[swarm_design_fold] {len(reports)} part design(s), "
                   f"{sum(1 for r in reports if r.get('accepted'))} accepted; {len(lessons)} lesson(s), {len(open_q)} open"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_design_fold", e)


@worker_task(task_definition_name="swarm_item_reviewing")
def swarm_item_reviewing(task):
    """Mark an item "in review" on the board for the time its reviewer is at work."""
    i = task.input_data or {}
    try:
        from common import team_board
        run, item = str(i.get("runId") or ""), str(i.get("itemId") or "")
        summary = str(_mapping(_mapping(i.get("work")).get("structured")).get("summary") or "")
        if run and item:
            team_board.set_state(run, "harness", item, team_board.REVIEW, attempt=int(i.get("round") or 0),
                                 reason=("its reviewer is checking it" + (f"; the agent reported: {summary}" if summary else ""))[:400])
        return ok(task, {"item": item}, [f"[swarm_item_reviewing] {item} is in review"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_item_reviewing", e)


# Releases are mostly waiting (the board, the smoke lock): one per item of a wide round at once, not four.
@worker_task(task_definition_name="swarm_item_release", thread_count=16)
def swarm_item_release(task):
    """Record one item's outcome and start what was waiting on it. Accepted by its review: every item of
    the round whose dependencies are now all accepted is claimed (once, under the clone's lock, so two
    dependencies finishing together cannot both start it) and started as its own sub-workflow. Not
    accepted: everything waiting on it, at any depth, is held for the round's review."""
    import time as _time
    release_started = _time.time()  # this item's change is on disk from here: a smoke run started later covers it
    i = task.input_data or {}
    try:
        from common import team_board
        from common import blackboard as bb
        it = _mapping(i.get("item"))
        run, rnd, work = str(i.get("runId") or ""), int(i.get("round") or 0), str(i.get("workdir") or "")
        out, rev = _mapping(i.get("work")), _mapping(i.get("review"))
        s, r = _mapping(out.get("structured")), _mapping(rev.get("structured"))
        # A review that never answered does not hold a chain of work back; the round's review sees it.
        accepted = bool(r.get("accepted")) if r else True
        review_said = (r.get("accepted") is True) if r else None  # the reviewer's own verdict, for its ladder
        from swarm import team as team_mod
        strays = team_mod.outside_owns(it, _list(s.get("files")), work) if team_mod.is_design(it) else []
        if strays:
            accepted = False
            r = {**r, "notes": (f"not accepted by the harness: a designer writes only what it owns "
                                f"({', '.join(team_mod.owns(it)) or 'nothing listed'}), and it reported "
                                f"{', '.join(strays[:8])}. " + str(r.get("notes") or "")).strip()}
        # The spec is checked by the step that wrote it: an architect's own capability (or the TLA+ model) must be
        # sound and covered by its own checks before the plan is made; a defect goes back to that architect.
        spec_problems = team_mod.lint_part(work, it, s) if team_mod.is_design(it) and work else []
        if spec_problems:
            accepted = False
            r = {**r, "notes": ("not accepted by the harness: your spec is not ready:\n"
                                + "\n".join(f"- {p}" for p in spec_problems[:30]) + "\n" + str(r.get("notes") or "")).strip()}
        # An item its own agent says is not finished is not done, whatever its review says (run 97427f07: report-render
        # was accepted with nothing rendered and its imports missing). It goes back to an agent like a rejected one.
        if accepted and not team_mod.is_design(it) and s and s.get("done") is False:
            accepted = False
            r = {**r, "notes": ("not accepted by the harness: its agent reported it is not finished"
                                + (": " + "; ".join(str(b) for b in _list(s.get("blockers"))[:5]) if _list(s.get("blockers")) else "")
                                + "\n" + str(r.get("notes") or "")).strip()}
        # The smoke test as items land: the program must still start once this item is in (run b74d3da7's server
        # died at boot from minute ~12 and nobody knew until the round was over, 44 minutes later).
        smoke, spawn_fixer = {"ok": None}, False
        if accepted and run and work and rnd >= 1 and not team_mod.is_design(it):
            from common import blackboard as bb
            before = [_mapping(f.get("data")) for f in bb.read_facts(run, kind=SMOKE_KIND)]
            worked_before = bool(before) and before[-1].get("ok") is True
            graph_now = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1]
            reported_now = {x.get("item") for x in _round_facts(run, rnd, REPORT_KIND)} | {it["id"]}
            last_in = not [x for x in _list(graph_now.get("items")) if _mapping(x).get("id") not in reported_now]
            # While the program has not started yet, a smoke run proves nothing about this item and blames no one:
            # it runs when the round's last item lands (and a fixer starts if it fails). Once it has started, every
            # release runs it, so the item that breaks it is the one sent back.
            if worked_before or last_in or it.get("kind") == FIXER_KIND:
                smoke = _run_smoke(work, since=release_started)
            if smoke["ok"] is not None and not smoke.get("shared"):
                bb.post_fact(run, "harness", SMOKE_KIND, {"round": rnd, "item": it["id"], "ok": smoke["ok"],
                                                          "output": smoke["output"][-2000:]})
            if smoke["ok"] is False:
                if worked_before or it.get("kind") == FIXER_KIND:
                    accepted = False
                    r = {**r, "notes": ("not accepted by the harness: the program no longer starts with your change "
                                        "in (it did before)" if worked_before else "not accepted by the harness: the "
                                        "smoke checks still fail") + ":\n" + smoke["output"][-1500:] + "\n"
                                       + str(r.get("notes") or "")}
                    team_board.post_lesson(run, "harness", f"The program stopped starting when {it['id']} landed: "
                                           + smoke["output"][-300:])
                spawn_fixer = not worked_before
        cause = "" if accepted else _miss_cause(out)
        waits_on: list[str] = []
        if cause == "not_accepted" and run and rnd >= 1 and not team_mod.is_design(it) and s.get("done") is False:
            g = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1]
            acc = {x.get("item") for x in _round_facts(run, rnd, REPORT_KIND) if x.get("accepted")}
            waits = _round_facts(run, rnd, WAIT_KIND)
            waited = [w for w in waits if w.get("item") == it["id"]]
            # Never wait on an item that is itself waiting: two items naming each other would never restart (run
            # 8b40c25c: admin-info and admin-persist each waited on the other, both really on server-core).
            woke = {(w.get("item"), w.get("attempt")) for w in _round_facts(run, rnd, WAKE_KIND)}
            asleep = {w.get("item") for w in waits if (w.get("item"), w.get("attempt")) not in woke}
            if len(waited) < MAX_ITEM_WAITS:
                waits_on = [x for x in _waits_on(it, s, [_mapping(x).get("id") for x in _list(g.get("items"))], acc)
                            if x not in asleep]
            if waits_on:
                cause = "waiting"  # not the agent's: no climb, and the ladder does not count it
        report = {"round": rnd, "item": it["id"], "kind": it.get("kind") or "item", "rung": int(i.get("rung") or 0),
                  "attempt": int(it.get("attempt") or 1), "cause": cause, "reviewAccepted": review_said,
                  "reviewRung": int(i.get("reviewRung") or 0), "status": out.get("status"),
                  "summary": s.get("summary") or out.get("error") or "no report", "done": s.get("done") is True,
                  "files": _list(s.get("files")), "blockers": _list(s.get("blockers")), "lessons": _list(s.get("lessons")),
                  "accepted": accepted, "reviewNotes": r.get("notes") or ("" if r else "the review did not answer"),
                  "reviewFixed": _list(r.get("fixed")),
                  **({"provides": s.get("provides") or "", "requires": s.get("requires") or "",
                      "proposedItems": _list(s.get("proposedItems")), "proposedChecks": _list(s.get("proposedChecks"))}
                     if team_mod.is_design(it) else {})}
        ctx = {k: i.get(k) for k in _CTX_KEYS}
        tasks, inputs, held = [], {}, []
        if run:
            bb.post_fact(run, "harness", REPORT_KIND, report)
            # Accepted by its review means done for now (the round's review can still hand it back);
            # "review" is only the time its reviewer is at work. Accepted items sat in "In review" until
            # the round ended and the board read as if nobody was reviewing them (run 746bbce4).
            team_board.set_state(run, "review", it["id"], team_board.DONE if accepted else team_board.REOPENED,
                                 reason=(f"accepted by its review: {report['reviewNotes']}" if accepted
                                         else f"not accepted by its review: {report['reviewNotes']}")[:400])
            with git._repo_lock(work):
                graph = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1]
                deps = {k: _list(v) for k, v in _mapping(graph.get("deps")).items()}
                unstartable = _mapping(graph.get("unstartable"))
                claimed = {c.get("item") for c in _round_facts(run, rnd, CLAIM_KIND)}
                accepted_ids = {x.get("item") for x in _round_facts(run, rnd, REPORT_KIND) if x.get("accepted")}
                items = {(_mapping(x)).get("id"): _mapping(x) for x in _list(graph.get("items"))}
                attempts = [x for x in _round_facts(run, rnd, REPORT_KIND) if x.get("item") == it["id"]]
                if not accepted and team_mod.is_design(it) and rnd == 0 and len(attempts) <= team_mod.MAX_DESIGN_REWORKS:
                    # The design team's rework loop, like the build items': a design its review rejects goes back to
                    # its own architect now, with the review's notes, while its teammates' documents are there to
                    # align with; the second rework runs a rung higher. Nobody sits on a reopened design.
                    n = len(attempts)
                    again = {**it, "rung": int(it.get("rung") or 0) + 1, "attempt": n + 1,
                             "brief": str(it.get("brief") or "").split("\n\nYOUR REVIEW DID NOT ACCEPT")[0]
                             + f"\n\nYOUR REVIEW DID NOT ACCEPT YOUR DESIGN (attempt {n} of "
                             f"{team_mod.MAX_DESIGN_REWORKS + 1}). Its notes:\n{report['reviewNotes']}\n\nRework your document "
                             "now: read your teammates' current documents and the board again, make what your part "
                             "REQUIRES match what theirs PROVIDE (or raise the gap with `board decide`), and fix every "
                             "point the review made."}
                    team_board.set_state(run, "harness", it["id"], team_board.IN_PROGRESS, attempt=n + 1,
                                         reason=f"back to its architect (rework {n} of {team_mod.MAX_DESIGN_REWORKS}): "
                                                f"{report['reviewNotes']}"[:400])
                    t, inp = _item_flow(again, ctx)
                    t = {**t, "taskReferenceName": f"{t['taskReferenceName']}_rework{n}"}
                    tasks.append(t)
                    inputs[t["taskReferenceName"]] = inp
                elif waits_on and not set(waits_on) <= accepted_ids:
                    # Waiting, not stuck: it restarts on the same rung when what it waits for is accepted (below, in
                    # the release of the last of them). Its dependents keep waiting for it.
                    bb.post_fact(run, "harness", WAIT_KIND, {"round": rnd, "item": it["id"], "on": waits_on,
                                                             "rung": int(i.get("rung") or 0), "attempt": len(attempts) + 1,
                                                             "item_entry": it})
                    team_board.set_state(run, "harness", it["id"], team_board.BLOCKED, attempt=len(attempts),
                                         reason=(f"waiting for {', '.join(waits_on)} to land; it restarts on rung "
                                                 f"{int(i.get('rung') or 0)} when they do")[:400])
                elif (not accepted and not team_mod.is_design(it) and rnd >= 1
                      and cause not in ("interrupted", "needs_permission")
                      and len([a for a in attempts if a.get("cause") != "waiting"]) <= MAX_ITEM_REWORKS):
                    # Climbing when stuck: the item goes back to an agent now, one rung up when the miss was the
                    # agent's (the same rung when the session never ran or its cap ran out), with the reasons, and
                    # its dependents keep waiting for it. The round review sees it only if every attempt misses.
                    from common import rung_policy
                    n = len(attempts)
                    rung_now = int(i.get("rung") or 0)
                    climb = rung_policy.agent_fixable(cause) and cause != "waiting"
                    nxt = min(rung_now + (1 if climb else 0), max(int(i.get("maxRung") or 0), rung_now))
                    why = (report["reviewNotes"] if cause == "not_accepted" else
                           f"what it waited for ({', '.join(waits_on)}) has landed" if cause == "waiting" else
                           f"the session did not finish ({cause}: {out.get('error') or out.get('status') or 'no output'})")
                    again = {**it, "rung": nxt, "attempt": n + 1,
                             "brief": str(it.get("brief") or "").split("\n\nYOUR LAST ATTEMPT WAS NOT ACCEPTED")[0]
                             + f"\n\nYOUR LAST ATTEMPT WAS NOT ACCEPTED (attempt {n} of {MAX_ITEM_REWORKS + 1}; this one "
                             f"runs on rung {nxt}). Why:\n{why[-2500:]}\n\nThe earlier attempt's work is on the shared "
                             "tree: start from it, fix every point above, and finish the item."}
                    team_board.set_state(run, "harness", it["id"], team_board.IN_PROGRESS, attempt=n + 1,
                                         reason=(f"attempt {n + 1} on rung {nxt}"
                                                 + (f" (up from {rung_now})" if nxt > rung_now else "")
                                                 + f": {why}")[:400])
                    t, inp = _item_flow(again, ctx)
                    t = {**t, "taskReferenceName": f"{t['taskReferenceName']}_rework{n}"}
                    tasks.append(t)
                    inputs[t["taskReferenceName"]] = inp
                elif accepted:
                    pass  # what waited on it starts below, in the free slots
                else:
                    blocked, frontier = set(), {it["id"]}
                    while frontier:
                        nxt = {iid for iid, d in deps.items() if set(d) & frontier and iid not in blocked and iid not in claimed}
                        blocked |= nxt
                        frontier = nxt
                    for iid in sorted(blocked):
                        team_board.set_state(run, "harness", iid, team_board.BLOCKED, attempt=rnd,
                                             reason=f"held: it waits for {it['id']}, which its review did not accept")
                        held.append(iid)
                # This item's slot is free: start what is ready (its dependents first, then items still waiting for a
                # slot), up to the round's width of items running at once.
                if run:  # the design round too: a width of 2 must not leave four architects never started
                    reported_ids = {x.get("item") for x in _round_facts(run, rnd, REPORT_KIND)}
                    running = len([c for c in claimed if c not in reported_ids]) + len(tasks)
                    width = int(graph.get("width") or len(items) or 1)
                    # Items that were waiting for others restart once everything they waited for is accepted.
                    woken = {(w.get("item"), w.get("attempt")) for w in _round_facts(run, rnd, WAKE_KIND)}
                    for w in _round_facts(run, rnd, WAIT_KIND):
                        key = (w.get("item"), w.get("attempt"))
                        if key in woken or not set(_list(w.get("on"))) <= accepted_ids or running >= width:
                            continue
                        prior = _mapping(w.get("item_entry"))
                        landed = ", ".join(_list(w.get("on")))
                        again = {**prior, "rung": int(w.get("rung") or 0), "attempt": int(w.get("attempt") or 2),
                                 "brief": str(prior.get("brief") or "").split("\n\nWHAT YOU WAITED FOR HAS LANDED")[0]
                                 .split("\n\nYOUR LAST ATTEMPT WAS NOT ACCEPTED")[0]
                                 + f"\n\nWHAT YOU WAITED FOR HAS LANDED ({landed}, accepted). Your earlier work is on the "
                                 "shared tree: build against what is there now and finish the item."}
                        bb.post_fact(run, "harness", WAKE_KIND, {"round": rnd, "item": w.get("item"), "attempt": w.get("attempt")})
                        team_board.set_state(run, "harness", str(w.get("item")), team_board.IN_PROGRESS,
                                             attempt=int(w.get("attempt") or 2),
                                             reason=f"{landed} landed: attempt {w.get('attempt')} on rung {again['rung']}"[:400])
                        t, inp = _item_flow(again, ctx)
                        t = {**t, "taskReferenceName": f"{t['taskReferenceName']}_wake{w.get('attempt')}"}
                        tasks.append(t)
                        inputs[t["taskReferenceName"]] = inp
                        woken.add(key)
                        running += 1
                    ready = [x for x in items if x not in claimed and x not in unstartable and x not in held
                             and all(d in accepted_ids for d in deps.get(x, []))]
                    for iid in sorted(ready, key=lambda x: it["id"] not in deps.get(x, [])):  # its dependents first
                        if running >= width:
                            break
                        bb.post_fact(run, "harness", CLAIM_KIND, {"round": rnd, "item": iid})
                        t, inp = _item_flow(items[iid], ctx)
                        tasks.append(t)
                        inputs[t["taskReferenceName"]] = inp
                        running += 1
                if spawn_fixer and not tasks:
                    # Every item of the round is in and the program still does not start, and no single item can be
                    # blamed (it never started, or was already failing): one build agent fixes the integration now,
                    # inside the round, instead of the round review finding it afterwards.
                    reported = {x.get("item") for x in _round_facts(run, rnd, REPORT_KIND)}
                    pending = [iid for iid in items if iid not in reported]
                    fixers = [c for c in _round_facts(run, rnd, CLAIM_KIND) if str(c.get("item") or "").startswith("smoke-fix-")]
                    if not pending and len(fixers) < MAX_FIXERS_PER_ROUND:
                        fid = f"smoke-fix-r{rnd}-{len(fixers) + 1}"
                        cmds = ", ".join(f"`{c.get('command')}`" for c in _smoke_checks(work))
                        fixer = {"id": fid, "title": "Make the program start (the smoke checks)", "kind": FIXER_KIND,
                                 "rung": 1, "dependsOn": [],
                                 "brief": ("Every item of this round has landed and the program does not start: the smoke "
                                           f"checks {cmds} fail on the shared tree:\n{smoke.get('output', '')[-2000:]}\n\n"
                                           "Find the cause wherever it is (it is a fault between items, in any file) and make "
                                           "the smallest change that makes the smoke checks pass; run them once to confirm. "
                                           "Post what you changed and why with `board lesson`.")}
                        team_board.post_item(run, "harness", {"id": fid, "title": fixer["title"], "description": fixer["brief"],
                                                              "kind": FIXER_KIND, "dependsOn": []})
                        bb.post_fact(run, "harness", CLAIM_KIND, {"round": rnd, "item": fid})
                        t, inp = _item_flow(fixer, ctx)
                        tasks.append(t)
                        inputs[t["taskReferenceName"]] = inp
        return ok(task, {"accepted": accepted, "dynamicTasks": tasks, "dynamicTasksInput": inputs, "smoke": smoke.get("ok"),
                         "started": [t["taskReferenceName"] for t in tasks], "held": held},
                  [f"[swarm_item_release] {it['id']}: {'accepted' if accepted else 'not accepted'}; "
                   f"starting {len(tasks)} dependent(s); holding {len(held)}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_item_release", e)


@worker_task(task_definition_name="swarm_formal")
def swarm_formal(task):
    """The gate after planning (docs/FORMAL_SPEC_DESIGN.md): the plan gives the spec and design documents
    this run asks for (the person's choice, or the architect's), and the spec is proven before anyone
    builds from it. A TLA+ spec: TLC checks every invariant in every reachable state of the bounded model,
    every mutant must break its invariant, and the state graph becomes the trace suite the code is
    replayed against. An OpenSpec spec: well formed, and every scenario proven by an acceptance check."""
    i = task.input_data or {}
    try:
        from swarm import spec as spec_mod
        plan = _mapping(_mapping(i.get("plan")).get("structured"))
        out = spec_mod.gate(str(i["workdir"]), plan, spec_mod.spec_mode(i.get("specs")),
                            spec_mod.design_mode(i.get("design")), str(i.get("existingSpec") or ""))
        # The team rules are part of what the plan must give: too many designers, a designer for a part
        # nothing builds on, or one that would own the lead's files goes back to the lead like a bad spec.
        from swarm import team as team_mod
        problems = team_mod.design_problems(plan, team_mod.architects_mode(i.get("architects")))
        problems += team_mod.chain_problems(_list(plan.get("items")))
        problems += team_mod.core_problems(_list(plan.get("items")),
                                           _stacks_mod.needs_core(str(i["workdir"]), _list(plan.get("stacks"))))
        coded = team_mod.non_planning_changes(str(i["workdir"]))
        if coded:
            problems.append("Planning is not coding: you changed files outside `design/` and `.cc/` ("
                            + ", ".join(coded[:20]) + (" ..." if len(coded) > 20 else "")
                            + "). Delete them; whatever they were for is an item that builds it in round 1.")
        if problems:
            out.update(ok=False, summary=("the plan's design items break the team rules"
                                          if out.get("ok") else str(out.get("summary") or "")),
                       feedback="\n".join(x for x in (str(out.get("feedback") or "") if not out.get("ok") else "",
                                                       *problems) if x))
        # What the lead reads in its next attempt (the plan prompt's {{formalFeedback}}). In a team run the spec was
        # written and checked by the architects during design: what the gate finds is the plan's coverage, said so.
        text = str(out.get("feedback") or out.get("summary") or "")
        if not out.get("ok") and str(i.get("team") or "") == "team":
            lines = text.splitlines()
            coverage = [x for x in lines if "no check proves" in x or "which is not a scenario" in x]
            spec = [x for x in lines if x.startswith("- ") and x not in coverage]
            text = "\n".join(
                (["THE PLAN DOES NOT COVER THE SPEC (add or fix checks; do not edit the spec):"] + coverage if coverage else [])
                + (["A SPEC DEFECT reached the plan (the architects' specs were checked during design; one was changed "
                    "since). Record which subsystem's spec stands as a binding decision; do not rewrite their specs:"]
                   + spec if spec else []) + [x for x in lines if not x.startswith("- ") and x not in coverage])
        out["feedback"] = ("" if out.get("ok") else
                           "THE HARNESS REJECTED YOUR LAST PLAN; fix it (your files are still in the tree):\n" + text)
        return ok(task, out,
                  [f"[swarm_formal] spec {out.get('kind') or '?'}: {'ready' if out.get('ok') else 'not ready'}: "
                   f"{out.get('summary')}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_formal", e)


AUTHOR_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}},
                   "failsNow": {"type": "boolean"}},
    "required": ["summary", "files", "failsNow"],
}


@worker_task(task_definition_name="swarm_authoring")
def swarm_authoring(task):
    """After the architect decided: write ``.cc/checks.json`` from its list, and start one author session
    per check, plus one for the conformance adapter when there is a formal spec, all at once on the code
    role's cheapest rung. Authoring is execution, not design: it needs neither the architect's model nor
    its turn, and in one session it made planning the longest phase of a run."""
    i = task.input_data or {}
    try:
        from common import budget_gate
        work = str(i["workdir"])
        from common import formal
        from swarm import spec as spec_mod
        plan = _mapping(_mapping(i.get("plan")).get("structured"))
        checks = [_mapping(c) for c in _list(plan.get("checks")) if _mapping(c).get("id") and _mapping(c).get("command")]
        # The repository's existing checks stay (they prove what earlier runs built); the plan's new or
        # changed ones are written by an author each, the retired ones dropped.
        merged = spec_mod.merge_checks(spec_mod.read_checks(work), plan)
        os.makedirs(os.path.join(work, ".cc"), exist_ok=True)
        with open(os.path.join(work, spec_mod.CHECKS_FILE), "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=1)
        run = str(i.get("runId") or "")
        # A check the repository already has, with the same command and its script in place, is not written again:
        # the plan re-listing it (a follow-up) re-authored all 30 existing checks (run 0a717ffe).
        have = {str(c.get("id")): c for c in spec_mod.read_checks(work)}
        checks = [c for c in checks if not (str(c["id"]) in have
                                            and str(have[str(c["id"])].get("command")) == str(c.get("command"))
                                            and c.get("file") and os.path.isfile(os.path.join(work, str(c["file"]))))]
        jobs = [("swarm_author_check", f"check_{_ref(c['id'])}", {"check": json.dumps(c, indent=1)}, f"check:{c['id']}")
                for c in checks]
        # The adapter maps the TLA+ spec onto the program: written when there is none yet, and again when
        # this run changed the spec (its actions or its state may have changed).
        adapter = os.path.join(work, ".cc", "conformance", "adapter.mjs")
        spec_changed = bool(git.git(work, "status", "--porcelain", "--", formal.SPEC_DIR, check=False).stdout.strip())
        if str(i.get("specKind") or "") == "tla" and (not os.path.isfile(adapter) or spec_changed):
            jobs.append(("swarm_author_adapter", "adapter", {}, "adapter"))
        spent = budget_gate.read_spend(run)["usd"] if run else 0.0
        ceiling = float(i.get("ceilingUsd") or 0)
        # At least AUTHOR_MIN_USD each: at $0.50 a third of a run's authors ran out mid-write (run 0a717ffe).
        cap = round(max((ceiling - spent) / max(4 * len(jobs), 1), AUTHOR_MIN_USD), 2) if ceiling > 0 else None
        base = {"request": i.get("description") or "", "branch": i.get("branch") or ""}
        tasks, inputs = [], {}
        for key, ref, ctx, author in jobs:
            tasks.append({"name": "coding_agent", "taskReferenceName": f"author_{ref}", "type": "SIMPLE", "optional": True})
            inputs[f"author_{ref}"] = {
                "worktreePath": work, "templateKey": key, "modelRole": "code", "modelTier": 0,
                "promptContext": {**base, **ctx}, "schema": AUTHOR_SCHEMA, "sharedTree": True, "allowedWriteRoots": [],
                "allowedTools": SWARM_ALLOWED_TOOLS, "disallowedTools": SWARM_DISALLOWED_TOOLS,
                "feedbackCorrelationId": run, "feedbackRunId": run, "feedbackItem": author,
                **{k: i.get(k) for k in ("modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256",
                                         "modelsConfig", "modelOverrides", "modelResolution", "skills")},
                "promptTemplate": "", "promptTemplateSource": "",
                **({"maxBudgetUsd": cap} if cap is not None else {}),
                "spendRunId": run, "spendAuthor": f"author:{author}",
            }
        return ok(task, {"dynamicTasks": tasks, "dynamicTasksInput": inputs, "checks": len(merged),
                         "newChecks": len(checks), "authors": len(tasks)},
                  [f"[swarm_authoring] {len(merged)} check(s) in .cc/checks.json ({len(checks)} new or changed); "
                   f"{len(tasks)} author session(s)"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_authoring", e)


@worker_task(task_definition_name="swarm_measure")
def swarm_measure(task):
    """Commit the shared tree, then run each of the architect's checks on it once: exit code and output,
    nothing more. Given ``previous`` (an earlier measurement) of the very same tree, return it instead
    of running anything: the same code and the same commands prove the same thing (run 2bc5d0c6 ran the
    whole-app check 18 times in one round, the architect re-running what had just been run)."""
    i = task.input_data or {}
    try:
        from common import architect, check_execution
        from common.coding_agent import _tool_cache_env
        work = str(i["workdir"])
        # ONE tree, so there is nothing to merge: whoever wrote it, the round's work is one commit.
        # Agents never commit (Codex's sandbox protects .git; run 18e18490 had every commit refused).
        git.git(work, "add", "-A")
        if git.git(work, "diff", "--cached", "--quiet", check=False).code != 0:
            git.git(work, "commit", "--quiet", "--no-verify", "-m", str(i.get("label") or "swarm round").strip())
        commit = git.head(work)
        tree = git.git(work, "rev-parse", "HEAD^{tree}").stdout.strip()
        # What this commit changed, file by file: the architect starts its review here instead of
        # reading every patch of the round (3.7M tokens for a six-item round, run 032b778c).
        changes = git.git(work, "show", "--stat", "--format=", "HEAD", check=False).stdout.strip()[-6000:]
        previous = _mapping(i.get("previous"))
        if previous.get("tree") == tree and previous.get("results") is not None:
            return ok(task, {**{k: previous.get(k) for k in ("results", "allGreen", "summary")},
                             "tree": tree, "commit": commit, "reused": True, "changes": changes},
                      [f"[swarm_measure] tree {tree[:12]} unchanged since it was measured: reusing that result"])
        try:
            with open(os.path.join(work, ".cc", "checks.json"), encoding="utf-8") as fh:
                checks = [_mapping(c) for c in _list(json.load(fh))]
        except (OSError, ValueError) as e:
            return ok(task, {"results": [], "allGreen": False, "summary": f"no readable .cc/checks.json: {e}",
                             "brief": "", "tree": tree, "commit": commit, "reused": False, "changes": changes})
        from common import test_budget
        env = {**check_execution.inherited_environment(_tool_cache_env(work)), **test_budget.HARNESS_ENV}
        results = []
        # What the console shows while the checks run: which one, for how long, and how the finished
        # ones went (a run page that said only "measure" for twelve minutes told nobody anything).
        from common.progress import ProgressReporter
        import threading
        import time as _time
        current = {"n": 0, "check": {}, "since": _time.monotonic()}
        reporter = ProgressReporter(task, heartbeat_s=10.0).start()
        stop = threading.Event()

        def _progress() -> None:
            while not stop.wait(5.0):
                c = current["check"]
                done = "; ".join(f"{r['id']}: {'stalled' if r.get('stalled') else 'exit ' + str(r['exitCode'])}"
                                 for r in results)
                reporter.update({"progress": f"check {current['n']} of {len(checks)}: {c.get('id')} "
                                             f"(`{c.get('command')}`), running for "
                                             f"{_time.monotonic() - current['since']:.0f} s"
                                             + (f"\nfinished: {done}" if done else "")})
        threading.Thread(target=_progress, daemon=True).start()
        try:
            _run_checks(checks, results, current, work, env, check_execution, architect)
            kind = str(i.get("specKind") or "none")
            try:
                _run_spec(kind, results, current, work, env)
            except Exception as e:  # noqa: BLE001 - a fault in the proof machinery is a red check, never a failed run
                results.append({"id": "spec-conformance" if kind == "tla" else "spec-coverage",
                                "command": f"check the program against the {kind} spec",
                                "exitCode": 1, "seconds": 0, "stalled": False,
                                "output": f"the harness could not check the spec: {type(e).__name__}: {e}"})
        finally:
            stop.set()
            reporter.stop()
        return _measured(task, results, tree=tree, commit=commit, changes=changes)
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_measure", e)


CHECK_TIMES = os.path.join(".cc-cache", "check-times.json")
CHECK_BUDGET_FACTOR, CHECK_BUDGET_MARGIN_S = 3, 60


def _check_budget(times: dict, c: dict) -> tuple[float | None, float | None]:
    """``(expected_s, typical_s)`` for one check from its earlier runs in this tree (None without any)."""
    seen = [float(x) for x in times.get(f"{c.get('id')}|{c.get('command')}") or []]
    if not seen:
        return None, None
    typical = max(seen)
    return CHECK_BUDGET_FACTOR * typical + CHECK_BUDGET_MARGIN_S, typical


def _run_checks(checks, results, current, work, env, check_execution, architect) -> None:
    import time as _time
    times_path = os.path.join(work, CHECK_TIMES)
    try:
        times = json.load(open(times_path, encoding="utf-8"))
    except (OSError, ValueError):
        times = {}
    for n, c in enumerate(checks, 1):
        current.update(n=n, check=c, since=_time.monotonic())
        try:
            # The progress rule: a check that blocks (a race, a lost reply) is stopped and reported
            # stalled after five quiet minutes instead of holding the run forever; one that works,
            # however slowly, always shows output or CPU (run 169589fd waited 736 s on a dropped reply).
            # The expected-duration rule: a check that usually takes seconds and is still running minutes
            # later is hung (run 221d632a); a slow one keeps the time its history shows it needs.
            expected, typical = _check_budget(times, c)
            r = check_execution.execute(architect.check_argv(str(c.get("command") or "")), cwd=work, env=env,
                                        stall_after_s=STALL_AFTER_S, expected_s=expected)
            results.append({"id": c.get("id"), "command": c.get("command"), "exitCode": r.exit_code,
                            "seconds": r.duration_seconds, "stalled": r.stalled, "overran": r.overran,
                            "typicalSeconds": typical,
                            "output": ((r.stdout or "") + (r.stderr or ""))[-3000:]})
            if not (r.stalled or r.overran):
                k = f"{c.get('id')}|{c.get('command')}"
                times[k] = ((times.get(k) or []) + [round(r.duration_seconds, 1)])[-5:]
        except (ValueError, OSError) as e:
            results.append({"id": c.get("id"), "command": c.get("command"), "exitCode": None, "output": str(e)})
    os.makedirs(os.path.dirname(times_path), exist_ok=True)
    with open(times_path, "w", encoding="utf-8") as fh:
        json.dump(times, fh)


def _run_spec(kind: str, results, current, work, env) -> None:
    """The run's spec, of the kind its plan settled (``specKind``), checked on the committed tree. The kind
    comes from the run, not from what the tree holds: a spec removed after planning is a red check, never
    a check that silently stops existing."""
    if kind == "tla":
        _run_formal(results, current, work, env)
    elif kind == "openspec":
        _run_openspec(results, current, work)


def _run_openspec(results, current, work) -> None:
    """The OpenSpec spec: still well formed, every scenario still proven by a check, and which requirements
    hold on this tree (a scenario holds when every check covering it passed, measured just before)."""
    import time as _time
    from common import openspec_spec
    from swarm import spec as spec_mod
    current.update(n=len(results) + 1, check={"id": "spec-coverage", "command": "OpenSpec coverage"}, since=_time.monotonic())
    proof = openspec_spec.prove(work, spec_mod.read_checks(work))
    held = openspec_spec.status(proof, results) if proof.get("requirements") else ""
    if proof.get("missing"):
        proof["feedback"] = (f"The run's OpenSpec spec is gone from {openspec_spec.SPECS}/: it was there after planning. "
                             "Restore it.")
    results.append({"id": "spec-coverage", "command": f"validate {openspec_spec.SPECS} and its coverage by the checks",
                    "exitCode": 0 if proof.get("ok") else 1, "seconds": 0, "stalled": False,
                    "output": "\n".join(x for x in (proof.get("summary"), proof.get("feedback"), held) if x)})


def _run_formal(results, current, work, env) -> None:
    """The TLA+ spec: the proof (cached by the spec's hash, so it re-runs only when the spec changed) and,
    once proven, the conformance of the program to every transition of the bounded model."""
    import time as _time
    from common import conformance, formal
    if not os.path.isfile(os.path.join(work, formal.SPEC_DIR, "abstraction.json")):
        results.append({"id": "spec-proof", "command": f"TLC model check of {formal.SPEC_DIR}", "exitCode": 1,
                        "seconds": 0, "stalled": False,
                        "output": f"The run's TLA+ spec is gone from {formal.SPEC_DIR}/ (no abstraction.json): it was "
                                  "there after planning. Restore it; the program is checked against it."})
        return
    current.update(n=len(results) + 1, check={"id": "spec-proof", "command": "TLC model check"}, since=_time.monotonic())
    started = _time.monotonic()
    proof = formal.prove(work)
    results.append({"id": "spec-proof", "command": f"TLC model check of {formal.SPEC_DIR}", "exitCode": 0 if proof.get("ok") else 1,
                    "seconds": round(_time.monotonic() - started, 1), "stalled": False,
                    "output": (proof.get("summary") or "") + ("\n" + proof["feedback"] if proof.get("feedback") else "")})
    if not proof.get("ok"):
        return
    current.update(n=len(results) + 1, check={"id": "spec-conformance", "command": "replay every trace"}, since=_time.monotonic())
    r = conformance.run(work, env=env)
    results.append({"id": "spec-conformance", "command": "replay the spec's traces through the adapter",
                    "exitCode": 0 if r.get("ok") else 1, "seconds": r.get("seconds") or 0, "stalled": False,
                    "output": conformance.report(r)[-3000:]})


RESUME_NOTE = ("THIS RUN CONTINUES AN EARLIER RUN, from where it stopped: its plan, checks and code are in this tree, so "
               "there was no new design and no new plan, and this first round built nothing. Check what exists against "
               "the WHOLE request: every failing check, and anything the request asks for that no check covers yet. Hand "
               "out an item for each gap (adding a check where one is missing), and nothing for what already works.")


def _resume_phase(work: str, i: dict) -> str:
    """Where a run picks up. A new request: "new" (from the beginning). A continuation of an earlier run (the
    console's `continueFrom`): from the phase that run stopped in, read from the tree it left: its checks there mean
    the plan was made ("build": straight to the rounds, which measure what exists and hand out only the gaps); design
    documents without checks mean the design was done ("plan": the lead plans alone from it); nothing means it
    stopped in design ("design"). A continuation that started over re-designed six parts and re-authored 30 checks
    before building anything (run 0a717ffe)."""
    from swarm import spec as spec_mod
    continuing = bool(str(i.get("continueFrom") or "").strip()) or str(i.get("description") or "").lstrip().startswith(
        "Follow-up to an earlier swarm run")
    if not continuing:
        return "new"
    if spec_mod.read_checks(work):
        return "build"
    if spec_mod.design_docs(work) or os.path.isfile(os.path.join(work, ".cc", "design.md")):
        return "plan"
    return "design"


def _brief(results: list[dict]) -> str:
    """The checks' state in a few lines: what already passes (keep it) and what fails, with its last line."""
    if not results:
        return ""
    good = [r["id"] for r in results if r["exitCode"] == 0]
    bad = [r for r in results if r["exitCode"] != 0]
    tail = lambda r: ((r.get("output") or "").strip().splitlines() or ["(no output)"])[-1][:200]
    return (f"THE EXISTING CHECKS, RUN ON THE TREE AS IT IS NOW: {len(good)} of {len(results)} pass.\n"
            + (f"PASSING (this already works: keep it, do not redesign or rebuild it): {', '.join(good)}\n" if good else "")
            + ("FAILING (what is left to do):\n" + "\n".join(f"- {r['id']}: {tail(r)}" for r in bad) if bad else
               "Nothing fails."))


def _measured(task, results, *, tree: str, commit: str, changes: str = ""):
    green = bool(results) and all(r["exitCode"] == 0 for r in results)
    summary = "\n\n".join(f"- {r['id']} (`{r['command']}`) "
                            + (f"STALLED after {r['seconds']:.0f} s: no output and no CPU, stopped"
                               if r.get("stalled") else
                               f"STOPPED after {r['seconds']:.0f} s: it usually takes about "
                               f"{r.get('typicalSeconds') or 0:.0f} s, so it is hung; find out why"
                               if r.get("overran") else f"exit {r['exitCode']} in {r.get('seconds') or 0:.0f} s")
                            + f"\n{r['output']}" for r in results)
    return ok(task, {"results": results, "allGreen": green, "summary": summary or "no checks listed",
                     "brief": _brief(results), "tree": tree, "commit": commit, "reused": False, "changes": changes},
              [f"[swarm_measure] {sum(r['exitCode'] == 0 for r in results)}/{len(results)} green"])


@worker_task(task_definition_name="swarm_round")
def swarm_round(task):
    """Fold one round. Without ``evaluation`` (after the agents): their reports, each item's state on the
    board, and what each rung did for each kind of item (measured, for the architect to read next time).
    With it (after the architect): commit the tree, the verdict, the spend, whether to go on."""
    i = task.input_data or {}
    try:
        from common import budget_gate, rung_policy, team_board
        run = str(i.get("runId") or "")
        rnd = int(i.get("round") or 0)
        repo_key = rung_policy.repo_key(str(i.get("repoPath") or ""))
        evaluation = _mapping(i.get("evaluation"))
        reports = _round_facts(run, rnd, REPORT_KIND) if run else []
        restored: list[dict] = []
        work = str(i.get("workdir") or "")
        if not evaluation and work and os.path.isdir(work):
            # The round's work is over: put the lead's files back where an item changed them without the lead
            # handing them over (the agents share one tree; what must not change is checked, not who wrote it).
            from swarm import team as team_mod
            graph = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1] if run else {}
            grants = [p for x in _list(graph.get("items")) for p in team_mod.owns(_mapping(x))]
            restored = team_mod.restore(work, rnd, grants)
            if run and restored:
                from common import blackboard as bb
                bb.post_fact(run, "harness", "protected_restored", {"round": rnd, "files": restored})
                team_board.post_lesson(run, "harness",
                                       "Put back the lead architect's files an item changed this round: "
                                       + ", ".join(f"{x['path']} ({x['action']})" for x in restored[:12])
                                       + ". Only the lead changes the seams, the spec and the checks; post "
                                       "`board decide` with the evidence instead.")
        if run and not evaluation:
            # After the agents: what each item's session did, measured per kind and rung for the next
            # run's architect (done in one session AND accepted by its review), and what never started.
            graph = (_round_facts(run, rnd, ROUND_KIND) or [{}])[-1]
            reported = {r.get("item") for r in reports}
            board_items = team_board.fold_board(run)["items"]
            for r in reports:
                rung_policy.record(repo_key, role="code", kind=str(r.get("kind") or "item"), rung=int(r.get("rung") or 0),
                                   ok=bool(r.get("done") and r.get("accepted")), cause=str(r.get("cause") or ""),
                                   item=r.get("item"), run=run)
            for x in _list(graph.get("items")):
                iid = _mapping(x).get("id")
                if iid not in reported:
                    reports.append({"item": iid, "notStarted": True,
                                    "reason": (board_items.get(iid) or {}).get("reason") or "not started"})
            if restored:
                reports.append({"item": "harness", "restoredLeadFiles": restored,
                                "reason": "items changed the lead's files; the harness put them back"})
            # How much of the round went to test runs, and to which (common/test_budget.py).
            from common import test_budget
            tests = test_budget.summary(test_budget.runs(work, float(graph.get("startedAt") or 0))) if work else {}
            if tests:
                from common import blackboard as bb
                bb.post_fact(run, "harness", "test_time", {"round": rnd, **tests})
                reports.append({"item": "harness", "testTime": test_budget.summary_text(tests)})
        # The round review's rung: the cheapest that has delivered rounds before (history across all
        # repositories), one higher for each round of this run that has not delivered. Starting it on
        # the cheapest rung every time cost run 649fa3c8 two extra rounds: terra handed back
        # "verify" items twice before sol fixed the integration itself.
        round_rung, round_reason = 0, ""
        if not evaluation:
            top = _ladder_top(i.get("modelResolution"), "review")
            start, round_reason = rung_policy.start_rung(ROUND_REVIEW_REPO, role="round_review", kind="round", top=top)
            round_rung = min(start + max(rnd - 1, 0), top)
        spend = budget_gate.read_spend(run)["usd"] if run else 0.0
        verdict = _mapping(evaluation.get("structured"))
        delivered = bool(verdict.get("delivered")) and bool(i.get("allGreen"))
        next_items = _list(verdict.get("items"))
        if run and evaluation:
            if i.get("roundReviewRung") not in (None, ""):
                rung_policy.record(ROUND_REVIEW_REPO, role="round_review", kind="round",
                                   rung=int(i["roundReviewRung"]), ok=delivered, item=f"round {rnd}", run=run)
            board = team_board.fold_board(run)["items"]
            again = {str(_mapping(x).get("id")) for x in next_items}
            # An item the review hands out again under a new id names what it redoes (``replaces``): the reviews
            # that accepted those items missed something (every review counted as a success while the round
            # review re-issued work as fix-... items: 441 of 441 recorded).
            again |= {str(y).strip() for x in next_items for y in _list(_mapping(x).get("replaces")) if str(y).strip()}
            # How each item review did, for the review ladder: one that accepted an item the architect
            # then handed back missed something, so that kind of item gets a stronger reviewer next time.
            for r in reports:
                # The reviewer accepted it: right if the harness kept it (the smoke check still passed) and the
                # round review did not hand it back.
                if r.get("reviewAccepted", r.get("accepted")) is True:
                    rung_policy.record(repo_key, role="review", kind=str(r.get("kind") or "item"),
                                       rung=int(r.get("reviewRung") or 0),
                                       ok=bool(r.get("accepted")) and r.get("item") not in again,
                                       item=r.get("item"), run=run)
            # The architect's judgment of the round settles every item it did not hand back: one an agent
            # left unfinished ("reopened") that the architect then completed is done too, or it stayed
            # "reopened" on a delivered run's board (run dc50a7bb, launcher-integration). A delivered run
            # also settles items held or never started: the architect delivered without them.
            settles = {team_board.REVIEW, team_board.REOPENED} | (
                {team_board.BLOCKED, team_board.BACKLOG} if delivered else set())
            for iid, it in board.items():
                if it.get("state") in settles and (delivered or iid not in again):
                    team_board.set_state(run, "architect", iid, team_board.DONE,
                                         reason={team_board.REVIEW: "the architect accepted it",
                                                 team_board.REOPENED: "the architect completed it in review"}.get(
                                             it.get("state"), "the run was delivered without it"))
        over = float(i.get("ceilingUsd") or 0) > 0 and spend >= float(i["ceilingUsd"])
        # The verification gate: once per run, when the lead would deliver a green build and the run has
        # verifiers (swarm/verify.py). Its outcome decides whether the run is delivered after all.
        gate, focuses = "skip", []
        if evaluation and delivered and not over and run:
            from common import blackboard as bb
            from swarm import verify as verify_mod
            focuses = verify_mod.plan_focuses(verify_mod.verifiers_mode(i.get("verifiers")),
                                              _list(_mapping(i.get("verification")).get("focuses")))
            if focuses and not bb.read_facts(run, kind=GATE_KIND):
                gate = "verify"
        return ok(task, {"delivered": delivered, "stop": delivered or over, "overBudget": over,
                         "gate": gate, "focuses": focuses,
                         "items": next_items, "assessment": verdict.get("assessment") or "",
                         "reports": reports, "spentUsd": round(spend, 4),
                         "roundReviewRung": round_rung, "roundReviewReason": round_reason,
                         **_granted(run, work)},
                  [f"[swarm_round] delivered={delivered} spent=${spend:.2f} next={len(next_items)}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_round", e)


MAX_FINDINGS = 5  # per verifier: more than that from one reader is a flood, not a review


def _rule_schema() -> dict:
    from swarm import verify as verify_mod
    return {"type": "object", "properties": {**verify_mod.RULE_SCHEMA["properties"], **ITEMS_SCHEMA["properties"]},
            "required": ["rulings", "assessment", "items"]}


def _copy_path(work: str, run: str, name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(work)), f"verify-{run[:8]}-{name}")


@worker_task(task_definition_name="swarm_verify_fork")
def swarm_verify_fork(task):
    """Start the verification gate: one session per focus, each in its own throwaway copy of the committed
    tree (the request-fidelity one with no design, no checks, no board and no history, so it cannot inherit
    the lead's reading), with no board feed. Marks the run as verified so the gate never runs twice."""
    i = task.input_data or {}
    try:
        from common import budget_gate, prompt_files
        from common import blackboard as bb
        from swarm import team as team_mod
        from swarm import verify as verify_mod
        work, run = str(i["workdir"]), str(i.get("runId") or "")
        focuses = [f for f in _list(i.get("focuses")) if f in verify_mod.FOCUSES][:verify_mod.MAX_VERIFIERS]
        if run:
            bb.post_fact(run, "harness", GATE_KIND, {"round": int(i.get("round") or 0), "focuses": focuses})
        spent = budget_gate.read_spend(run)["usd"] if run else 0.0
        ceiling = float(i.get("ceilingUsd") or 0)
        cap = round(max((ceiling - spent) / max(3 * len(focuses), 1), 0.5), 2) if ceiling > 0 else None
        tasks, inputs, copies = [], {}, {}
        for focus in focuses:
            blind = focus == "fidelity"
            copy = verify_mod.copy_tree(work, _copy_path(work, run, focus),
                                        strip=(".git", ".cc", "design", ".cc-cache/prompts", ".cc-cache/bin") if blind else ())
            ctx = {"request": i.get("description") or "", "focus": focus, "focusBrief": verify_mod.FOCUSES[focus],
                   "measurement": "" if blind else ("The harness ran the team's acceptance checks on this tree and all "
                                                    "pass; do not re-run them:\n" + str(i.get("measurement") or "")),
                   "precedence": "" if blind else team_mod.PRECEDENCE,
                   "maxFindings": str(MAX_FINDINGS), "maxNotes": str(verify_mod.MAX_NOTES)}
            ref = f"verify_{focus}"
            path = prompt_files.stash(copy, ref, f"verify {focus}", ctx)
            tasks.append({"name": "coding_agent", "taskReferenceName": ref, "type": "SIMPLE", "optional": True})
            inputs[ref] = {
                "worktreePath": copy, "templateKey": "swarm_verify", "promptPath": path, "promptContext": {},
                "modelRole": "judge", "schema": verify_mod.VERIFY_SCHEMA, "sharedTree": False, "allowedWriteRoots": [],
                "allowedTools": SWARM_ALLOWED_TOOLS, "disallowedTools": SWARM_DISALLOWED_TOOLS, "maxTurns": 80,
                **{k: i.get(k) for k in ("modelProfile", "modelPolicy", "modelPolicySource", "modelPolicySha256",
                                         "modelsConfig", "modelOverrides", "modelResolution", "skills")},
                "promptTemplate": "", "promptTemplateSource": "",
                **({"maxBudgetUsd": cap} if cap is not None else {}),
                "spendRunId": run, "spendAuthor": f"verify:{focus}",
            }
            copies[ref] = {"focus": focus, "path": copy}
        return ok(task, {"dynamicTasks": tasks, "dynamicTasksInput": inputs, "copies": copies, "focuses": focuses},
                  [f"[swarm_verify_fork] {len(tasks)} verifier(s): {', '.join(focuses) or 'none'}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_verify_fork", e)


@worker_task(task_definition_name="swarm_gate")
def swarm_gate(task):
    """Fold the verifiers' findings into what the lead rules on. A finding stands only when the harness
    reproduces it and it quotes the request, the spec or the seams; the rest are follow-ups, notes or
    dropped, each with its reason. Same requirement, one finding; already ruled, not raised again."""
    i = task.input_data or {}
    try:
        import shutil
        from common import check_execution
        from common.coding_agent import _tool_cache_env
        from common import blackboard as bb
        from swarm import spec as spec_mod
        from swarm import verify as verify_mod
        work, run = str(i["workdir"]), str(i.get("runId") or "")
        fork = _mapping(i.get("fork"))
        # Inside the round loop Conductor names each forked task `<ref>__<iteration>` in the join's output.
        results = {re.sub(r"__\d+$", "", str(k)): v for k, v in _mapping(i.get("results")).items()}
        sources = verify_mod.quote_sources(work, str(i.get("description") or ""))
        ruled = {str(e.get("key")): e for e in verify_mod.read_ledger(work)}
        checks = {str(c.get("id")): c for c in spec_mod.read_checks(work)}
        notes, followups, dropped, timed_out = [], [], [], []
        groups: dict[str, list[dict]] = {}
        for ref, meta in _mapping(fork.get("copies")).items():
            meta = _mapping(meta)
            focus, copy = str(meta.get("focus")), str(meta.get("path"))
            st = _mapping(_mapping(results.get(ref)).get("structured"))
            if not st:
                timed_out.append(focus)
                continue
            notes += [f"[{focus}] {n}" for n in _list(st.get("notes"))[:verify_mod.MAX_NOTES]]
            found = [_mapping(f) for f in _list(st.get("findings"))]
            for f in found[MAX_FINDINGS:]:
                dropped.append({"focus": focus, "requirement": f.get("requirement"), "why": f"over {MAX_FINDINGS} findings from one verifier"})
            for f in found[:MAX_FINDINGS]:
                f = {**f, "focus": focus, "copy": copy}
                if not all(str(f.get(k) or "").strip() for k in ("requirement", "expected", "observed", "file", "command")):
                    dropped.append({"focus": focus, "requirement": f.get("requirement"), "why": "incomplete finding"})
                    continue
                k = verify_mod.key(f["requirement"])
                if k in ruled:
                    dropped.append({"focus": focus, "requirement": f["requirement"],
                                    "why": f"already ruled ({ruled[k].get('ruling')}): {ruled[k].get('reason') or ''}"[:300]})
                    continue
                problem = verify_mod.script_problem(f, work)
                if not problem and not os.path.isfile(os.path.join(copy, f["file"])):
                    problem = f"its file `{f['file']}` is not in the verifier's copy"
                if problem:
                    dropped.append({"focus": focus, "requirement": f["requirement"], "why": f"unusable reproduction: {problem}"})
                    continue
                # Where the quote is decides blocking or follow-up, AFTER the harness reproduced it: nothing
                # unreproduced reaches the lead or the run's report.
                k = next((g for g, members in groups.items()
                          if verify_mod.same_requirement(members[0]["requirement"], f["requirement"])), k)
                groups.setdefault(k, []).append({**f, "source": verify_mod.quoted_in(f["requirement"], sources)})
        from common import test_budget
        env = {**check_execution.inherited_environment(_tool_cache_env(work)), **test_budget.HARNESS_ENV}
        blocking = []
        for n, (k, candidates) in enumerate(groups.items(), 1):
            if n > verify_mod.MAX_REPRODUCTIONS:
                dropped += [{"focus": c["focus"], "requirement": c["requirement"], "why": "over the reproduction cap"}
                            for c in candidates]
                continue
            fid = f"v{n}"
            outcome = None
            for c in candidates:
                outcome = verify_mod.reproduce(work, c, os.path.join(c["copy"], c["file"]),
                                               _copy_path(work, run, "repro"), env, checks)
                if outcome["ok"] and not c["source"]:
                    followups.append({"focus": ", ".join(sorted({x["focus"] for x in candidates})),
                                      "requirement": c["requirement"], "expected": c["expected"],
                                      "observed": c["observed"], "key": k,
                                      "why": "reproduced, but it quotes nothing in the request, the spec or the seams"})
                    break
                if outcome["ok"]:
                    # Kept in the run's tree (git-excluded) so the check can be adopted after the copies go.
                    keep = os.path.join(work, verify_mod.KEEP, fid)
                    os.makedirs(keep, exist_ok=True)
                    kept = os.path.join(keep, os.path.basename(c["file"]))
                    shutil.copy2(os.path.join(c["copy"], c["file"]), kept)
                    blocking.append({"id": fid, "key": k, "focuses": sorted({x["focus"] for x in candidates}),
                                     **{x: c.get(x) or "" for x in ("requirement", "source", "expected", "observed",
                                                                    "file", "command", "mutation", "check")},
                                     "kept": kept, "reproduction": outcome,
                                     # A check-strength finding only from the checks verifier (verify.reproduce).
                                     **({} if c.get("focus") == "checks" else {"mutation": "", "check": ""})})
                    break
            if not outcome or not outcome["ok"]:
                dropped.append({"focus": ", ".join(sorted({x["focus"] for x in candidates})),
                                "requirement": candidates[0]["requirement"],
                                "why": f"not reproduced by the harness: {(outcome or {}).get('why')}"})
        if run:
            bb.post_fact(run, "harness", "swarm_gate_fold", {
                "blocking": [{k2: b[k2] for k2 in ("id", "focuses", "requirement", "command")} for b in blocking],
                "followUps": len(followups), "dropped": dropped, "notes": notes, "timedOut": timed_out})
        counts = {"blocking": len(blocking), "followUps": len(followups), "dropped": len(dropped), "notes": len(notes),
                  "timedOut": len(timed_out)}
        return ok(task, {"blocking": blocking, "hasBlocking": "rule" if blocking else "none",
                         "table": verify_mod.table(blocking), "followUps": followups, "notes": notes,
                         "dropped": dropped, "timedOut": timed_out, "counts": counts},
                  [f"[swarm_gate] {counts}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_gate", e)


@worker_task(task_definition_name="swarm_gate_apply")
def swarm_gate_apply(task):
    """Carry out the lead's rulings. A fix makes the finding's reproduction a frozen check (the ordinary
    rounds then prove the fix once, and it can never regress); a defect to fix reopens the run for one more
    round; follow-ups go to the run's output. Every ruling is kept in ``.cc/findings.json`` so it is never
    raised again, and recorded per verifier so a verifier this repository keeps rejecting is skipped."""
    i = task.input_data or {}
    try:
        import shutil
        from common import budget_gate, rung_policy
        from common import blackboard as bb
        from swarm import spec as spec_mod
        from swarm import verify as verify_mod
        work, run = str(i["workdir"]), str(i.get("runId") or "")
        gate = _mapping(i.get("gate"))
        rule = _mapping(_mapping(i.get("rule")).get("structured"))
        rulings = {str(_mapping(r).get("id")): _mapping(r) for r in _list(rule.get("rulings"))}
        repo_key = rung_policy.repo_key(str(i.get("repoPath") or ""))
        ledger = verify_mod.read_ledger(work)
        checks = spec_mod.read_checks(work)
        adopted, fixes, followups, rejected = [], [], [], []
        for f in (_mapping(x) for x in _list(gate.get("blocking"))):
            r = rulings.get(str(f.get("id"))) or {"ruling": "followup", "reason": "the lead gave no ruling"}
            ruling = str(r.get("ruling") or "followup")
            if ruling == "fix":
                dst = os.path.join(work, str(f["file"]))
                if os.path.lexists(dst):
                    ruling, r = "followup", {"reason": f"its check path {f['file']} is taken"}
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(str(f["kept"]), dst)
                    cid = "verify-" + _ref(f["id"] + "-" + str(f["requirement"]))
                    checks.append({"id": cid, "file": f["file"], "command": f["command"], "covers": [],
                                   "scenario": f"{f['requirement']} (found by verification: expected {f['expected']}; "
                                               f"observed {f['observed']})"})
                    adopted.append(cid)
                    if not f.get("mutation"):
                        fixes.append(f["id"])
            if ruling == "followup":
                followups.append({"requirement": f["requirement"], "expected": f["expected"], "observed": f["observed"],
                                  "why": r.get("reason") or "", "focus": ", ".join(_list(f.get("focuses")))})
            elif ruling == "reject":
                rejected.append({"id": f["id"], "requirement": f["requirement"], "why": r.get("reason") or ""})
            for focus in _list(f.get("focuses")):
                rung_policy.record(repo_key, role="verify", kind=str(focus), rung=0, ok=ruling == "fix",
                                   item=str(f.get("id")), run=run)
            ledger.append({"key": f.get("key"), "requirement": f["requirement"], "ruling": ruling,
                           "reason": r.get("reason") or "", "run": run, "focuses": _list(f.get("focuses"))})
        for f in (_mapping(x) for x in _list(gate.get("followUps"))):
            followups.append({k: f.get(k) for k in ("requirement", "expected", "observed", "why", "focus")})
            ledger.append({"key": f.get("key"), "requirement": f.get("requirement"), "ruling": "followup",
                           "reason": f.get("why") or "", "run": run, "focuses": [f.get("focus")]})
        if adopted:
            with open(os.path.join(work, spec_mod.CHECKS_FILE), "w", encoding="utf-8") as fh:
                json.dump(checks, fh, indent=1)
        if _list(gate.get("blocking")) or _list(gate.get("followUps")):
            verify_mod.write_ledger(work, ledger)
        spend = budget_gate.read_spend(run)["usd"] if run else 0.0
        over = float(i.get("ceilingUsd") or 0) > 0 and spend >= float(i["ceilingUsd"])
        reopen = bool(fixes) and not over
        delivered = not fixes
        git.git(work, "add", "-A")
        if git.git(work, "diff", "--cached", "--quiet", check=False).code != 0:
            git.git(work, "commit", "--quiet", "--no-verify", "-m",
                    f"verification: {len(adopted)} finding(s) adopted as checks")
        for meta in _mapping(_mapping(i.get("fork")).get("copies")).values():
            shutil.rmtree(str(_mapping(meta).get("path") or ""), ignore_errors=True)
        counts = _mapping(gate.get("counts"))
        summary = (f"Verification ({', '.join(_list(_mapping(i.get('fork')).get('focuses'))) or 'no verifiers'}): "
                   f"{counts.get('blocking', 0)} reproduced finding(s): {len(fixes)} to fix, "
                   f"{len(adopted) - len(fixes)} weak check(s) strengthened, {len(followups)} follow-up(s), "
                   f"{len(rejected)} rejected; {counts.get('dropped', 0)} dropped by the harness"
                   + (f"; {len(_list(gate.get('timedOut')))} verifier(s) gave no answer" if _list(gate.get("timedOut")) else "")
                   + (". Over the ceiling: the accepted defects are not fixed in this run" if fixes and over else "."))
        if followups:
            summary += "\nFollow-ups:\n" + "\n".join(f"- {x['requirement']}: expected {x['expected']}; observed "
                                                     f"{x['observed']}" for x in followups[:20])
        assessment = "\n\n".join(x for x in (str(i.get("assessment") or "").strip(), summary,
                                              str(rule.get("assessment") or "").strip()) if x)
        verification = {"focuses": _list(_mapping(i.get("fork")).get("focuses")), "counts": counts,
                        "fixed": fixes, "adoptedChecks": adopted, "followUps": followups, "rejected": rejected,
                        "notes": _list(gate.get("notes")), "dropped": _list(gate.get("dropped")),
                        "timedOut": _list(gate.get("timedOut"))}
        if run:
            bb.post_fact(run, "harness", "swarm_gate_rulings", verification)
        return ok(task, {"reopen": reopen, "delivered": delivered and not (fixes and over),
                         "items": _list(rule.get("items")) if reopen else [], "assessment": assessment,
                         "verification": verification, "followUps": followups},
                  [f"[swarm_gate_apply] {summary.splitlines()[0]}"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_gate_apply", e)


def phase_times(tasks: list[dict]) -> list[dict]:
    """The run's critical path, phase by phase: wall minutes and the member it waited on (the slowest of the phase's
    parallel agents). Speed is a number per run, not an impression."""
    def base(t):
        return re.sub(r"__\d+$", "", t.get("referenceTaskName") or "")
    def span(ts):
        ts = [t for t in ts if t.get("startTime") and t.get("endTime")]
        return ((max(t["endTime"] for t in ts) - min(t["startTime"] for t in ts)) / 60000) if ts else 0.0
    def slowest(ts):
        ts = [t for t in ts if t.get("startTime") and t.get("endTime")]
        if not ts:
            return "", 0.0
        t = max(ts, key=lambda t: t["endTime"] - t["startTime"])
        return base(t), (t["endTime"] - t["startTime"]) / 60000
    groups = [("split", lambda b: b == "kickoff", None),
              ("design", lambda b: b.startswith("item_design-") or b in ("design_dispatch", "designs", "designs_join"),
               lambda b: b.startswith("item_design-") and not b.startswith("item_design-gap-")),
              ("review", lambda b: b == "design_review", None),
              ("gaps", lambda b: b.startswith("item_design-gap-") or b in ("gap_dispatch", "gaps", "gaps_join"),
               lambda b: b.startswith("item_design-gap-")),
              ("plan", lambda b: b == "plan", None),
              ("spec gate", lambda b: b == "formal", None),
              ("check authors", lambda b: b.startswith("author") or b == "authoring", lambda b: b.startswith("author_")),
              ("verification", lambda b: b in ("verify_fork", "verifiers", "verifiers_join", "gate", "rule", "gate_apply")
               or b.startswith("verify_"), lambda b: b.startswith("verify_") and b != "verify_fork")]
    out = []
    for name, member, worker in groups:
        ts = [t for t in tasks if member(base(t))]
        if ts:
            who, mins = slowest([t for t in ts if worker(base(t))]) if worker else ("", 0.0)
            out.append({"phase": name, "minutes": round(span(ts), 1), "waitedOn": who, "waitedOnMinutes": round(mins, 1)})
    rounds = sorted({int(m.group(1)) for t in tasks for m in [re.search(r"__(\d+)$", t.get("referenceTaskName") or "")]
                     if m and base(t) in ("dispatch", "decide")})
    for n in rounds:
        ts = [t for t in tasks if (t.get("referenceTaskName") or "").endswith(f"__{n}")
              and base(t) in ("dispatch", "work", "work_join", "fold", "measure", "evaluate", "remeasure", "decide")]
        items = [t for t in tasks if base(t).startswith("item_") and base(t).endswith(f"_r{n}")]
        who, mins = slowest(items)
        out.append({"phase": f"round {n}", "minutes": round(span(ts), 1), "waitedOn": who, "waitedOnMinutes": round(mins, 1)})
    return out


def _run_tasks(run: str) -> list[dict]:
    import os as _os
    import urllib.request
    server = _os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api").rstrip("/")
    with urllib.request.urlopen(f"{server}/workflow/{run}?includeTasks=true", timeout=None) as r:
        return json.load(r).get("tasks") or []


@worker_task(task_definition_name="swarm_finish")
def swarm_finish(task):
    """Bring the swarm's branch back into the user's repository."""
    i = task.input_data or {}
    try:
        repo, work, branch = str(i["repoPath"]), str(i["workdir"]), str(i["branch"])
        git.git(repo, "fetch", "--quiet", work, f"+{branch}:{branch}")
        head = git.git(repo, "rev-parse", branch).stdout.strip()
        delivered = str(i.get("delivered")).strip().lower() == "true"
        # The same handoff record every branch-owning workflow reports: the console shows the branch,
        # offers merge and pull request, and continues from it.
        handoff = {"sourceRepoPath": repo, "repoPath": repo, "branch": branch, "commit": head,
                   "originalBranch": str(i.get("originalBranch") or ""), "originalHead": str(i.get("originalHead") or "")}
        from common import test_budget
        tests = test_budget.summary(test_budget.runs(work))
        try:
            phases = phase_times(_run_tasks(str(task.workflow_instance_id or "")))
        except Exception:  # noqa: BLE001 - timing is a report, never a failed finish
            phases = []
        return ok(task, {"branch": branch, "head": head, "delivered": delivered, "testTime": tests, "phases": phases,
                         "verdict": "DELIVERED" if delivered else "INCOMPLETE", "sourceHandoff": handoff},
                  [f"[swarm_finish] {branch} is in {repo} ({'delivered' if delivered else 'incomplete'})"])
    except Exception as e:  # noqa: BLE001
        return fail(task, "swarm_finish", e)
