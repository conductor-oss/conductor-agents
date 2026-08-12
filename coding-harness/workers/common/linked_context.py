"""Safely retrieve small, explicitly linked pieces of PR discussion context.

This module deliberately treats every retrieved byte as untrusted evidence.  It
does not execute JavaScript, follow links found in documents, unpack archives,
or use credentials embedded in a URL.  Individual failures are represented as
warnings so an unavailable CI log cannot block a PR workflow.
"""

from __future__ import annotations

import base64
import html
import ipaddress
import json
import re
import socket
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .exec import run
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_LINKS = 10
MAX_LINK_CHARS = 64 * 1024
MAX_TOTAL_CHARS = 256 * 1024
_URL = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.I)
_SENSITIVE_QUERY = re.compile(r"(?:token|signature|sig|credential|secret|key|password|x-amz-|x-goog-)", re.I)
_TRACKING_QUERY = re.compile(r"^(?:utm_[^=]+|fbclid|gclid)$", re.I)


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def redact_url(url: str) -> str:
    """Return a safe display form, never retaining credentials or signed query data."""
    p = urlsplit(url)
    query = [(k, "[REDACTED]" if _SENSITIVE_QUERY.search(k) else v)
             for k, v in parse_qsl(p.query, keep_blank_values=True)]
    return urlunsplit((p.scheme, p.hostname or "", p.path, urlencode(query), ""))


def extract_urls(bodies: list[str]) -> tuple[list[str], list[str]]:
    """Extract normalized, de-duplicated public candidates from actionable bodies."""
    urls: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for body in bodies:
        for raw in _URL.findall(body or ""):
            raw = raw.rstrip(".,;:!?")
            p = urlsplit(raw)
            if p.scheme.lower() != "https":
                warnings.append(f"Skipped non-HTTPS link: {redact_url(raw)}")
                continue
            if not p.hostname:
                warnings.append("Skipped malformed HTTPS link")
                continue
            if p.username or p.password or any(_SENSITIVE_QUERY.search(k) for k, _ in parse_qsl(p.query, keep_blank_values=True)):
                warnings.append(f"Skipped credential-bearing link: {redact_url(raw)}")
                continue
            query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                     if not _TRACKING_QUERY.match(k)]
            normalized = urlunsplit(("https", p.netloc.lower(), p.path or "/", urlencode(query), ""))
            if normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)
    if len(urls) > MAX_LINKS:
        warnings.append(f"Only the first {MAX_LINKS} linked references were retrieved")
    return urls[:MAX_LINKS], warnings


def _public_host(host: str) -> None:
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"DNS lookup failed: {exc}") from exc
    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global:
            raise ValueError("host does not resolve exclusively to public addresses")


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        p = urlsplit(target)
        if p.scheme != "https" or not p.hostname:
            raise ValueError("redirected to a non-HTTPS URL")
        _public_host(p.hostname)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _http_text(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    _public_host(p.hostname or "")
    request = Request(url, headers={"User-Agent": "conductor-pr-comments/1"})
    try:
        with build_opener(_SafeRedirect).open(request) as response:
            content_type = response.headers.get_content_type().lower()
            raw = response.read(MAX_LINK_CHARS + 1)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    if len(raw) > MAX_LINK_CHARS:
        raw = raw[:MAX_LINK_CHARS]
    if content_type == "application/pdf" or urlsplit(url).path.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            import io
            text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
        except Exception as exc:  # optional parser, failure remains non-blocking
            raise ValueError(f"could not extract PDF text: {exc}") from exc
    else:
        decoded = raw.decode("utf-8", errors="replace")
        if content_type in {"text/html", "application/xhtml+xml"}:
            parser = _TextParser()
            parser.feed(decoded)
            text = html.unescape(" ".join(parser.parts))
        elif content_type in {"application/json", "text/json"}:
            try:
                text = json.dumps(json.loads(decoded), indent=2, ensure_ascii=False)
            except ValueError:
                text = decoded
        elif not (content_type.startswith("text/") or content_type in {"application/xml", "application/javascript"}):
            raise ValueError(f"unsupported content type: {content_type}")
        else:
            text = decoded
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_LINK_CHARS], content_type


def _github_reference(url: str, github_api) -> tuple[str, str] | None:
    p = urlsplit(url)
    if p.hostname not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in p.path.split("/") if part]
    if len(parts) < 2:
        return None
    repo = "/".join(parts[:2])
    # Actions run and job URLs require auth; gh run view supplies metadata + failed logs.
    if len(parts) >= 5 and parts[2:4] == ["actions", "runs"]:
        run_id = parts[4]
        meta = github_api.run_view(repo, run_id)
        jobs = meta.get("jobs") or []
        job_note = ""
        logs = ""
        unavailable: list[str] = []
        if len(parts) >= 7 and parts[5] == "job":
            wanted = parts[6]
            matched = next((j for j in jobs if str(j.get("databaseId") or j.get("id")) == wanted), None)
            job_note = f"\nMatched job: {(matched or {}).get('name', 'not found')}"
            logs = github_api.run_failed_logs(repo, run_id, job_id=wanted)
        failed = [j.get("name", "?") for j in jobs if str(j.get("conclusion", "")).lower() == "failure"]
        if not logs and not job_note:
            for job in jobs:
                if str(job.get("conclusion", "")).lower() != "failure":
                    continue
                job_id = str(job.get("databaseId") or job.get("id") or "").strip()
                if not job_id:
                    unavailable.append(f"{job.get('name', '?')}: missing job id")
                    continue
                try:
                    job_logs = github_api.run_failed_logs(repo, run_id, job_id=job_id)
                    if job_logs.strip():
                        logs += (("\n\n" if logs else "") + job_logs)
                    else:
                        unavailable.append(f"{job.get('name', '?')}: no failed log output")
                except Exception as exc:  # a generated check can have no log archive
                    unavailable.append(f"{job.get('name', '?')}: {str(exc)[:160]}")
        header = f"GitHub Actions run {run_id}: {meta.get('workflowName') or meta.get('displayTitle') or ''}; conclusion={meta.get('conclusion')}"
        if failed:
            header += "\nFailed jobs: " + ", ".join(failed)
        if unavailable:
            header += "\nUnavailable failed logs: " + "; ".join(unavailable)
        # The tail usually contains the compiler/test failure that matters most.
        return header + job_note + "\n\nFailed log tail:\n" + logs[-MAX_LINK_CHARS:], "github-actions"
    if len(parts) >= 4 and parts[2] in {"pull", "issues"} and parts[3].isdigit():
        issue = github_api.api_json(f"repos/{repo}/issues/{parts[3]}")
        return f"GitHub {parts[2][:-1]} #{parts[3]}: {issue.get('title', '')}\n\n{issue.get('body', '')}", "github-issue"
    if len(parts) == 2:
        data = github_api.api_json(f"repos/{repo}/readme")
    elif len(parts) >= 5 and parts[2] in {"blob", "raw", "tree"}:
        # Tree URLs intentionally retrieve only the named file; a directory isn't traversed.
        path = "/".join(parts[4:])
        if not path:
            return None
        data = github_api.api_json(f"repos/{repo}/contents/{path}?ref={parts[3]}")
    else:
        return None
    if data.get("type") != "file" or not data.get("content"):
        raise ValueError("GitHub link did not resolve to a file")
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return content[:MAX_LINK_CHARS], "github-file"


def _git_reference(url: str) -> tuple[str, str] | None:
    """Read only a root README from a public HTTPS Git remote, shallowly."""
    if not urlsplit(url).path.lower().endswith(".git"):
        return None
    p = urlsplit(url)
    _public_host(p.hostname or "")
    with tempfile.TemporaryDirectory(prefix="conductor-linked-git-") as directory:
        target = Path(directory) / "repo"
        result = run(["git", "clone", "--depth", "1", "--no-tags", url, str(target)], check=False)
        if result.code:
            raise ValueError(f"shallow clone failed: {(result.stderr or result.stdout)[:180]}")
        readmes = [p for p in target.iterdir() if p.is_file() and p.name.lower().startswith("readme")]
        if not readmes:
            raise ValueError("Git repository has no root README")
        return readmes[0].read_text(encoding="utf-8", errors="replace")[:MAX_LINK_CHARS], "git-readme"


def _cloud_reference(url: str) -> tuple[str, str] | None:
    """Read cloud objects through ambient SDK credentials, never URL credentials.

    This accepts the standard HTTPS object URL forms that reviewers commonly paste.
    Signed URLs have already been rejected by ``extract_urls``.
    """
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    bits = [part for part in p.path.split("/") if part]
    raw: bytes | None = None
    kind = ""
    if host == "s3.amazonaws.com" and len(bits) >= 2:
        import boto3
        raw = boto3.client("s3").get_object(Bucket=bits[0], Key="/".join(bits[1:]))["Body"].read(MAX_LINK_CHARS + 1)
        kind = "s3"
    elif ".s3." in host and bits:
        import boto3
        bucket = host.split(".s3.", 1)[0]
        raw = boto3.client("s3").get_object(Bucket=bucket, Key="/".join(bits))["Body"].read(MAX_LINK_CHARS + 1)
        kind = "s3"
    elif host == "storage.googleapis.com" and len(bits) >= 2:
        from google.cloud import storage
        raw = storage.Client().bucket(bits[0]).blob("/".join(bits[1:])).download_as_bytes(start=0, end=MAX_LINK_CHARS)
        kind = "gcs"
    elif host.endswith(".blob.core.windows.net") and len(bits) >= 2:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobClient
        account = host.split(".", 1)[0]
        client = BlobClient(account_url=f"https://{account}.blob.core.windows.net", container_name=bits[0],
                            blob_name="/".join(bits[1:]), credential=DefaultAzureCredential())
        raw = client.download_blob(offset=0, length=MAX_LINK_CHARS + 1).readall()
        kind = "azure-blob"
    if raw is None:
        return None
    # Cloud objects are deliberately treated as plain text; binary/archive content isn't parsed.
    return raw[:MAX_LINK_CHARS].decode("utf-8", errors="replace").strip(), kind


def resolve(urls: list[str], github_api) -> tuple[list[dict], list[str], int]:
    """Resolve links independently; failures are warnings and never stop feedback."""
    refs: list[dict] = []
    warnings: list[str] = []
    used = 0
    for url in urls:
        if used >= MAX_TOTAL_CHARS:
            warnings.append("Linked context reached the 256 KiB aggregate limit")
            break
        try:
            resolved = _github_reference(url, github_api)
            if resolved is None:
                resolved = _git_reference(url)
            if resolved is None:
                resolved = _cloud_reference(url)
            text, kind = resolved if resolved is not None else _http_text(url)
            remaining = MAX_TOTAL_CHARS - used
            text = text[:remaining]
            used += len(text)
            refs.append({"url": redact_url(url), "kind": kind, "content": text,
                         "truncated": len(text) >= MAX_LINK_CHARS or len(text) == remaining})
        except Exception as exc:  # every individual link is best effort
            warnings.append(f"Could not retrieve {redact_url(url)}: {str(exc)[:240]}")
    return refs, warnings, used
