#!/usr/bin/env python3
"""Nightly breadth-of-language real-e2e sweep.

Runs one ``run_real_e2e.py start-campaign`` scenario per supported language
(python, java, csharp, go, typescript, typescript-react, rust, cpp) against the
real test repository, with bounded concurrency, and writes a summary report
under ``reports/real-e2e/nightly/<run-id>/summary.json``.

This is a real, side-effecting run: each language creates a real GitHub issue,
a real branch, and (on success) a real pull request in the configured test
repo, and spends real coding-agent budget. It is meant to be scheduled (cron,
CI nightly job), not run casually:

    scripts/run_nightly_e2e.py --strict

A language whose toolchain isn't installed on this machine is skipped (not
failed) unless ``--strict`` is given, in which case a missing toolchain is
also a hard failure -- the intended mode for a CI runner that is supposed to
have every toolchain provisioned.

Exit code is 0 only if every attempted language passed (and, under --strict,
none were skipped for a missing toolchain).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUN_REAL_E2E = ROOT / "scripts" / "run_real_e2e.py"

# language -> binaries that must resolve on PATH for run_real_e2e.py's chosen
# scenario to have any chance of building/testing. This is a cheap preflight,
# not a substitute for the real verification pipeline (workers/common/verification.py),
# which does its own strict runtime probing inside the disposable check worktree.
TOOLCHAINS: dict[str, tuple[str, ...]] = {
    "python": ("python3",),
    "java": ("mvn", "java"),
    "csharp": ("dotnet",),
    "go": ("go",),
    "typescript": ("node", "npm"),
    "typescript-react": ("node", "npm"),
    "rust": ("cargo",),
    "cpp": ("cmake", "ctest"),
}
_CPP_COMPILERS = ("c++", "g++", "clang++")


def missing_toolchain(language: str) -> list[str]:
    missing = [binary for binary in TOOLCHAINS[language] if shutil.which(binary) is None]
    if language == "cpp" and not any(shutil.which(compiler) for compiler in _CPP_COMPILERS):
        missing.append("|".join(_CPP_COMPILERS))
    return missing


def _last_json_object(text: str) -> dict[str, Any] | None:
    """run_real_e2e.py prints one JSON object per progress event, then a final
    pretty-printed summary object. Take the last top-level JSON value in the
    stream rather than assuming line-delimited output, since the summary is
    itself multi-line."""
    decoder = json.JSONDecoder()
    pos = 0
    last: dict[str, Any] | None = None
    text = text.strip()
    while pos < len(text):
        start = text.find("{", pos)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(value, dict):
            last = value
        pos = end
    return last


def run_language(language: str, args: argparse.Namespace) -> dict[str, Any]:
    missing = missing_toolchain(language)
    if missing:
        return {"language": language, "status": "toolchain_missing", "missing": missing}

    started = datetime.now(timezone.utc)
    cmd = [
        sys.executable, str(RUN_REAL_E2E), "start-campaign",
        "--language", language,
        "--repo", args.repo,
        "--server", args.server,
        "--reports", args.reports,
        "--checkout-root", args.checkout_root,
        "--max-budget", str(args.max_budget),
        "--max-turns", str(args.max_turns),
        "--max-parallelism", str(args.max_parallelism),
        "--max-tasks", str(args.max_tasks),
        "--max-waves", str(args.max_waves),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    result: dict[str, Any] = {
        "language": language,
        "status": "passed" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "durationSeconds": round(duration, 1),
        "stdoutTail": proc.stdout[-4000:],
        "stderrTail": proc.stderr[-4000:],
    }
    summary = _last_json_object(proc.stdout)
    if summary is not None:
        result["summary"] = summary

    if result["status"] == "passed" and args.with_review and summary and summary.get("stateFile"):
        scenario_dir = str(Path(summary["stateFile"]).parent)
        review_cmd = [sys.executable, str(RUN_REAL_E2E), "start-review", scenario_dir,
                      "--server", args.server]
        review_proc = subprocess.run(review_cmd, capture_output=True, text=True)
        result["review"] = {
            "status": "passed" if review_proc.returncode == 0 else "failed",
            "returncode": review_proc.returncode,
            "stdoutTail": review_proc.stdout[-4000:],
            "stderrTail": review_proc.stderr[-4000:],
        }
    return result


def parser() -> argparse.ArgumentParser:
    languages = sorted(TOOLCHAINS)
    value = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    value.add_argument("--languages", nargs="+", default=languages, choices=languages,
                        help="subset of languages to run (default: all)")
    value.add_argument("--concurrency", type=int, default=3,
                        help="max campaigns running at once (default: 3, to bound coding-agent load and GitHub API rate)")
    value.add_argument("--strict", action="store_true",
                        help="treat a missing toolchain as a failure instead of a skip (use on a fully-provisioned CI runner)")
    value.add_argument("--with-review", action="store_true",
                        help="after a successful campaign, also run start-review against its PR")
    value.add_argument("--repo", default=os.environ.get("E2E_REPO", "conductor-oss/coding-agent-test"))
    value.add_argument("--server", default=os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080/api"))
    value.add_argument("--reports", default=str(ROOT / "reports" / "real-e2e"))
    value.add_argument("--checkout-root", default="/tmp/coding-harness-real-e2e")
    value.add_argument("--max-budget", type=float, default=50)
    value.add_argument("--max-turns", type=int, default=500)
    value.add_argument("--max-parallelism", type=int, default=4)
    value.add_argument("--max-tasks", type=int, default=12)
    value.add_argument("--max-waves", type=int, default=10)
    return value


def main() -> int:
    args = parser().parse_args()
    run_id = datetime.now(timezone.utc).strftime("nightly-%Y%m%d-%H%M%S")
    out_dir = Path(args.reports) / "nightly" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(run_language, language, args): language for language in args.languages}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({"language": result["language"], "status": result["status"]}, sort_keys=True), flush=True)

    results.sort(key=lambda r: r["language"])
    summary = {
        "runId": run_id,
        "repo": args.repo,
        "languagesRequested": sorted(args.languages),
        "results": results,
        "passed": [r["language"] for r in results if r["status"] == "passed"],
        "failed": [r["language"] for r in results if r["status"] == "failed"],
        "skipped": [r["language"] for r in results if r["status"] == "toolchain_missing"],
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"\nsummary written to {summary_path}", file=sys.stderr)

    hard_failures = list(summary["failed"])
    if args.strict:
        hard_failures += summary["skipped"]
    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
