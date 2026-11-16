"""Who does what in a swarm, enforced by the harness rather than asked of the agents.

One lead architect keeps every decision that needs one mind: the parts and the seams between them
(``design/architecture.md``, ``.cc/design.md``), the spec, the checks, the review, delivery. It may hand
the writing of a deep part's design to a ``kind: "design"`` item; that part's build items depend on it
and start the moment it is accepted. These rules keep a team of designers from turning into a
committee and many writers from editing the same thing:

* a design item exists only for a part at least two build items build on, and owns its own document
  (``design/<part>.md``) and the paths it is given in ``owns``, never the lead's files;
* the number of designers is capped (``architects``: the lead plus at most N-1 designers);
* the lead's files are snapshotted when a round starts and anything an item changed in them is put back
  when the round's work ends (the agents share ONE tree, so who wrote a file cannot be told from git; what
  must not change can be). The lead grants an item one of its files by listing it in that item's ``owns``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil

# The lead's files: the seams, the spec, the checks, its rulings on verification findings.
PROTECTED = ("design/architecture.md", ".cc/design.md", "design/spec", ".cc/spec", ".cc/checks.json", ".cc/checks",
             ".cc/conformance", ".cc/openspec", ".cc/findings.json")
SNAPSHOTS = os.path.join(".cc-cache", "protected")
DESIGN_KIND = "design"
MAX_DESIGNERS = 6
MAX_DESIGN_REWORKS = 2
# Architects of subsystems (and gap-fillers) run on the code role's second rung, a strong but fast model (Sonnet at
# high effort; Codex terra at high), not the lead's: six Opus architects spent 7-18 min and $1.6-$4.5 each writing
# documents (run 8da53b05). A rework climbs one rung (Opus).
DESIGNER_ROLE, DESIGNER_RUNG = "code", 1  # a design its review rejects goes back to its architect at most this many times
MIN_DEPENDENTS = 2

# Every role reads the same order when two sources disagree.
PRECEDENCE = ("WHEN SOURCES DISAGREE, the higher one wins: (1) the request; (2) the spec; (3) the seams in "
              "`.cc/design.md` (THE CONTRACT THE CHECKS USE); (4) `design/architecture.md`; (5) the part's own design "
              "document; (6) the item's brief; (7) notes and lessons on the board. Never resolve a disagreement by "
              "changing a higher source yourself: only the lead architect changes the seams, the spec and the checks; "
              "post the conflict with `board decide` and build to the higher source meanwhile.")


def _int_or_auto(value, default: str = "auto"):
    text = str(value if value is not None else "").strip().lower()
    if text in ("", "auto"):
        return default
    try:
        return max(int(float(text)), 0)
    except ValueError as e:
        raise ValueError(f"expected auto or a number, got {value!r}") from e


def architects_mode(value) -> str | int:
    """``auto`` (the lead decides, at most ``MAX_DESIGNERS`` designers) or N architects in all: the lead
    and at most N-1 designers (1 = the lead alone, today's behaviour)."""
    mode = _int_or_auto(value)
    return mode if mode == "auto" else max(mode, 1)


def designer_cap(mode) -> int:
    return MAX_DESIGNERS if mode == "auto" else min(max(int(mode) - 1, 0), MAX_DESIGNERS)


def is_design(item: dict) -> bool:
    return str((item or {}).get("kind") or "").strip().lower() == DESIGN_KIND


def owns(item: dict) -> list[str]:
    out = []
    for p in (item or {}).get("owns") or []:
        norm = os.path.normpath(str(p).strip()).replace(os.sep, "/")
        if norm and norm not in (".", "..") and not norm.startswith("../") and not os.path.isabs(norm):
            out.append(norm)
    return out


def is_protected(path: str) -> bool:
    p = path.replace(os.sep, "/")
    return any(p == root or p.startswith(root + "/") for root in PROTECTED)


def _covered(path: str, grants: list[str]) -> bool:
    return any(path == g or path.startswith(g.rstrip("/") + "/") for g in grants)


def team_guide(mode) -> str:
    """What the plan prompt says about delegating design, for this run's ``architects``."""
    cap = designer_cap(mode)
    if cap == 0:
        return ("This run has one architect, you: write every design document yourself; return no item of kind "
                "\"design\".")
    return (f"A system of several deep parts is designed by a TEAM of architects, you leading at most {cap} "
            "designer(s); a small change, or a system whose parts are all shallow, you design alone. A deep part is one "
            "whose inside needs real design work before anyone can build it (an interpreter, a storage engine, a "
            "protocol, a query planner, a UI with many flows): hand each such part to a designer, an item of kind "
            "\"design\". Decide the parts and the seams between them yourself first, in "
            "`design/architecture.md` and `.cc/design.md` (as text: interfaces, signatures, data shapes, formats): "
            "everything two parts must agree on is yours and is fixed before anyone starts. Then stop at the seams: "
            "do not work out the inside of a part you delegate. A design item writes its part's document "
            "`design/<part>.md` (list it in the item's `owns`; a designer writes no code either); "
            f"it exists only for a part that at least {MIN_DEPENDENTS} build items build on (each lists the design "
            "item in `dependsOn` and starts the moment it is accepted). The design team works the moment your plan "
            "is accepted, every designer at once and beside the check authors, before any build item starts. A small or shallow part needs no designer: "
            "cover it in `architecture.md`. Never delegate the spec, the checks or the seams. Brief build items "
            "briefly: point to their part's document instead of retelling it.")


def kickoff_guide(mode) -> str:
    """What the lead's kickoff is told about forming the design team, for this run's ``architects``."""
    cap = designer_cap(mode)
    return (f"Split the system into the parts that need design work, at most {cap}, one architect each. A small "
            "change, a fix, or a system with nothing to design inside its parts needs no team: return `team` false "
            "and no parts, and you plan it alone next." if cap else
            "This run has one architect, you: return `team` false and no parts.")


def design_items(kickoff: dict, mode, *, spec_kind: str = "none") -> list[dict]:
    """One design item per part of the kickoff (none without a team), each owning its part's document and, with
    an OpenSpec spec, its part's capability spec (the architects write the spec of their parts in parallel)."""
    if not kickoff.get("team"):
        return []
    out, seen = [], set()
    for part in (kickoff.get("parts") or [])[:designer_cap(mode)]:
        if not isinstance(part, dict):
            continue
        slug = "".join(c if c.isalnum() else "-" for c in str(part.get("id") or "").lower()).strip("-")[:40]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        if slug == SPEC_PART:
            continue  # the TLA+ model is the harness's own subsystem (spec_item), never a split part
        owned = [f"design/{slug}.md"] + ([f".cc/openspec/specs/{slug}"] if spec_kind == "openspec" else [])
        out.append({"id": f"design-{slug}", "title": f"Design: {part.get('title') or slug}",
                    "brief": str(part.get("brief") or "") + (f"\nOwns: {part.get('owns')}" if part.get("owns") else ""),
                    "area": str(part.get("owns") or ""), "kind": DESIGN_KIND, "rung": DESIGNER_RUNG,
                    "dependsOn": [], "owns": owned, "spec": spec_kind})
    if out and spec_kind == "tla":
        out.append(spec_item())
    return out


SPEC_PART = "spec"


def spec_item() -> dict:
    """A team run's TLA+ model is one subsystem of its own, written during design against the seams and proven by
    TLC the moment it is done; never the lead's quick review (run b74d3da7's review had 12 turns at low effort)."""
    return {"id": f"design-{SPEC_PART}", "title": "Design: the run's TLA+ model",
            "brief": "Write the TLA+ model of the system's semantic core, against the SEAMS in `design/architecture.md`.",
            "area": "the TLA+ model in .cc/spec/", "kind": DESIGN_KIND, "rung": DESIGNER_RUNG + 1, "dependsOn": [],
            "owns": [".cc/spec"], "spec": "tla"}


def is_spec_item(item: dict) -> bool:
    return str(item.get("id") or "") == f"design-{SPEC_PART}"


def _part(item: dict) -> str:
    return str(item.get("id") or "").removeprefix("design-")


def spec_task(item: dict) -> str:
    """What a part's architect writes of the run's spec; the harness checks it when the architect finishes."""
    kind = str(item.get("spec") or "")
    if is_spec_item(item):
        from swarm import spec as spec_mod
        return ("THE SPEC (TLA+) IS YOUR SUBSYSTEM. " + spec_mod.TLA_GUIDE
                + " Model the seams in `design/architecture.md`; do not design any subsystem's inside.")
    if kind != "openspec":
        return "THE SPEC: none to write for your subsystem" + (
            " (the run's TLA+ model has its own architect)." if kind == "tla" else ".")
    cap = next((p for p in owns(item) if p.startswith(".cc/openspec/specs/")), "")
    part = _part(item)
    return (f"THE SPEC (OpenSpec): write your subsystem's capability in `{cap}/spec.md`: a `## Purpose`, then "
            "`## Requirements` with `### Requirement: <name>` (\"The system SHALL ...\") and under each at least one "
            f"`#### Scenario: {part}: <name>` with a WHEN and a THEN bullet, stating what a user observes, in EXACTLY this "
            f"form (bold keywords, one bullet each; plain `WHEN`/`THEN` is rejected):\n\n"
            f"### Requirement: <name>\nThe system SHALL <behaviour>.\n\n#### Scenario: {part}: <name>\n"
            f"- **WHEN** <what the user does>\n- **THEN** <what the user observes>\n\nEVERY "
            f"scenario name starts with `{part}: ` and is unique in your file; every scenario is proven by one of YOUR "
            "proposed checks (its name in that check's `covers`), and every `covers` names one of your scenarios. The "
            "harness checks all of this the moment you finish and sends any problem back to you.")


def lint_part(work: str, item: dict, report: dict) -> list[str]:
    """Is this architect's own spec sound, and covered by its own proposed checks? Checked when the architect
    finishes, so a spec defect goes back to its author during design, never to the plan (run b74d3da7: two
    capabilities named the same scenario and the gate after the plan sent it to the lead)."""
    kind = str(item.get("spec") or "")
    if is_spec_item(item):
        from common import formal
        proof = formal.prove(work)
        return [] if proof.get("ok") else [f"the TLA+ model is not proven: {proof.get('summary')}. {proof.get('feedback') or ''}".strip()]
    if kind != "openspec":
        return []
    import os
    from common import openspec_spec
    part = _part(item)
    rel = os.path.join(openspec_spec.SPECS, part, "spec.md")
    try:
        with open(os.path.join(work, rel), encoding="utf-8") as fh:
            reqs, problems = openspec_spec.parse(fh.read(), part)
    except OSError:
        return [f"your capability `{rel}` is missing: write it"]
    names = [s["name"] for r in reqs for s in r["scenarios"]]
    keys = [openspec_spec.key(n) for n in names]
    problems += [f"scenario '{n}' does not start with `{part}: `" for n in names
                 if not openspec_spec.key(n).startswith(f"{part.lower()}:")]
    problems += [f"scenario '{n}' appears more than once in your capability" for n, k in zip(names, keys)
                 if keys.count(k) > 1]
    covered: set = set()
    for c in report.get("proposedChecks") or []:
        for n in (c or {}).get("covers") or []:
            k = openspec_spec.key(n)
            covered.add(k)
            if k not in keys:
                problems.append(f"your check '{(c or {}).get('id')}' covers '{n}', which is not one of your scenarios")
    problems += [f"no check of yours proves scenario '{n}'" for n, k in zip(names, keys) if k not in covered]
    return sorted(set(problems))


def team_roster(round_items: list[dict], me: dict) -> str:
    """What the other subsystems own, so an architect stays out of them (it does not coordinate with them)."""
    peers = [x for x in round_items if is_design(x) and x.get("id") != me.get("id")]
    if not peers:
        return "Yours is the only subsystem being designed."
    return ("NOT YOURS (each belongs to another architect; stay out of it):\n" + "\n".join(
        f"- {x.get('id')}: {x.get('title')}: {x.get('area') or x.get('brief')}" for x in peers))


def design_problems(plan: dict, mode) -> list[str]:
    """What is wrong with the plan's design items, in words the lead acts on (``[]`` when nothing)."""
    items = [i for i in (plan.get("items") or []) if isinstance(i, dict)]
    design = [i for i in items if is_design(i)]
    if not design:
        return []
    cap = designer_cap(mode)
    problems = []
    if cap == 0:
        return ["This run has one architect: write the part designs yourself and return no item of kind \"design\" ("
                + ", ".join(str(i.get("id")) for i in design) + ")."]
    if len(design) > cap:
        problems.append(f"{len(design)} design items; this run allows at most {cap}. Merge parts or cover the "
                        "shallow ones in `design/architecture.md`.")
    for d in design:
        did = str(d.get("id") or "").strip()
        mine = owns(d)
        docs = [p for p in mine if p.startswith("design/") and p.endswith(".md")]
        dependents = [i for i in items if did and did in [str(x).strip() for x in i.get("dependsOn") or []]]
        if not docs:
            problems.append(f"Design item `{did}` must list its document `design/<part>.md` in `owns`.")
        code = [p for p in mine if not p.startswith("design/")]
        if code:
            problems.append(f"Design item `{did}` lists code in `owns` ({', '.join(code)}): a designer writes its "
                            "part's design document only; the build items write the code.")
        lead = [p for p in mine if is_protected(p)]
        if lead:
            problems.append(f"Design item `{did}` lists your own files in `owns` ({', '.join(lead)}): the seams, the "
                            "spec and the checks stay yours.")
        if len(dependents) < MIN_DEPENDENTS:
            problems.append(f"Design item `{did}` has {len(dependents)} build item(s) depending on it; a designer "
                            f"is worth it only for a part at least {MIN_DEPENDENTS} build on. Cover it in "
                            "`design/architecture.md` instead.")
        if any(is_design(i) for i in dependents):
            problems.append(f"Design item `{did}` has another design item depending on it: designs of different "
                            "parts depend on the seams you fixed, never on each other.")
    return problems


PLANNING_ROOTS = ("design/", ".cc/")


def non_planning_changes(work: str) -> list[str]:
    """Files the planning session changed outside `design/` and `.cc/`: planning is not coding."""
    from common import git
    out = []
    for line in git.git(work, "status", "--porcelain", "-uall", check=False).stdout.splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path and not path.startswith(PLANNING_ROOTS) and not path.startswith((".cc-cache/", ".conductor-code/")):
            out.append(path)
    return sorted(out)


MAX_CHAIN = 2  # items in a dependency chain: one item may wait for one other, never a queue


def chain_problems(items: list[dict]) -> list[str]:
    """A round lasts as long as its longest chain: three items waiting one behind another kept run b74d3da7's
    round open for its slowest item plus two more. Any chain of more than ``MAX_CHAIN`` items is a plan problem."""
    by_id = {str(i.get("id")): i for i in items if isinstance(i, dict)}
    memo: dict = {}

    def longest(iid: str, seen: frozenset) -> list[str]:
        if iid in memo:
            return memo[iid]
        best: list[str] = []
        for d in by_id.get(iid, {}).get("dependsOn") or []:
            d = str(d)
            # The core item (the module file and shared types every item compiles against) is not part of a chain:
            # it is small, goes first, and the harness makes every build item wait for it.
            if d in by_id and d not in seen and by_id[d].get("core") is not True:
                path = longest(d, seen | {d})
                if len(path) > len(best):
                    best = path
        memo[iid] = best + [iid]
        return memo[iid]
    out = []
    for iid in by_id:
        path = longest(iid, frozenset({iid}))
        if len(path) > MAX_CHAIN and not any(path[-1] in p and path != p for p in out):
            out.append(" -> ".join(path))
    return [f"items wait in a chain of {len(c.split(' -> '))}: {c}. A round lasts as long as its longest chain: let "
            "each item build against the interfaces in the design instead of waiting, or merge items, so no chain "
            f"has more than {MAX_CHAIN}." for c in sorted(set(out), key=len, reverse=True)[:5]]


def outside_owns(item: dict, files: list[str], work: str = "") -> list[str]:
    """The files a design item reported that it does not own. A path reported absolute, inside the tree, is the
    tree's relative path: an architect that reported its own spec by its full path was rejected for writing outside
    what it owns (run 09a229e8)."""
    mine = owns(item)
    root = os.path.realpath(work) if work else ""

    def rel(f: str) -> str:
        f = str(f).strip()
        if root and os.path.isabs(f):
            full = os.path.realpath(f)
            if full == root or full.startswith(root + os.sep):
                f = os.path.relpath(full, root)
        return os.path.normpath(f).replace(os.sep, "/")
    paths = {rel(f) for f in files or [] if str(f).strip()}
    return sorted(p for p in paths if not _covered(p, mine))


def _files_under(work: str, rel: str) -> list[str]:
    path = os.path.join(work, rel)
    if os.path.isfile(path):
        return [rel]
    out = []
    for base, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in ("node_modules", "__pycache__"))
        out += [os.path.relpath(os.path.join(base, f), work).replace(os.sep, "/") for f in sorted(files)]
    return out


def _protected_files(work: str) -> list[str]:
    out: list[str] = []
    for root in PROTECTED:
        out += _files_under(work, root)
    return sorted(set(out))


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def snapshot(work: str, rnd: int) -> dict:
    """Copy the lead's files as they are when round ``rnd`` starts; returns ``{path: sha}``."""
    folder = os.path.join(work, SNAPSHOTS, f"r{rnd}")
    shutil.rmtree(folder, ignore_errors=True)
    manifest = {}
    for rel in _protected_files(work):
        src = os.path.join(work, rel)
        dst = os.path.join(folder, "files", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        manifest[rel] = _sha(src)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return manifest


def adopt(work: str, paths: list[str]) -> list[str]:
    """Make the lead's own change to its files part of the current round's snapshot, so the round's end keeps it
    instead of putting the old file back (the architect fixing a defective check mid-round). Returns what it took."""
    rounds = sorted((int(d[1:]) for d in os.listdir(os.path.join(work, SNAPSHOTS)) if d[1:].isdigit())
                    if os.path.isdir(os.path.join(work, SNAPSHOTS)) else [])
    if not rounds:
        return []
    folder = os.path.join(work, SNAPSHOTS, f"r{rounds[-1]}")
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return []
    taken = []
    for rel in paths:
        src, dst = os.path.join(work, rel), os.path.join(folder, "files", rel)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            manifest[rel] = _sha(src)
        else:
            manifest.pop(rel, None)
            if os.path.isfile(dst):
                os.remove(dst)
        taken.append(rel)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return taken


def restore(work: str, rnd: int, grants: list[str]) -> list[dict]:
    """Put the lead's files back as they were when round ``rnd`` started, except the paths granted to an
    item of the round (``owns``). Returns what was put back: ``[{path, action}]``, action ``restored``
    (changed or deleted) or ``removed`` (created). Nothing happens without a snapshot of the round."""
    folder = os.path.join(work, SNAPSHOTS, f"r{rnd}")
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return []
    now = set(_protected_files(work))
    out = []
    for rel in sorted(now | set(manifest)):
        if _covered(rel, grants):
            continue
        path = os.path.join(work, rel)
        if rel not in manifest:
            os.remove(path)
            out.append({"path": rel, "action": "removed"})
        elif rel not in now or _sha(path) != manifest[rel]:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(os.path.join(folder, "files", rel), path)
            out.append({"path": rel, "action": "restored"})
    return out
