"""Formal specs: prove the design with TLC, and turn the bounded model into a transition-covering trace
suite the conformance runner replays against the program (docs/FORMAL_SPEC_DESIGN.md).

The architect writes, in ``design/spec/``:

- ``<Module>.tla`` and ``<Module>.cfg`` — the spec and its bounds (CONSTANTS, INIT, NEXT, INVARIANTs);
- ``abstraction.json`` — ``{"module", "config", "state": [variables compared after each step],
  "result": variable holding the last action's observable result, "adapter": the command that runs the
  adapter, "mutants": [{"invariant", "next"}]}`` where each mutant ``next`` is an alternative NEXT operator
  in the spec with a fault that must break ``invariant``.

``prove(workdir)`` model-checks the spec, checks every mutant is caught, dumps the state graph and writes
``.cc/conformance/traces.json``. Results are cached by the hash of ``design/spec/`` so a measurement of
an unchanged spec costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import deque

SPEC_DIR = os.path.join("design", "spec")
OUT_DIR = os.path.join(".cc", "conformance")
TLA_HOME = os.environ.get("CONDUCTOR_TLA_HOME") or os.path.expanduser("~/.cache/conductor-harness/tla")
TLA_JAR = os.path.join(TLA_HOME, "tla2tools.jar")
TLA_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"
MAX_TRANSITIONS = 20000  # beyond this the bounds are too big to replay: the architect is asked to shrink them


# ─────────────────────────────── TLC ───────────────────────────────
def ensure_tlc() -> str:
    """The TLA+ tools jar (the official release), installed once per host in the harness cache."""
    if os.path.isfile(TLA_JAR) and os.path.getsize(TLA_JAR) > 1_000_000:
        return TLA_JAR
    os.makedirs(TLA_HOME, exist_ok=True)
    tmp = TLA_JAR + ".part"
    from common.exec import run
    run(["curl", "-sSfL", "-o", tmp, TLA_URL])
    os.replace(tmp, TLA_JAR)
    return TLA_JAR


def _tlc(spec_dir: str, module: str, cfg_text: str, *, dump: str = "") -> dict:
    """Run TLC on ``module`` with the given config text, in a scratch copy of the spec folder."""
    from common import check_execution
    jar = ensure_tlc()
    with tempfile.TemporaryDirectory(prefix="tlc-") as tmp:
        for name in os.listdir(spec_dir):
            if name.endswith(".tla"):
                shutil.copy(os.path.join(spec_dir, name), tmp)
        with open(os.path.join(tmp, "Run.cfg"), "w", encoding="utf-8") as fh:
            fh.write(cfg_text)
        argv = ["java", "-XX:+UseParallelGC", "-cp", jar, "tlc2.TLC", "-workers", "auto", "-cleanup",
                "-config", "Run.cfg"]
        if dump:
            argv += ["-dump", "dot,actionlabels", os.path.join(tmp, "graph.dot")]
        argv.append(module)
        r = check_execution.execute(argv, cwd=tmp, env=check_execution.inherited_environment(), stall_after_s=300)
        out = (r.stdout or "") + (r.stderr or "")
        graph = ""
        if dump and os.path.isfile(os.path.join(tmp, "graph.dot")):
            with open(os.path.join(tmp, "graph.dot"), encoding="utf-8") as fh:
                graph = fh.read()
    m = re.search(r"(\d+) states generated, (\d+) distinct states found", out)
    return {"exitCode": r.exit_code, "output": out, "graph": graph,
            "violated": _violated(out), "error": _error(out, r.exit_code),
            "generated": int(m.group(1)) if m else 0, "distinct": int(m.group(2)) if m else 0}


def _violated(out: str) -> str:
    m = re.search(r"Error: Invariant (\w+) is violated", out)
    return m.group(1) if m else ""


def _error(out: str, code: int) -> str:
    if code == 0 or _violated(out):
        return ""
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith(("TLC2", "Running", "Parsing", "Semantic", "Starting", "Implied", "Computing", "Checking", "Finished", "The depth", "The average"))]
    return "\n".join(lines[-25:])


def _counterexample(out: str) -> str:
    """TLC's error trace, trimmed for a prompt."""
    i = out.find("Error:")
    return out[i:i + 4000] if i >= 0 else out[-2000:]


# ─────────────────────────────── TLA+ values → JSON ───────────────────────────────
class _Parser:
    """TLA+ values as TLC prints them: strings, numbers, booleans, model values, ``<<…>>`` sequences,
    ``{…}`` sets, ``[k |-> v]`` records/functions, ``(k :> v @@ …)`` functions. A set becomes
    ``{"$set": [...]}`` so the comparison can treat it as unordered."""

    def __init__(self, text: str):
        self.s, self.i = text, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def peek(self, tok: str) -> bool:
        self.ws()
        return self.s.startswith(tok, self.i)

    def eat(self, tok: str):
        self.ws()
        if not self.s.startswith(tok, self.i):
            raise ValueError(f"expected {tok!r} at {self.i}: {self.s[self.i:self.i + 40]!r}")
        self.i += len(tok)

    def value(self):
        self.ws()
        c = self.s[self.i] if self.i < len(self.s) else ""
        if c == '"':
            return self.string()
        if self.s.startswith("<<", self.i):
            return self.seq()
        if c == "{":
            return self.set()
        if c == "[":
            return self.record()
        if c == "(":
            return self.func()
        m = re.compile(r"-?\d+").match(self.s, self.i)
        if m:
            self.i = m.end()
            return int(m.group())
        m = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(self.s, self.i)
        if m:
            self.i = m.end()
            return {"TRUE": True, "FALSE": False}.get(m.group(), m.group())
        raise ValueError(f"unexpected {self.s[self.i:self.i + 40]!r}")

    def string(self):
        self.eat('"')
        out = []
        while self.s[self.i] != '"':
            if self.s[self.i] == "\\":
                self.i += 1
                out.append({"n": "\n", "t": "\t"}.get(self.s[self.i], self.s[self.i]))
            else:
                out.append(self.s[self.i])
            self.i += 1
        self.i += 1
        return "".join(out)

    def items(self, close: str):
        out = []
        if self.peek(close):
            self.eat(close)
            return out
        while True:
            out.append(self.value())
            if self.peek(","):
                self.eat(",")
                continue
            self.eat(close)
            return out

    def seq(self):
        self.eat("<<")
        return self.items(">>")

    def set(self):
        self.eat("{")
        return {"$set": self.items("}")}

    def record(self):
        self.eat("[")
        out = {}
        while True:
            self.ws()
            m = re.compile(r'"?([^"|\s]+)"?\s*\|->').match(self.s, self.i)
            if not m:
                raise ValueError(f"record field expected at {self.s[self.i:self.i + 40]!r}")
            self.i = m.end()
            out[m.group(1)] = self.value()
            if self.peek(","):
                self.eat(",")
                continue
            self.eat("]")
            return out

    def func(self):
        self.eat("(")
        out = {}
        while True:
            k = self.value()
            self.eat(":>")
            out[_key(k)] = self.value()
            if self.peek("@@"):
                self.eat("@@")
                continue
            self.eat(")")
            return out


def _key(k) -> str:
    return k if isinstance(k, str) else json.dumps(k, sort_keys=True)


def parse_value(text: str):
    p = _Parser(text)
    v = p.value()
    p.ws()
    if p.i != len(p.s):
        raise ValueError(f"trailing text {p.s[p.i:p.i + 40]!r}")
    return v


def parse_state(label: str) -> dict:
    """A dot node label (``/\\ x = v\\n/\\ y = w``) as ``{x: v, y: w}``."""
    out = {}
    for part in re.split(r"(?:^|\n)/\\ ", label.replace("\\n", "\n")):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(" = ")
        out[name.strip()] = parse_value(value.strip())
    return out


# ─────────────────────────────── state graph → traces ───────────────────────────────
_NODE = re.compile(r'^(-?\d+) \[label="((?:[^"\\]|\\.)*)"(.*)\];?$')
_EDGE = re.compile(r'^(-?\d+) -> (-?\d+) \[label="((?:[^"\\]|\\.)*)"')


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def parse_graph(dot: str) -> tuple[dict, list, list]:
    """``(states, edges, initial)`` from TLC's ``-dump dot,actionlabels`` output."""
    states, edges, initial = {}, [], []
    for line in dot.splitlines():
        line = line.strip()
        e = _EDGE.match(line)
        if e:
            label = _unescape(e.group(3))
            name, _, rest = label.partition("(")
            args = parse_value("<<" + rest[:-1] + ">>") if rest else []
            edges.append((e.group(1), e.group(2), name.strip(), args))
            continue
        n = _NODE.match(line)
        if n:
            states[n.group(1)] = parse_state(_unescape(n.group(2)))
            if "style = filled" in n.group(3):
                initial.append(n.group(1))
    return states, edges, initial


def traces(states: dict, edges: list, initial: list) -> list[dict]:
    """A transition-covering suite: for every edge, the shortest path to its source, then the edge.
    Each trace is ``{"init": state, "steps": [{"action", "args", "state"}]}``; every transition of the
    bounded model is exercised, and every trace is minimal."""
    out_edges: dict[str, list] = {}
    for src, dst, name, args in edges:
        out_edges.setdefault(src, []).append((dst, name, args))
    parent: dict[str, tuple | None] = {i: None for i in initial}
    q = deque(initial)
    while q:
        s = q.popleft()
        for dst, name, args in out_edges.get(s, []):
            if dst not in parent:
                parent[dst] = (s, name, args)
                q.append(dst)

    def path(s: str) -> tuple[str, list]:
        steps = []
        while parent.get(s) is not None:
            src, name, args = parent[s]
            steps.append({"action": name, "args": args, "state": states[s]})
            s = src
        return s, list(reversed(steps))

    suite, seen = [], set()
    for src, dst, name, args in edges:
        if src not in parent:
            continue
        key = (src, dst, name, json.dumps(args, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        root, steps = path(src)
        suite.append({"init": states[root], "steps": steps + [{"action": name, "args": args, "state": states[dst]}]})
    return suite


# ─────────────────────────────── the proof ───────────────────────────────
def _hash(spec_dir: str) -> str:
    h = hashlib.sha256()
    for name in sorted(os.listdir(spec_dir)):
        p = os.path.join(spec_dir, name)
        if os.path.isfile(p):
            h.update(name.encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def _cfg_without(cfg: str, keyword: str) -> str:
    """The config with every ``keyword`` line (and its indented continuation) removed."""
    out, skip = [], False
    for ln in cfg.splitlines():
        head = ln.strip().split()[0] if ln.strip() else ""
        if head in ("CONSTANTS", "CONSTANT", "INIT", "NEXT", "SPECIFICATION", "INVARIANT", "INVARIANTS",
                    "PROPERTY", "PROPERTIES", "SYMMETRY", "VIEW", "CHECK_DEADLOCK", "ALIAS", "POSTCONDITION"):
            skip = head in (keyword, keyword + "S", keyword.rstrip("S"))
        if not skip:
            out.append(ln)
    return "\n".join(out) + "\n"


def prove(workdir: str) -> dict:
    """Model-check the spec, check its mutants, and write the trace suite. ``{"ok", "summary",
    "feedback", "traces", "transitions", "states", ...}``; ``ok`` is False with ``feedback`` (what to fix,
    with TLC's counterexample) when the spec does not hold, a mutant is not caught, or it is too large."""
    spec_dir = os.path.join(workdir, SPEC_DIR)
    abstraction_path = os.path.join(spec_dir, "abstraction.json")
    if not os.path.isfile(abstraction_path):
        return {"ok": False, "missing": True, "summary": "no formal spec",
                "feedback": f"No formal spec: write {SPEC_DIR}/abstraction.json, the .tla module and its .cfg."}
    out_dir = os.path.join(workdir, OUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    digest = _hash(spec_dir)
    proof_path = os.path.join(out_dir, "proof.json")
    try:
        with open(proof_path, encoding="utf-8") as fh:
            cached = json.load(fh)
        if cached.get("specHash") == digest:
            return {**cached, "cached": True}
    except (OSError, ValueError):
        pass
    try:
        with open(abstraction_path, encoding="utf-8") as fh:
            abstraction = json.load(fh)
        module = str(abstraction["module"])
        with open(os.path.join(spec_dir, str(abstraction.get("config") or f"{module}.cfg")), encoding="utf-8") as fh:
            cfg = fh.read()
    except (OSError, ValueError, KeyError) as e:
        return {"ok": False, "summary": "unreadable spec", "feedback": f"The formal spec could not be read: {e}"}

    result = {"specHash": digest, "module": module}
    main = _tlc(spec_dir, module, cfg, dump="graph")
    result.update(states=main["distinct"], generated=main["generated"])
    if main["error"]:
        return {**result, "ok": False, "summary": "TLC could not check the spec",
                "feedback": f"TLC could not check {module}:\n{main['error']}"}
    if main["violated"]:
        return {**result, "ok": False, "summary": f"invariant {main['violated']} is violated",
                "feedback": f"The spec violates its invariant {main['violated']}. TLC's counterexample:\n"
                            + _counterexample(main["output"])}
    states, edges, initial = parse_graph(main["graph"])
    if len(edges) > MAX_TRANSITIONS:
        return {**result, "ok": False, "summary": f"{len(edges)} transitions: bounds too large",
                "feedback": f"The bounded model has {len(edges)} transitions (limit {MAX_TRANSITIONS}); shrink the "
                            f"constants in the .cfg (fewer ids and values) so every transition can be replayed."}
    # Each invariant must be able to fail: its mutant NEXT must be caught.
    mutants = []
    for m in abstraction.get("mutants") or []:
        inv, nxt = str(m.get("invariant") or ""), str(m.get("next") or "")
        mcfg = _cfg_without(_cfg_without(cfg, "NEXT"), "INVARIANT") + f"NEXT {nxt}\nINVARIANT {inv}\n"
        r = _tlc(spec_dir, module, mcfg)
        mutants.append({"invariant": inv, "next": nxt, "caught": r["violated"] == inv,
                        "error": r["error"][:500]})
    uncaught = [m for m in mutants if not m["caught"]]
    suite = traces(states, edges, initial)
    with open(os.path.join(out_dir, "traces.json"), "w", encoding="utf-8") as fh:
        json.dump({"module": module, "state": abstraction.get("state") or [], "result": abstraction.get("result") or "",
                   "adapter": abstraction.get("adapter") or "", "traces": suite}, fh)
    result.update(transitions=len(edges), traces=len(suite), mutants=mutants,
                  steps=sum(len(t["steps"]) for t in suite))
    invariants = re.findall(r"^\s*INVARIANTS?\s+(.+)$", cfg, re.M)
    if uncaught:
        result.update(ok=False, summary=f"{len(uncaught)} mutant(s) not caught",
                      feedback="These faults should break their invariant but TLC accepts them, so the invariant "
                               "cannot fail (vacuous) or the mutant is not a fault: "
                               + "; ".join(f"{m['next']} vs {m['invariant']}" + (f" ({m['error']})" if m["error"] else "")
                                           for m in uncaught))
    else:
        result.update(ok=True, summary=(f"invariants {', '.join(invariants) or '(none)'} hold in all {main['distinct']} "
                                        f"states; {len(mutants)} mutant(s) caught; {len(suite)} traces cover all "
                                        f"{len(edges)} transitions"), feedback="")
    with open(proof_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    return result
