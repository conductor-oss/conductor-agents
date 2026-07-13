"""Async Conductor REST client + typed models for the TUI.

Read-mostly: search runs, fetch an execution (recursing into sub-workflows), read
worker liveness and task logs; and the three mutations the TUI performs — start,
terminate, retry. Every call is async (httpx); nothing blocks the Textual event loop.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from . import catalog

TERMINAL = {"COMPLETED", "FAILED", "TERMINATED", "TIMED_OUT", "FAILED_WITH_TERMINAL_ERROR"}
CODING_AGENT = "coding_agent"
_WORKER_TASKS = {"coding_agent": "coding_agent", "gitops": "commit"}  # a representative task per module


class ConductorError(RuntimeError):
    """Any non-2xx or transport failure talking to Conductor."""


def _to_ms(v) -> int | None:
    """Parse a Conductor timestamp to epoch-ms. Accepts int/float ms or an ISO-8601
    string (search results use ISO; executions use epoch ms) — be defensive."""
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    try:
        return int(float(s))          # numeric string
    except ValueError:
        pass
    try:
        s = s.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def coerce_map(v) -> dict:
    """Conductor's `/workflow/search` serializes input/output as a Java Map
    `toString()` (e.g. `{repo=acme/app, prNumber=7}`), while `/workflow/{id}` returns
    real JSON. Accept either: dict → as-is; JSON string → parsed; Java-map string →
    best-effort flat parse (enough for scalar fields like repo/prNumber/totalTokens)."""
    if isinstance(v, dict):
        return v
    if not isinstance(v, str) or not v.strip():
        return {}
    s = v.strip()
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        pass
    if not (s.startswith("{") and s.endswith("}")):
        return {}
    body, out, depth, buf = s[1:-1], {}, 0, ""
    entries = []
    for ch in body:                     # split on top-level ", " (respect nesting)
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            entries.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        entries.append(buf)
    for e in entries:
        if "=" not in e:
            continue
        k, val = e.split("=", 1)
        k, val = k.strip(), val.strip()
        if not k:
            continue
        if val.lstrip("-").isdigit():
            out[k] = int(val)
        else:
            try:
                out[k] = float(val)
            except ValueError:
                out[k] = val
    return out


# --------------------------------------------------------------------------- models

@dataclass
class Run:
    id: str
    workflow: str
    status: str
    start_ms: int | None
    end_ms: int | None
    input: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)
    reason: str | None = None

    @property
    def running(self) -> bool:
        return self.status not in TERMINAL

    @property
    def target(self) -> str:
        return catalog.target_for(self.workflow, self.input)

    def duration_ms(self, now_ms: int | None = None) -> int:
        end = self.end_ms or (now_ms or _now_ms())
        start = self.start_ms or end
        return max(0, end - start)


@dataclass
class AgentSnapshot:
    status: str
    num_turns: int
    tokens: int
    cost: float
    turns: list[dict]
    running: bool
    elapsed_s: float | None
    agent: str
    model: str
    denials: list[str]
    file_changes: list[dict] = field(default_factory=list)   # [{"path","status"}]

    @classmethod
    def from_output(cls, o: dict) -> "AgentSnapshot":
        fc = o.get("fileChanges")
        if not fc:  # legacy runs: paths only, unknown status
            fc = [{"path": p, "status": "•"} for p in (o.get("filesChanged") or [])
                  if isinstance(p, str)]
        return cls(
            status=str(o.get("status", "")),
            num_turns=int(o.get("numTurns") or 0),
            tokens=int(o.get("tokenUsed") or 0),
            cost=float(o.get("costUsd") or 0.0),
            turns=list(o.get("turns") or []),
            running=bool(o.get("running", False)),
            elapsed_s=(float(o["elapsedSeconds"]) if o.get("elapsedSeconds") is not None else None),
            agent=str(o.get("agent") or ""),
            model=str(o.get("model") or ""),
            denials=list(o.get("denials") or []),
            file_changes=[c for c in fc if isinstance(c, dict) and c.get("path")],
        )


@dataclass
class TaskNode:
    ref: str
    def_name: str
    type: str
    status: str
    task_id: str
    output: dict
    input: dict = field(default_factory=dict)
    sub_workflow_id: str | None = None
    children: list["TaskNode"] = field(default_factory=list)
    reason: str | None = None

    @property
    def is_coding_agent(self) -> bool:
        return self.def_name == CODING_AGENT

    @property
    def running(self) -> bool:
        return self.status not in TERMINAL and self.status != "CANCELED"

    def snapshot(self) -> AgentSnapshot | None:
        if self.is_coding_agent and self.output:
            return AgentSnapshot.from_output(self.output)
        return None

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class RunDetail:
    run: Run
    tasks: list[TaskNode]

    def all_tasks(self):
        for t in self.tasks:
            yield from t.walk()

    def coding_agents(self) -> list[TaskNode]:
        return [t for t in self.all_tasks() if t.is_coding_agent]

    def workspace(self) -> str | None:
        return workspace_path(self.run, list(self.all_tasks()))

    def file_changes(self) -> list[tuple[str, str]]:
        """Aggregated (status, path) across every coding_agent in the recursed tree —
        the run-level 'what changed' list. A real status (A/M/D/R) wins over the legacy
        '•'. For read-only runs (pr_review) falls back to the reviewed changedFiles."""
        merged: dict[str, str] = {}
        for t in self.all_tasks():
            snap = t.snapshot()
            if not snap:
                continue
            for c in snap.file_changes:
                path, status = str(c["path"]), str(c.get("status") or "•")
                if path.endswith("/"):     # directory noise (e.g. __pycache__/)
                    continue
                if merged.get(path, "•") == "•" or status != "•":
                    merged[path] = status
        if not merged:
            cf = (self.run.output or {}).get("changedFiles")
            if isinstance(cf, list):
                merged = {str(p): "•" for p in cf}
        return sorted(((s, p) for p, s in merged.items()), key=lambda x: x[1])

    def pending_gate(self) -> TaskNode | None:
        """The HITL review gate awaiting a decision, if any: a HUMAN/WAIT task that is
        still open (IN_PROGRESS/SCHEDULED). Its `input.draft` holds what to review."""
        for t in self.all_tasks():
            if t.type in ("HUMAN", "WAIT") and t.status in ("IN_PROGRESS", "SCHEDULED"):
                return t
        return None

    def busiest_running_agent(self) -> TaskNode | None:
        agents = [t for t in self.coding_agents() if t.running]
        if not agents:
            return None
        return max(agents, key=lambda t: (t.snapshot().num_turns if t.snapshot() else 0))

    def tokens_cost(self) -> tuple[int, float]:
        """Aggregate tokens/cost. Prefer the workflow's own terminal totals; else sum
        every coding_agent snapshot across the (recursed) tree — mirrors the recursive
        accounting the workflows report on completion."""
        o = self.run.output or {}
        if not self.run.running:
            tt, tc = o.get("totalTokens"), o.get("totalCostUsd")
            if tt is None:                       # pr_review et al. use tokenUsed/costUsd
                tt, tc = o.get("tokenUsed"), o.get("costUsd")
            if tt is not None:
                return int(tt or 0), float(tc or 0.0)
        tokens = 0
        cost = 0.0
        for t in self.all_tasks():
            snap = t.snapshot()
            if snap:
                tokens += snap.tokens
                cost += snap.cost
        return tokens, cost


@dataclass
class PollState:
    module: str
    alive: bool
    age_s: float | None    # seconds since last poll, None if never/unknown
    workers: int


# --------------------------------------------------------------------------- pure parsers
# (network-free; unit-tested against captured fixtures)

def parse_run(w: dict) -> Run:
    return Run(
        id=w.get("workflowId", ""),
        workflow=w.get("workflowType", w.get("workflowName", "?")),
        status=w.get("status", "?"),
        start_ms=_to_ms(w.get("startTime")),
        end_ms=_to_ms(w.get("endTime")),
        input=coerce_map(w.get("input")),
        output=coerce_map(w.get("output")),
        reason=w.get("reasonForIncompletion"),
    )


def workspace_path(run: "Run", tasks: list["TaskNode"] | None = None) -> str | None:
    """Local filesystem dir where a run's code lives, if resolvable: code_parallel uses
    the input `repoPath`; the GitHub flows surface it in the output or on the `git_clone`
    task. Existence/host is the caller's concern."""
    p = (run.input or {}).get("repoPath") or (run.output or {}).get("repoPath")
    if p:
        return p
    for t in (tasks or []):
        if t.def_name == "git_clone" and (t.output or {}).get("repoPath"):
            return t.output["repoPath"]
    return None


def parse_execution(d: dict) -> tuple[Run, list[TaskNode]]:
    run = parse_run(d)
    if run.workflow == "?":
        run.workflow = d.get("workflowName", "?")
    tasks = [
        TaskNode(
            ref=t.get("referenceTaskName", "?"),
            def_name=t.get("taskDefName", ""),
            type=t.get("taskType", ""),
            status=t.get("status", "?"),
            task_id=t.get("taskId", ""),
            output=t.get("outputData") or {},
            input=t.get("inputData") or {},
            sub_workflow_id=t.get("subWorkflowId"),
            reason=t.get("reasonForIncompletion"),
        )
        for t in (d.get("tasks") or [])
    ]
    return run, tasks


# --------------------------------------------------------------------------- client

class ConductorClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _get(self, path: str, **params):
        try:
            r = await self._client.get(path, params=params or None)
        except httpx.HTTPError as e:
            raise ConductorError(f"GET {path}: {e}") from e
        if r.status_code >= 400:
            raise ConductorError(f"GET {path}: HTTP {r.status_code}")
        return r

    # -- reads ---------------------------------------------------------------
    async def search_runs(self, limit: int = 50) -> list[Run]:
        q = "workflowType IN (" + ",".join(catalog.DASHBOARD_TYPES) + ")"
        r = await self._get("/workflow/search", start=0, size=limit,
                            sort="startTime:DESC", freeText="*", query=q)
        results = (r.json() or {}).get("results") or []
        return [parse_run(w) for w in results]

    async def get_run(self, workflow_id: str, *, recurse: bool = True,
                      only_running: bool = False) -> RunDetail:
        run, tasks = await self._fetch_execution(workflow_id)
        if recurse:
            await self._recurse(tasks, only_running=only_running, depth=0)
        return RunDetail(run=run, tasks=tasks)

    async def _fetch_execution(self, workflow_id: str) -> tuple[Run, list[TaskNode]]:
        r = await self._get(f"/workflow/{workflow_id}", includeTasks="true")
        return parse_execution(r.json() or {})

    async def _recurse(self, tasks: list[TaskNode], *, only_running: bool, depth: int) -> None:
        if depth > 3:  # safety; real max is 2
            return
        subs = [t for t in tasks if t.type == "SUB_WORKFLOW" and t.sub_workflow_id
                and (not only_running or t.running)]
        if not subs:
            return
        # Fetch all sub-workflows for this level concurrently (one round-trip of latency).
        async def load(node: TaskNode):
            try:
                _, child_tasks = await self._fetch_execution(node.sub_workflow_id)  # type: ignore[arg-type]
                node.children = child_tasks
                await self._recurse(child_tasks, only_running=only_running, depth=depth + 1)
            except ConductorError:
                node.children = []
        await asyncio.gather(*(load(n) for n in subs))

    async def task_logs(self, task_id: str) -> list[str]:
        try:
            r = await self._get(f"/tasks/{task_id}/log")
        except ConductorError:
            return []
        return [str(e.get("log", "")) for e in (r.json() or [])]

    async def health(self) -> dict[str, PollState]:
        out: dict[str, PollState] = {}
        now = _now_ms()
        for module, task_type in _WORKER_TASKS.items():
            try:
                r = await self._get("/tasks/queue/polldata", taskType=task_type)
                data = r.json() or []
            except ConductorError:
                data = []
            last = max((_to_ms(p.get("lastPollTime")) or 0 for p in data), default=0)
            age = (now - last) / 1000.0 if last else None
            out[module] = PollState(
                module=module,
                alive=age is not None and age < 15.0,
                age_s=age,
                workers=len(data),
            )
        return out

    async def workflow_registered(self, name: str) -> bool:
        try:
            await self._get(f"/metadata/workflow/{name}")
            return True
        except ConductorError:
            return False

    # -- mutations -----------------------------------------------------------
    async def start(self, name: str, payload: dict) -> str:
        try:
            r = await self._client.post(f"/workflow/{name}", json=payload)
        except httpx.HTTPError as e:
            raise ConductorError(f"start {name}: {e}") from e
        if r.status_code >= 400:
            raise ConductorError(f"start {name}: HTTP {r.status_code} {r.text[:200]}")
        return r.text.strip().strip('"')

    async def terminate(self, workflow_id: str, reason: str = "") -> None:
        try:
            r = await self._client.delete(f"/workflow/{workflow_id}", params={"reason": reason})
        except httpx.HTTPError as e:
            raise ConductorError(f"terminate: {e}") from e
        if r.status_code >= 400:
            raise ConductorError(f"terminate: HTTP {r.status_code}")

    async def signal_task(self, workflow_id: str, task_ref: str, status: str,
                          output: dict | None = None) -> None:
        """Advance a paused HUMAN/WAIT task: POST /tasks/{wid}/{taskRef}/{status} with the
        decision output as the body. status COMPLETED proceeds; FAILED_WITH_TERMINAL_ERROR
        rejects (fails the run). The output map flows to `${taskRef.output.*}` downstream."""
        try:
            r = await self._client.post(
                f"/tasks/{workflow_id}/{task_ref}/{status}", json=output or {})
        except httpx.HTTPError as e:
            raise ConductorError(f"signal {task_ref}: {e}") from e
        if r.status_code >= 400:
            raise ConductorError(f"signal {task_ref}: HTTP {r.status_code} {r.text[:200]}")

    async def retry(self, workflow_id: str) -> None:
        try:
            r = await self._client.post(f"/workflow/{workflow_id}/retry",
                                        params={"resumeSubworkflowTasks": "false"})
        except httpx.HTTPError as e:
            raise ConductorError(f"retry: {e}") from e
        if r.status_code >= 400:
            raise ConductorError(f"retry: HTTP {r.status_code}")
