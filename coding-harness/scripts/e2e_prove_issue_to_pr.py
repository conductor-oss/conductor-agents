#!/usr/bin/env python3
"""End-to-end proof harness for the `issue_to_pr` workflow against a real repo.

Creates a GitHub issue, starts `issue_to_pr` on a live Conductor server, polls
it to a terminal state, and -- if a PR was published -- independently proves
the PR actually works by cloning it fresh and running this repository's own
verification engine (`common.verification`) against the PR's head commit.
That last step is deliberately not "trust the workflow's own verdict": it
re-derives and re-runs the test command from scratch, in a disposable clone,
the same way a human reviewer would.

Usage:
    python3 scripts/e2e_prove_issue_to_pr.py \
        --repo conductor-oss/coding-agent-test \
        --title "..." --body-file /path/to/body.md \
        [--design] [--no-openspec-human-approval] [--base main] \
        [--conductor-url http://localhost:8080/api] \
        [--poll-interval 15]

Exit code is 0 only when the workflow completed, a PR was published, and the
independent post-hoc verification passed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_STATES = {"COMPLETED", "FAILED", "TERMINATED", "TIMED_OUT"}


def log(message: str) -> None:
    print(f"[e2e] {message}", flush=True)


def run(*args: str, cwd: str | None = None, check: bool = True) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def create_issue(repo: str, title: str, body: str) -> int:
    url = run("gh", "issue", "create", "--repo", repo, "--title", title, "--body", body)
    log(f"created issue: {url}")
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def api_get(base_url: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base_url}{path}") as response:
        return json.loads(response.read())


def start_workflow(base_url: str, name: str, payload: dict) -> str:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base_url}/workflow/{name}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.read().decode().strip().strip('"')
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"start {name}: HTTP {exc.code} {exc.read().decode()[:500]}") from exc


def poll_workflow(base_url: str, workflow_id: str, *, poll_interval: int) -> dict:
    """Poll until the workflow reaches a terminal state -- no deadline.

    This repository's Conductor Timeout Policy (AGENTS.md) is absolute: no
    execution deadline anywhere, including a one-off proof script. A run that
    is genuinely stuck (a down worker fleet, an exhausted queue) is an
    operational fact to diagnose via queue depth or `verification_health`, not
    something a client-side timeout should paper over by giving up early. The
    operator can always interrupt this loop by hand.
    """
    seen = ""
    while True:
        doc = api_get(base_url, f"/workflow/{workflow_id}?includeTasks=true")
        status = doc.get("status", "")
        tasks = doc.get("tasks") or []
        active = [t.get("referenceTaskName") for t in tasks if t.get("status") in ("IN_PROGRESS", "SCHEDULED")]
        line = f"status={status} tasks={len(tasks)} active={','.join(active) or '-'}"
        if line != seen:
            log(line)
            seen = line
        if status in TERMINAL_STATES:
            return doc
        time.sleep(poll_interval)


def find_task(doc: dict, ref: str) -> dict | None:
    return next((t for t in doc.get("tasks") or [] if t.get("referenceTaskName") == ref), None)


def independent_pr_verification(repo: str, pr_number: int, *, workdir: Path) -> dict:
    """Clone the PR fresh and re-derive + re-run its test command from scratch.

    This intentionally does not read anything the workflow itself computed --
    it is a from-first-principles re-check using the harness's own discovery
    engine, exactly as a human reviewer checking out the branch would.
    """
    pr = json.loads(run("gh", "pr", "view", str(pr_number), "--repo", repo,
                        "--json", "headRefName,headRefOid,isDraft,title,files"))
    clone_dir = workdir / "verify-clone"
    url = run("gh", "repo", "view", repo, "--json", "url", "--jq", ".url")
    run("git", "clone", url, str(clone_dir))
    run("git", "fetch", "origin", pr["headRefName"], cwd=str(clone_dir))
    sha = pr["headRefOid"]
    actual = run("git", "rev-parse", "--verify", f"{sha}^{{commit}}", cwd=str(clone_dir))
    if actual != sha:
        raise RuntimeError(f"PR head {sha} not present after fetch (got {actual})")
    # `fetch` only makes the commit resolvable; the working tree is still
    # whatever the initial clone checked out (the default branch). Discovery
    # reads real files under `root/`, so without this checkout it silently
    # re-derives a command against the wrong branch's tree.
    run("git", "checkout", "--detach", sha, cwd=str(clone_dir))

    sys.path.insert(0, str(ROOT / "workers"))
    from common import verification  # noqa: E402  (path insert must precede this)

    try:
        outcome = verification.verify_candidate(str(clone_dir), sha, scope="repository", allow_heavy=True)
    except verification.VerificationBlocked as exc:
        outcome = {"verificationState": "blocked", "executionOutcome": "configuration_blocked",
                  "reason": str(exc), "commands": []}
    return {"pr": pr, "verification": outcome}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="conductor-oss/coding-agent-test")
    parser.add_argument("--title", default=None)
    parser.add_argument("--body-file", type=Path, default=None)
    parser.add_argument("--base", default="main")
    parser.add_argument("--design", action="store_true", help="run the design phase (off by default)")
    parser.add_argument("--no-openspec-human-approval", action="store_true",
                        help="auto-approve the openspec plan via the LLM judge instead of a human WAIT")
    parser.add_argument("--design-max-iterations", type=int, default=5)
    parser.add_argument("--max-turns", type=int, default=300)
    parser.add_argument("--max-budget-usd", type=float, default=50)
    parser.add_argument("--conductor-url", default="http://localhost:8080/api")
    parser.add_argument("--poll-interval", type=int, default=20)
    parser.add_argument("--issue-number", type=int, default=None,
                        help="reuse an existing issue instead of creating a new one")
    args = parser.parse_args()

    if args.issue_number:
        issue_number = args.issue_number
        log(f"reusing existing issue #{issue_number} (no new issue created)")
    else:
        if not args.title or not args.body_file:
            parser.error("--title and --body-file are required unless --issue-number is given")
        issue_number = create_issue(args.repo, args.title, args.body_file.read_text(encoding="utf-8"))

    payload = {
        "repo": args.repo, "issueNumber": issue_number, "base": args.base,
        "design": args.design, "designHumanApproval": False,
        "designMaxIterations": args.design_max_iterations,
        # openspecHumanApproval defaults true (a human WAIT); the flag flips it
        # to auto (LLM judge).
        "openspecHumanApproval": not args.no_openspec_human_approval,
        "maxTurns": args.max_turns, "maxBudgetUsd": args.max_budget_usd,
    }

    log(f"starting issue_to_pr for {args.repo}#{issue_number} "
        f"(design={args.design}, openspecHumanApproval={payload['openspecHumanApproval']})")
    workflow_id = start_workflow(args.conductor_url, "issue_to_pr", payload)
    log(f"workflow started: {workflow_id}")

    doc = poll_workflow(args.conductor_url, workflow_id, poll_interval=args.poll_interval)
    output = doc.get("output") or {}
    cp_task = find_task(doc, "cp")
    verify_task = find_task(doc, "delivery_verification")

    log("=" * 72)
    log(f"workflow status:      {doc.get('status')}")
    log(f"code_parallel status: {(cp_task or {}).get('status')}")
    log(f"delivery outcome:     {output.get('deliveryOutcome')}")
    log(f"verification state:   {output.get('verificationState')}")
    if verify_task:
        vout = verify_task.get("outputData") or {}
        log(f"test_cycle state:     {vout.get('testCycleState')} (testsPassed={vout.get('testsPassed')})")
    log(f"publication state:    {output.get('publicationState')} ({output.get('publicationReason')})")
    log(f"PR:                   {output.get('prUrl')}")

    pr_url = output.get("prUrl")
    if not pr_url:
        log("RESULT: FAIL -- no PR was published")
        return 1

    pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
    with tempfile.TemporaryDirectory(prefix="e2e-verify-") as tmp:
        proof = independent_pr_verification(args.repo, pr_number, workdir=Path(tmp))

    log("-" * 72)
    log("independent post-hoc verification (fresh clone, harness's own discovery engine):")
    log(f"  PR draft: {proof['pr']['isDraft']}  files changed: {len(proof['pr']['files'])}")
    vstate = proof["verification"]
    log(f"  verificationState: {vstate.get('verificationState')}")
    log(f"  executionOutcome:  {vstate.get('executionOutcome')}")
    for command in vstate.get("commands") or []:
        log(f"  ran: {' '.join(command['argv'])} -> exitCode={command.get('exitCode')} "
            f"outcome={command.get('outcome')}")
    if not vstate.get("commands") and vstate.get("reason"):
        log(f"  reason: {vstate['reason']}")

    passed = vstate.get("verificationState") == "passed"
    log("=" * 72)
    log(f"RESULT: {'PASS' if passed else 'FAIL'} -- {pr_url}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
