"""code_exec -- the deep-pentest agent's real "hands".

Unlike the single-shot http_request worker, this runs agent-authored Python in an
ephemeral, hardened Docker container so the agent can *operate the product* end to
end: drive the documented multi-step flows (create app -> grant role -> define a
workflow with an HTTP task -> start it -> poll/complete -> observe), capture the
object ids those calls return, and then abuse the resulting state -- exactly how a
human pentester uses an orchestration platform. The agent imports the ``sc`` helper
(sandbox_sc.py) for pre-authed, scope-enforced sessions, OOB canary minting,
resource ledgering, and structured evidence.

Safety model:
  - Code containment: ephemeral ``docker run --rm`` of the sc-codeexec image, as a
    non-root user, ``--cap-drop ALL``, ``--security-opt no-new-privileges``,
    read-only rootfs (+ tmpfs), no host mounts except the per-run workspace volume,
    pids/memory/cpu caps, and a hard wall-clock timeout.
  - Network: the ``sc`` session refuses any out-of-scope host. (A network-level
    egress allow-list via proxy is the planned hardening before aggressive prod runs.)
  - Auditability: created resources are ledgered for cleanup; every object the agent
    makes is name-prefixed ``sc-pentest-<runid>-``; tokens are injected via env and
    never echoed.

The worker NEVER raises -- failures come back in the result so an agent step can't
crash the loop. Idempotent: each call is a fresh container; cross-step state lives
in the mounted workspace volume keyed by (run_id, agent).
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile

from conductor.client.worker.worker_task import worker_task

from common import auditlog
from common import authz
from common import sensitive

from . import egress as egress_mod

log = logging.getLogger(__name__)

IMAGE = os.environ.get("SC_CODEEXEC_IMAGE", "sc-codeexec:latest")
# "jail" (default): run the sandbox on an internal network behind an allow-listing
# egress proxy so it can ONLY reach the target + OOB host. "sc": rely on the
# in-sandbox scope check + container hardening only (no network jail).
EGRESS_MODE = os.environ.get("SC_CODEEXEC_EGRESS", "jail")
DEFAULT_TIMEOUT = 90          # wall-clock seconds per step
HARD_TIMEOUT_CAP = 240
MEM = os.environ.get("SC_CODEEXEC_MEM", "512m")
CPUS = os.environ.get("SC_CODEEXEC_CPUS", "1.0")
PIDS = "256"
OUT_LIMIT = 24000            # cap stdout/stderr fed back to the agent
WORK_ROOT = os.environ.get("SC_CODEEXEC_WORK", os.path.join(tempfile.gettempdir(), "sc-codeexec"))


def _docker() -> str | None:
    return shutil.which("docker")


def _workspace(run_id: str, agent: str) -> str:
    """Per-(run, agent) host dir mounted at /work so state persists across steps."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{run_id}-{agent}")[:80]
    path = os.path.join(WORK_ROOT, safe or "default")
    os.makedirs(path, exist_ok=True)
    # The sandbox runs as uid 65534 (nobody); make the mounted workspace writable
    # by it. Contents are ephemeral, local, per-run scratch (step.py + out.json).
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass
    return path


@worker_task(task_definition_name="code_exec", thread_count=4)
def code_exec(task):
    inp = task.input_data or {}
    code = inp.get("code")
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "no code provided", "stdout": "", "stderr": "",
                "result": {}, "created_resources": []}

    # Capability gate (spec 15.1): operating the product (running agent-authored code
    # that drives multi-step state-changing flows) is a level-2 action -- level-3 when
    # flagged sensitive. The harness cannot raise its own level.
    is_sensitive = bool(inp.get("sensitive"))
    needed = authz.action_capability("", is_code_exec=True, is_sensitive=is_sensitive)
    try:
        capability_max = int(inp.get("capability_max")) if str(inp.get("capability_max") or "").strip() else 1
    except (TypeError, ValueError):
        capability_max = 1
    if needed > capability_max:
        return {"ok": False, "error": "refused: capability",
                "refused_reason": (f"code_exec needs capability level {needed} but the campaign "
                                   f"is authorized to level {capability_max}"),
                "stdout": "", "stderr": "", "result": {}, "created_resources": []}

    target = str(inp.get("target") or "").strip()
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else {}
    manifest = inp.get("manifest") if isinstance(inp.get("manifest"), dict) else {}
    identities = inp.get("identities") if isinstance(inp.get("identities"), dict) else {}
    default_identity = inp.get("identity") or next(
        (k for k, v in identities.items() if isinstance(v, dict) and v.get("value")), "anon")
    oob_base = str(inp.get("oob_base") or "").strip()
    run_id = str(inp.get("run_id") or "run")
    agent = str(inp.get("agent") or "a")
    try:
        timeout = min(int(inp.get("timeout") or DEFAULT_TIMEOUT), HARD_TIMEOUT_CAP)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    docker = _docker()
    if not docker:
        return {"ok": False, "error": "docker not available on the worker host",
                "stdout": "", "stderr": "", "result": {}, "created_resources": []}

    log.info("code_exec: run_id=%s agent=%s egress=%s code_len=%d", run_id, agent[:12], EGRESS_MODE, len(code))
    ws = _workspace(run_id, agent)
    # Fresh per-step script + result file; workspace (state) persists across steps.
    # Prepend the imports so the agent's code can use `sc` (and common stdlib +
    # requests) with no import boilerplate -- the prompt promises they are preloaded.
    # One line, so traceback line numbers stay aligned with the agent's code.
    preamble = "import sc, json, time, requests  # preloaded by the sandbox\n"
    with open(os.path.join(ws, "step.py"), "w") as fh:
        fh.write(preamble + code)
    out_path = os.path.join(ws, "out.json")

    # Feature-op classification rules: from the run's profile (threaded as feature_operations) when
    # present, else the worker's own SC_FEATURE_OPS env. Empty -> the sandbox classifier stays
    # product-neutral (generic product_write). Keeps the ENGINE free of any product's API paths.
    feature_ops = inp.get("feature_operations")
    sc_feature_ops = json.dumps(feature_ops) if isinstance(feature_ops, list) and feature_ops \
        else (os.environ.get("SC_FEATURE_OPS") or "")
    env = {
        "SC_TARGET": target,
        "SC_SCOPE": json.dumps(scope),
        # The authorization manifest: the in-sandbox sc helper enforces its
        # forbidden_operations/protected_records per request (mirrors authz.forbids
        # in the http_request worker, which the sandbox cannot import).
        "SC_MANIFEST": json.dumps(manifest),
        "SC_IDENTITIES": json.dumps(identities),
        "SC_DEFAULT_IDENTITY": default_identity,
        "SC_OOB_BASE": oob_base,
        "SC_RUN_ID": run_id,
        "SC_WORK": "/work",
        "SC_FEATURE_OPS": sc_feature_ops,
    }

    # Network jail: only the target + OOB host are reachable, including for raw
    # ``requests``/socket use in generated code.  Fail closed if the jail cannot be
    # established; an unrestricted bridge fallback would let agent code bypass ``sc``.
    if EGRESS_MODE != "jail":
        return {
            "ok": False,
            "error": "refused: insecure code_exec egress mode",
            "refused_reason": "SC_CODEEXEC_EGRESS must be 'jail'; bridge mode is not permitted",
            "stdout": "", "stderr": "", "result": {}, "created_resources": [],
        }
    jail = egress_mod.ensure_jail(target, oob_base)
    if not jail:
        return {
            "ok": False,
            "error": "refused: egress jail unavailable",
            "refused_reason": "code_exec fails closed when its target/OOB allow-list network cannot be established",
            "stdout": "", "stderr": "", "result": {}, "created_resources": [],
        }
    network = jail["network"]
    env["HTTP_PROXY"] = jail["proxy_url"]
    env["HTTPS_PROXY"] = jail["proxy_url"]
    env["NO_PROXY"] = ""

    env_args = []
    for k, v in env.items():
        env_args += ["-e", f"{k}={v}"]

    cmd = [
        docker, "run", "--rm",
        "--network", network,
        "--user", "65534:65534",                       # nobody
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:size=64m",
        "--pids-limit", PIDS,
        "--memory", MEM, "--memory-swap", MEM,
        "--cpus", CPUS,
        "-v", f"{ws}:/work",
        # Mount the LIVE in-sandbox helper over the image's baked copy (Dockerfile.codeexec
        # COPYs sandbox_sc.py -> /opt/sc/sc.py on PYTHONPATH). Without this, edits to
        # sandbox_sc.py only take effect after an image rebuild -- which is why operation
        # recording silently never reached the container. The :ro bind shadows the baked file.
        "-v", f"{os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sandbox_sc.py')}:/opt/sc/sc.py:ro",
        "-w", "/work",
        *env_args,
        IMAGE,
        # hard inner timeout in case the container ignores SIGTERM
        "timeout", "-s", "KILL", str(timeout), "python", "/work/step.py",
    ]

    # Best-effort: clear stale results so we never read a prior step's output.
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
    except OSError:
        pass

    stdout, stderr, rc, timed_out = "", "", None, False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        stdout, stderr, rc = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    except Exception as exc:  # never let a step crash the loop
        return {"ok": False, "error": f"container launch failed: {exc}",
                "stdout": stdout[:OUT_LIMIT], "stderr": stderr[:OUT_LIMIT],
                "result": {}, "created_resources": []}

    # Read structured output the sc helper wrote.
    result = {}
    try:
        with open(out_path) as fh:
            result = json.load(fh)
    except (OSError, ValueError):
        result = {}

    created = result.get("created") if isinstance(result.get("created"), list) else []

    # Count the sandbox's product interactions toward the campaign-wide request budget
    # (spec 15.2). Best-effort and approximate: the sandbox records one operation per
    # state-changing call it makes; per-request byte volume isn't surfaced from inside
    # the container, so only the request count is accumulated here.
    try:
        ops = result.get("operations") if isinstance(result.get("operations"), list) else []
        if ops:
            from common import budget as budget_mod
            budget_mod.bump(run_id, requests=len(ops))
    except Exception:
        pass

    # Tamper-evident audit record for this code-exec action (best-effort).
    try:
        from urllib.parse import urlparse
        host = (urlparse(target).hostname or "").lower()
        auditlog.append(host, {"action": "code_exec", "target": target, "agent": agent[:24],
                               "ok": (rc == 0) and not timed_out,
                               "created": len(created), "code_len": len(code)})
    except Exception:
        pass

    # Redact any secrets/PII the agent's code printed/recorded before it lands in
    # evidence chains and reports -- the scanner must not persist customer data.
    return {
        "ok": (rc == 0) and not timed_out,
        "exit_code": rc,
        "timed_out": timed_out,
        "stdout": sensitive.redact(stdout, OUT_LIMIT),
        "stderr": sensitive.redact(stderr, OUT_LIMIT),
        "result": result,                       # {created, evidence, findings, oob, operations}
        "created_resources": created,           # fed to the cleanup ledger
    }
