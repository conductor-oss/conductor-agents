"""Conductor workers for OpenSpec-driven development.

The worker keeps bulky OpenSpec context out of Conductor payloads. A validated,
immutable context file is written below ``OPENSPEC_SNAPSHOT_DIR`` and passed to
coding_agent as an internal context file.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from conductor.client.worker.worker_task import worker_task

from campaign.model import paths_overlap, validate_plan
from common import git, github
from common.results import fail, ok

_MAX_DOWNLOAD = 10 * 1024 * 1024
_MAX_EXPANDED = 50 * 1024 * 1024
_MAX_FILES = 1000
_MAX_CONTEXT = 512 * 1024
_CHANGE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def _snapshot_root() -> Path:
    root = Path(os.environ.get("OPENSPEC_SNAPSHOT_DIR", "/tmp/conductor-openspec"))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _openspec_bin() -> str:
    override = os.environ.get("OPENSPEC_BIN")
    if override:
        return override
    return str(Path(__file__).resolve().parent / "node_modules" / ".bin" / "openspec")


def _run(args: list[str], *, cwd: Path, allow_failure: bool = False) -> dict:
    env = dict(os.environ)
    env.update({"OPENSPEC_TELEMETRY": "0", "DO_NOT_TRACK": "1", "NO_COLOR": "1"})
    proc = subprocess.run([_openspec_bin(), *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True)
    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {"text": raw}
    if proc.returncode and not allow_failure:
        raise RuntimeError(f"openspec {' '.join(args)} failed ({proc.returncode}): {raw[:2000]}")
    return {"exitCode": proc.returncode, "data": data, "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:]}


def _source_type(source: str, requested: str, repo_path: str) -> str:
    if requested and requested != "auto":
        if requested not in {"local", "git", "url"}:
            raise ValueError("specSourceType must be auto, local, git, or url")
        return requested
    local = Path(source.removeprefix("file://"))
    if not local.is_absolute():
        local = Path(repo_path) / local
    if local.exists():
        return "local"
    parsed = urllib.parse.urlparse(source)
    lower_path = parsed.path.lower()
    if parsed.scheme in {"http", "https"} and lower_path.endswith((".zip", ".tar.gz", ".tgz")):
        return "url"
    if source.startswith(("git@", "ssh://")) or lower_path.endswith(".git") or \
            (parsed.netloc in {"github.com", "www.github.com"} and "/archive/" not in lower_path):
        return "git"
    raise ValueError("cannot infer spec source type; set specSourceType explicitly")


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _reject_inline_credentials(source: str) -> None:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
        raise ValueError("specSource must not contain inline credentials; use worker git/gh authentication")
    if parsed.scheme in {"http", "https"} and parsed.query:
        keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
        if keys & {"token", "access_token", "key", "api_key", "signature", "x-amz-signature"}:
            raise ValueError("specSource must not contain credential-bearing query parameters")


def _assert_public_https(url: str) -> None:
    _reject_inline_credentials(url)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("direct OpenSpec URLs must use public HTTPS")
    for result in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global:
            raise ValueError(f"URL resolves to a non-public address: {address}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _assert_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, dest: Path) -> None:
    _assert_public_https(url)
    opener = urllib.request.build_opener(_SafeRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": "conductor-openspec/1"})
    total = 0
    with opener.open(req, timeout=30) as response, dest.open("wb") as handle:
        while chunk := response.read(64 * 1024):
            total += len(chunk)
            if total > _MAX_DOWNLOAD:
                raise ValueError("OpenSpec archive exceeds 10 MiB download limit")
            handle.write(chunk)


def _safe_member(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if target != root and not str(target).startswith(str(root) + os.sep):
        raise ValueError(f"unsafe archive path: {name}")
    return target


def _extract(archive: Path, dest: Path) -> None:
    count = size = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                count += 1
                size += info.file_size
                _safe_member(dest, info.filename)
                if count > _MAX_FILES or size > _MAX_EXPANDED:
                    raise ValueError("OpenSpec archive exceeds extraction limits")
                mode = info.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise ValueError("symlinks are not allowed in OpenSpec archives")
            bundle.extractall(dest)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            for info in members:
                count += 1
                size += info.size
                _safe_member(dest, info.name)
                if info.issym() or info.islnk():
                    raise ValueError("links are not allowed in OpenSpec archives")
                if count > _MAX_FILES or size > _MAX_EXPANDED:
                    raise ValueError("OpenSpec archive exceeds extraction limits")
            bundle.extractall(dest, members=members, filter="data")
        return
    raise ValueError("URL must contain a zip, tar.gz, or tgz OpenSpec bundle")


def _locate_project(base: Path, spec_path: str, change_id: str) -> tuple[Path, Path, Path]:
    candidates: list[tuple[Path, Path]] = []
    if spec_path:
        requested = (base / spec_path).resolve()
        candidates.extend([(requested.parent, requested), (requested, requested / "openspec")])
    candidates.extend([(base, base / "openspec"), (base.parent, base)])
    for project, openspec in candidates:
        change = openspec / "changes" / change_id
        if change.is_dir():
            return project.resolve(), openspec.resolve(), change.resolve()
    direct = base.resolve()
    if direct.name == change_id and direct.parent.name == "changes":
        openspec = direct.parent.parent
        return openspec.parent.resolve(), openspec.resolve(), direct
    raise ValueError(f"OpenSpec change {change_id!r} was not found below {base}")


def _locate_openspec(base: Path, spec_path: str) -> tuple[Path, Path]:
    """Find an OpenSpec project even before a URL-sourced change is imported."""
    candidates: list[tuple[Path, Path]] = []
    if spec_path:
        requested = (base / spec_path).resolve()
        candidates.extend([(requested.parent, requested), (requested, requested / "openspec")])
    candidates.extend([(base, base / "openspec"), (base.parent, base)])
    for project, openspec in candidates:
        if openspec.is_dir() and (openspec / "changes").is_dir():
            return project.resolve(), openspec.resolve()
    matches = sorted(p for p in base.rglob("openspec")
                     if p.is_dir() and (p / "changes").is_dir())
    if len(matches) == 1:
        return matches[0].parent.resolve(), matches[0].resolve()
    raise ValueError(f"OpenSpec project was not found below {base}")


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _context_file(change: Path, instructions: dict, provenance: dict, dest: Path) -> Path:
    chunks = ["# OpenSpec execution context\n", "## Provenance\n",
              json.dumps(provenance, indent=2), "\n## Apply instructions\n",
              json.dumps(instructions, indent=2)]
    for path in sorted(p for p in change.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(change).as_posix()
        chunks.extend([f"\n## Artifact: {rel}\n", path.read_text(encoding="utf-8", errors="replace")])
    content = "\n".join(chunks)
    if len(content.encode()) > _MAX_CONTEXT:
        raise ValueError("normalized OpenSpec context exceeds 512 KiB")
    path = dest / "context.md"
    path.write_text(content, encoding="utf-8")
    return path


def _clone(source: str, dest: Path, ref: str | None) -> Path:
    github.ensure_git_auth()
    url = github.clone_url(source)
    out = git.clone(url, str(dest), branch=ref or None, depth=1)
    return Path(out["repoPath"]).resolve()


def _github_origin(repo: Path) -> str:
    origin = git.git(str(repo), "remote", "get-url", "origin", check=False).stdout.strip()
    if "github.com" not in origin:
        raise ValueError("full lifecycle publication requires a GitHub writeback repository")
    _reject_inline_credentials(origin)
    return origin


def _git_root(path: Path) -> Path | None:
    result = git.git(str(path), "rev-parse", "--show-toplevel", check=False)
    return Path(result.stdout.strip()).resolve() if result.code == 0 and result.stdout.strip() else None


def _git_identity(path: Path) -> str:
    root = _git_root(path)
    return git.common_gitdir(str(root)) if root else ""


def _relative_under(root: Path, path: Path, *, label: str, allow_root: bool = False) -> str:
    root = root.resolve()
    path = path.resolve()
    if (path == root and not allow_root) or (path != root and root not in path.parents):
        raise ValueError(f"{label} escapes its repository root")
    return path.relative_to(root).as_posix()


def _copy_regular_tree(source: Path, target: Path) -> None:
    """Replace a declared OpenSpec root inside an owned worktree, never following links."""
    files: list[Path] = []
    size = 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"OpenSpec local source may not contain symlinks: {path}")
        if path.is_file():
            files.append(path)
            size += path.stat().st_size
    if len(files) > _MAX_FILES or size > _MAX_EXPANDED:
        raise ValueError("OpenSpec local source exceeds materialization limits")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path in files:
        dest = target / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def _copy_change(source: Path, target: Path) -> None:
    if target.exists():
        if _digest_tree(source) != _digest_tree(target):
            raise ValueError(f"writeback change already exists with different content: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


@worker_task(task_definition_name="openspec_source_resolve")
def openspec_source_resolve(task):
    """Resolve which checkout owns an OpenSpec run before creating its worktree."""
    i = task.input_data or {}
    try:
        repo_value = str(i.get("repoPath") or "").strip()
        repo_path = str(Path(repo_value).expanduser().resolve()) if repo_value else ""
        source = str(i.get("specSource") or "").strip()
        if not source:
            raise ValueError("specSource is required")
        _reject_inline_credentials(source)
        change_id = str(i.get("changeId") or "")
        if not _CHANGE_RE.fullmatch(change_id):
            raise ValueError("changeId must be lowercase kebab-case")
        requested_type = str(i.get("specSourceType") or "auto")
        use_source_workspace = _bool(i.get("useSpecSourceWorkspace"))
        if use_source_workspace and not repo_path and not Path(source.removeprefix("file://")).is_absolute():
            raise ValueError("useSpecSourceWorkspace requires an absolute specSource when repoPath is blank")
        source_type = _source_type(source, requested_type, repo_path or os.getcwd())
        if not repo_path and not use_source_workspace:
            raise ValueError("repoPath is required unless useSpecSourceWorkspace is true for a local source")
        if use_source_workspace and source_type != "local":
            raise ValueError("useSpecSourceWorkspace is supported only for local specSource paths")

        output = {
            "sourceType": source_type,
            "useSpecSourceWorkspace": use_source_workspace,
            "workspaceRepoPath": repo_path,
            "sourceRepoPath": "",
            "sourceProjectRelativePath": "",
            "sourceOpenSpecRelativePath": "",
            "materializeLocalSource": False,
            "publishOnVerify": False,
            "warning": "",
        }
        if source_type != "local":
            return ok(task, output, [f"[openspec_source_resolve] source={source_type} target={repo_path}"])

        source_root = Path(source.removeprefix("file://"))
        if not source_root.is_absolute():
            source_root = Path(repo_path) / source_root
        source_root = source_root.resolve()
        source_repo = _git_root(source_root)
        if use_source_workspace and not source_repo:
            raise ValueError("useSpecSourceWorkspace requires specSource inside a checked-out Git repository")
        project, openspec_root, _change = _locate_project(
            source_root, str(i.get("specPath") or ""), change_id)
        if not source_repo:
            return ok(task, output, ["[openspec_source_resolve] local source is external to Git target"])

        output.update({
            "sourceRepoPath": str(source_repo),
            "sourceProjectRelativePath": _relative_under(
                source_repo, project, label="OpenSpec project", allow_root=True),
            "sourceOpenSpecRelativePath": _relative_under(source_repo, openspec_root, label="OpenSpec root"),
        })
        target_identity = _git_identity(Path(repo_path)) if repo_path else ""
        source_identity = _git_identity(source_repo)
        if use_source_workspace:
            output.update({
                "workspaceRepoPath": str(source_repo),
                "materializeLocalSource": True,
                "publishOnVerify": True,
            })
            if repo_path and target_identity and target_identity != source_identity:
                output["warning"] = "repoPath is ignored because the local spec source was selected as the workspace"
        elif target_identity and target_identity == source_identity:
            output["materializeLocalSource"] = True
        return ok(task, output, [
            f"[openspec_source_resolve] source={source_type} workspace={output['workspaceRepoPath']}",
            f"[openspec_source_resolve] materialize={output['materializeLocalSource']} publish={output['publishOnVerify']}",
        ])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "openspec_source_resolve", exc)


@worker_task(task_definition_name="openspec_intake")
def openspec_intake(task):
    i = task.input_data or {}
    try:
        repo_path = str(Path(i["repoPath"]).resolve())
        workspace_owned = _bool(i.get("workspaceOwned"))
        resolution = i.get("sourceResolution") if isinstance(i.get("sourceResolution"), dict) else {}
        source = str(i["specSource"])
        _reject_inline_credentials(source)
        change_id = str(i["changeId"])
        if not _CHANGE_RE.fullmatch(change_id):
            raise ValueError("changeId must be lowercase kebab-case")
        source_type = _source_type(source, str(i.get("specSourceType") or "auto"), repo_path)
        run_id = str(i.get("workflowId") or getattr(task, "workflow_instance_id", "")
                     or getattr(task, "task_id", "openspec"))
        root = _snapshot_root() / run_id
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        source_root: Path
        pinned_ref = ""
        if source_type == "local":
            source_root = Path(source.removeprefix("file://"))
            if not source_root.is_absolute():
                source_root = Path(repo_path) / source_root
            source_root = source_root.resolve()
        elif source_type == "git":
            source_root = _clone(source, root / "source", str(i.get("specRef") or "") or None)
            pinned_ref = git.head(str(source_root))
        else:
            archive = root / "source.bundle"
            _download(source, archive)
            source_root = root / "source"
            source_root.mkdir()
            _extract(archive, source_root)
            children = [p for p in source_root.iterdir() if p.is_dir()]
            if len(children) == 1 and not (source_root / "openspec").exists():
                source_root = children[0]
        project, openspec_root, change = _locate_project(
            source_root, str(i.get("specPath") or ""), change_id)
        if not change.is_relative_to(source_root.resolve()):
            raise ValueError("resolved OpenSpec change escapes its source root")
        digest = _digest_tree(change)
        provenance = {"type": source_type, "source": source, "ref": str(i.get("specRef") or ""),
                      "pinnedRef": pinned_ref, "changeId": change_id, "sha256": digest}
        materialized_paths: list[str] = []
        if source_type == "local" and _bool(resolution.get("materializeLocalSource")):
            source_repo = Path(str(resolution.get("sourceRepoPath") or "")).resolve()
            rel_openspec = str(resolution.get("sourceOpenSpecRelativePath") or "")
            rel_project = str(resolution.get("sourceProjectRelativePath") or "")
            if not source_repo.is_dir() or not rel_openspec:
                raise ValueError("local source resolution is missing its repository-relative OpenSpec path")
            expected_source = (source_repo / rel_openspec).resolve()
            if expected_source != openspec_root.resolve():
                raise ValueError("local OpenSpec source changed after source resolution; start a new workflow")
            target_openspec = (Path(repo_path) / rel_openspec).resolve()
            if Path(repo_path).resolve() not in target_openspec.parents:
                raise ValueError("resolved OpenSpec worktree path escapes workspace")
            if target_openspec.exists() and not workspace_owned:
                if _digest_tree(openspec_root) != _digest_tree(target_openspec):
                    raise ValueError("refusing to replace OpenSpec artifacts in an inherited workspace")
            elif target_openspec != openspec_root.resolve():
                _copy_regular_tree(openspec_root, target_openspec)
            project = (Path(repo_path) / rel_project).resolve()
            openspec_root = target_openspec
            change = openspec_root / "changes" / change_id
            if not change.is_dir():
                raise ValueError("materialized OpenSpec change was not found in the run worktree")
            materialized_paths = [rel_openspec]

        validate = _run(["validate", change_id, "--type", "change", "--strict",
                         "--no-interactive", "--json"], cwd=project)
        status = _run(["status", "--change", change_id, "--json"], cwd=project)
        status_data = status["data"] if isinstance(status["data"], dict) else {}
        if status_data.get("isComplete") is not True:
            raise ValueError(f"OpenSpec change is not apply-ready: {status_data}")
        instructions = _run(["instructions", "apply", "--change", change_id, "--json"], cwd=project)
        context = _context_file(change, instructions["data"], provenance, root)

        same_repo = bool(_git_identity(project)) and _git_identity(project) == _git_identity(Path(repo_path))
        writeback_repo = ""
        writeback_project = ""
        if materialized_paths or same_repo:
            writeback_repo = repo_path
            writeback_project = str(project)
        else:
            locator = str(i.get("specWritebackRepo") or "")
            if source_type == "git" and not locator:
                locator = source
            if source_type == "local" and not locator:
                locator = _github_origin(project)
            if not locator:
                raise ValueError("specWritebackRepo is required for URL OpenSpec sources")
            _reject_inline_credentials(locator)
            writeback_ref = str(i.get("specWritebackRef") or "")
            if not writeback_ref and source_type == "git" and locator == source:
                writeback_ref = str(i.get("specRef") or "")
            writeback_repo_path = _clone(locator, root / "writeback", writeback_ref or None)
            _github_origin(writeback_repo_path)
            wb_project, wb_openspec = _locate_openspec(
                writeback_repo_path, str(i.get("specPath") or ""))
            if source_type == "url":
                _copy_change(change, wb_openspec / "changes" / change_id)
            elif not (wb_openspec / "changes" / change_id).is_dir():
                raise ValueError(f"writeback repository does not contain OpenSpec change {change_id!r}")
            writeback_repo = str(writeback_repo_path)
            writeback_project = str(wb_project)
        return ok(task, {
            "valid": True, "changeId": change_id, "contextPath": str(context),
            "sourceProjectPath": str(project), "sourceChangePath": str(change),
            "sourceType": source_type, "provenance": provenance,
            "validation": validate["data"], "status": status["data"],
            "writebackRepoPath": writeback_repo, "writebackProjectPath": writeback_project,
            "sameRepo": same_repo,
            "materializedSourcePaths": materialized_paths,
            "forceAddPaths": [openspec_root.relative_to(Path(writeback_repo)).as_posix()]
                             if writeback_repo and openspec_root.is_relative_to(Path(writeback_repo)) else [],
            "publishOnVerify": _bool(resolution.get("publishOnVerify")) or not same_repo,
        }, [f"[openspec_intake] change={change_id} source={source_type} digest={digest[:12]}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "openspec_intake", exc)


def _parallel_safe(tasks: list[dict]) -> bool:
    if any(item.get("dependsOn") for item in tasks):
        return False
    seen: list[list[str]] = []
    for item in tasks:
        files = item.get("files") or []
        if any(paths_overlap(files, prior) for prior in seen):
            return False
        seen.append(files)
    return True


@worker_task(task_definition_name="openspec_route")
def openspec_route(task):
    i = task.input_data or {}
    try:
        raw = i.get("assessment") or {}
        raw_tasks = raw.get("tasks") if isinstance(raw, dict) else None
        validated = validate_plan({"tasks": raw_tasks or []}, max_tasks=int(i.get("maxTasks") or 25))
        if not validated["valid"]:
            raise ValueError("invalid OpenSpec execution plan: " + "; ".join(validated["errors"]))
        tasks = validated["tasks"]
        raw_by_id = {str(item.get("id") or ""): item for item in (raw_tasks or [])
                     if isinstance(item, dict)}
        for item in tasks:
            refs = raw_by_id.get(item["id"], {}).get("openspecTaskRefs") or []
            if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip()
                                                for ref in refs):
                raise ValueError(f"{item['id']}.openspecTaskRefs must be an array of non-empty strings")
            item["openspecTaskRefs"] = [ref.strip() for ref in refs]
            if item["openspecTaskRefs"]:
                item["description"] += "\nOpenSpec tasks: " + ", ".join(item["openspecTaskRefs"])
        confidence = float(raw.get("confidence") or 0)
        recommended = str(raw.get("recommendedMode") or "campaign")
        risks = [str(x).lower() for x in (raw.get("risks") or [])]
        safe = _parallel_safe(tasks) and len(tasks) <= int(i.get("maxParallelism") or 6)
        complex_risk = any(any(word in risk for word in
                               ("migration", "rollout", "attached", "managed", "cross-cutting"))
                           for risk in risks)
        auto = "parallel" if safe and confidence >= 0.8 and not complex_risk and recommended != "campaign" \
            else "campaign"
        requested = str(i.get("executionMode") or "auto")
        if requested not in {"auto", "parallel", "campaign"}:
            raise ValueError("executionMode must be auto, parallel, or campaign")
        selected = auto if requested == "auto" else requested
        if selected == "parallel" and not safe:
            raise ValueError("forced parallel mode requires dependency-free, file-disjoint tasks within maxParallelism")
        subtasks = []
        for item in tasks:
            criteria = "\n".join(f"- {x}" for x in item.get("acceptanceCriteria") or [])
            checks = item.get("checks") or []
            description = item["description"]
            if criteria:
                description += "\nAcceptance criteria:\n" + criteria
            subtasks.append({"id": item["id"], "description": description,
                             "files": item["files"], "testCmd": " && ".join(checks)})
        instruction = (f"Implement OpenSpec change {i.get('changeId')}. The normalized execution "
                       "plan and immutable OpenSpec context are authoritative. "
                       + str(i.get("supplementalInstruction") or ""))
        return ok(task, {"valid": True, "selectedMode": selected, "recommendedMode": auto,
                         "confidence": confidence, "rationale": raw.get("rationale") or "",
                         "risks": raw.get("risks") or [], "plan": {"tasks": tasks},
                         "parallelPlan": {"subtasks": subtasks}, "instruction": instruction},
                  [f"[openspec_route] selected={selected} recommended={auto} confidence={confidence:.2f}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "openspec_route", exc)


@worker_task(task_definition_name="openspec_verify")
def openspec_verify(task):
    i = task.input_data or {}
    try:
        semantic = i.get("semantic") or {}
        child = i.get("child") or {}
        checks = i.get("checks") or {}
        selected = str(i.get("selectedMode") or "")
        child_ok = child.get("outcome") == "verified" if selected == "campaign" \
            else not bool(child.get("conflicts"))
        passed = bool(semantic.get("passed")) and child_ok and bool(checks.get("blockingPassed", True))
        return ok(task, {"passed": passed, "childPassed": child_ok,
                         "checksPassed": bool(checks.get("blockingPassed", True)),
                         "semantic": semantic, "findings": semantic.get("findings") or []},
                  [f"[openspec_verify] passed={passed} child={child_ok}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "openspec_verify", exc)


def _complete_tasks(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^(\s*[-*]\s+)\[ \]", r"\1[x]", text)
    path.write_text(updated, encoding="utf-8")
    return count


@worker_task(task_definition_name="openspec_finalize")
def openspec_finalize(task):
    i = task.input_data or {}
    try:
        repo = Path(i["writebackRepoPath"]).resolve()
        project = Path(i["writebackProjectPath"]).resolve()
        change_id = str(i["changeId"])
        openspec_root = project / "openspec"
        if not (openspec_root / "changes" / change_id).exists() and project.name == "openspec":
            openspec_root = project
        change = openspec_root / "changes" / change_id
        tasks_file = change / "tasks.md"
        if not tasks_file.exists():
            raise ValueError("apply-ready change has no tasks.md to complete")
        workflow_id = str(getattr(task, "workflow_instance_id", "") or
                          getattr(task, "task_id", "openspec"))
        branch = str(i.get("branch") or f"openspec/archive/{change_id}/{workflow_id[:8]}")
        same_repo = _bool(i.get("sameRepo"))
        publish = _bool(i.get("publish"), not same_repo)
        git.ensure_ready(str(repo))
        if not same_repo:
            git.branch(str(repo), branch)
        completed = _complete_tasks(tasks_file)
        _run(["validate", change_id, "--type", "change", "--strict",
              "--no-interactive", "--json"], cwd=project)
        _run(["archive", change_id, "--yes"], cwd=project)
        commit = git.commit(str(repo), f"openspec: archive {change_id}",
                            force_add_paths=[str(p) for p in (i.get("forceAddPaths") or [])])
        current_branch = git.git(str(repo), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        archives = sorted((openspec_root / "changes" / "archive").glob(f"*-{change_id}"))
        archive_path = archives[-1].relative_to(repo).as_posix() if archives else ""
        return ok(task, {"archived": True, "changeId": change_id,
                         "archivePath": archive_path,
                         "tasksCompleted": completed, "repoPath": str(repo),
                         "branch": branch if not same_repo else current_branch,
                         "commit": commit.get("commit", ""), "publish": publish,
                         "base": str(i.get("base") or "main"),
                         "title": (f"Implement OpenSpec change: {change_id}" if same_repo and publish
                                   else f"Archive OpenSpec change: {change_id}"),
                         "body": (f"Completes and archives OpenSpec change `{change_id}` after "
                                  "verified implementation by Conductor Software Factory.")},
                  [f"[openspec_finalize] change={change_id} tasks={completed} publish={publish}"])
    except Exception as exc:  # noqa: BLE001
        return fail(task, "openspec_finalize", exc)
