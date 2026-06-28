"""RAG worker.

kb_chunks splits the remediation knowledge base (markdown) into per-section
chunks ready for vector indexing. The rag_index workflow fans these out through
LLM_INDEX_TEXT into pgvector; the scan then retrieves the relevant chunks at
triage time (LLM_SEARCH_INDEX). This is what lets the KB grow past prompt limits.

ingest_docs does the same for the target's *own* how-to-use docs (the v2
docs-driven feature): it accepts local paths and/or URLs (markdown / text / HTML
/ PDF / OpenAPI), turns them into chunks for a per-run docs index, and returns a
bounded raw excerpt for the docs_digest LLM. This is how the harness learns how
the app is MEANT to be used (intended workflows + documented invariants) so it
can test whether those guarantees actually hold.
"""

import logging
import os
import re
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from conductor.client.worker.worker_task import worker_task

from common import scope as scope_mod
from common import sitemap as sitemap_mod
from common.auth import auth_headers
from common.net import reachable_url

try:
    import yaml  # optional, for *.yaml OpenAPI specs
except Exception:  # pragma: no cover
    yaml = None
try:
    from bs4 import BeautifulSoup  # HTML -> text
except Exception:  # pragma: no cover
    BeautifulSoup = None
try:
    from pypdf import PdfReader  # PDF -> text (optional dep)
except Exception:  # pragma: no cover
    PdfReader = None
try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

DEFAULT_KB = os.path.join(os.path.dirname(__file__), "..", "..", "knowledge", "owasp-remediation.md")
OWASP_RE = re.compile(r"A\d{2}:\d{4}")

# ── docs ingestion config ────────────────────────────────────────────────────
DOC_EXTS = (".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml",
            ".html", ".htm", ".pdf", ".adoc")
SKIP_DIRS = {".git", "node_modules", "dist", "build", "vendor", "venv", ".venv",
             "__pycache__", "target", ".next", "out", "coverage", "site-packages"}
MAX_DOC_BYTES = 800_000      # per file
MAX_DOCS = 400               # files walked
MAX_CHUNKS = 300             # total chunks indexed
CHUNK_SIZE = 3500            # target chars per chunk
STORE_CAP = 4000             # hard cap on a stored chunk's text
RAW_EXCERPT_TOTAL = 28_000   # total chars handed to the digest LLM
USER_AGENT = "security-conductor/0.1 (+authorized-pentest)"
TIMEOUT = 20
MAX_RENDERED_DOCS = 30


@worker_task(task_definition_name="kb_chunks")
def kb_chunks(task):
    inp = task.input_data or {}
    path = str(inp.get("kb_path") or "").strip() or os.path.abspath(DEFAULT_KB)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {"chunks": [], "meta": {"error": str(exc), "path": path}}

    chunks = []
    # Split on level-2 headings; each section becomes one chunk.
    parts = re.split(r"(?m)^## ", text)
    for part in parts:
        part = part.strip()
        if not part or part.startswith("#"):  # skip the title block
            continue
        heading = part.splitlines()[0].strip()
        body = part.strip()
        owasp = OWASP_RE.search(heading)
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:60] or f"chunk-{len(chunks)}"
        chunks.append({
            "docId": slug,
            "text": body[:4000],
            "metadata": {"section": heading, "owasp": owasp.group(0) if owasp else ""},
        })
    return {"chunks": chunks, "meta": {"count": len(chunks), "path": path}}


# ── docs ingestion ───────────────────────────────────────────────────────────

def _slug(s, n=70):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:n] or "doc"


def _is_url(s):
    return str(s).startswith(("http://", "https://"))


def _openapi_summary(spec):
    """Render an OpenAPI/Swagger dict into readable text the digest can use."""
    if not isinstance(spec, dict) or not (spec.get("paths") or spec.get("openapi") or spec.get("swagger")):
        return None
    info = spec.get("info") or {}
    lines = [f"API spec: {info.get('title', '')} {info.get('version', '')}".strip()]
    if info.get("description"):
        lines.append(str(info["description"])[:1500])
    lines.append("Endpoints:")
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch"):
                continue
            summ = (op.get("summary") or op.get("operationId") or "") if isinstance(op, dict) else ""
            lines.append(f"- {method.upper()} {path} - {summ}".rstrip(" -"))
    return "\n".join(lines)


def _to_text(name, raw, content_type=""):
    """Turn a doc's raw bytes/str into plain text by type. Returns (kind, text)."""
    low = name.lower()
    # OpenAPI / structured specs (json/yaml)
    if low.endswith((".json", ".yaml", ".yml")) or "json" in content_type or "yaml" in content_type:
        text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        spec = None
        try:
            import json as _json
            spec = _json.loads(text)
        except Exception:
            if yaml is not None:
                try:
                    spec = yaml.safe_load(text)
                except Exception:
                    spec = None
        summary = _openapi_summary(spec) if isinstance(spec, dict) else None
        return ("openapi", summary) if summary else ("text", text)
    # PDF
    if low.endswith(".pdf") or "pdf" in content_type:
        if PdfReader is None:
            return ("pdf", "")
        try:
            import io
            reader = PdfReader(io.BytesIO(raw) if isinstance(raw, bytes) else raw)
            return ("pdf", "\n".join((p.extract_text() or "") for p in reader.pages))
        except Exception:
            return ("pdf", "")
    # HTML
    if low.endswith((".html", ".htm")) or "html" in content_type:
        text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        if BeautifulSoup is not None:
            try:
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                return ("html", soup.get_text("\n"))
            except Exception:
                pass
        return ("html", text)
    # markdown / text / rst / adoc
    return ("markdown", raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw)


def _chunk_text(text, source):
    """Split into ~CHUNK_SIZE chunks, preferring markdown headings, else windows."""
    text = (text or "").strip()
    if not text:
        return []
    out = []
    headings = re.split(r"(?m)^#{1,3}\s+", text)
    parts = [p for p in headings if p.strip()] if len(headings) > 2 else [text]
    for part in parts:
        part = part.strip()
        section = part.splitlines()[0].strip()[:80] if part else ""
        # window long sections on paragraph boundaries
        if len(part) <= CHUNK_SIZE:
            buckets = [part]
        else:
            buckets, cur = [], ""
            for para in part.split("\n\n"):
                if len(cur) + len(para) > CHUNK_SIZE and cur:
                    buckets.append(cur)
                    cur = ""
                cur += para + "\n\n"
            if cur.strip():
                buckets.append(cur)
        for b in buckets:
            b = b.strip()
            if b:
                out.append({"section": section, "text": b[:STORE_CAP]})
    return out


def _walk_docs(root):
    files, n = [], 0
    if os.path.isfile(root):
        return [root]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.lower().endswith(DOC_EXTS):
                files.append(os.path.join(dirpath, fn))
                n += 1
                if n >= MAX_DOCS:
                    return files
    return files


def _fetch_url(url, auth, scope):
    headers = {"User-Agent": USER_AGENT}
    # only attach the run's credential when the doc URL is on the target (in scope)
    if scope is None or scope_mod.in_scope(url, scope):
        headers.update(auth_headers(auth))
    r = requests.get(reachable_url(url), headers=headers, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "")


def _render_urls(urls, auth, scope):
    """Render JS documentation pages in one bounded Playwright session."""
    if sync_playwright is None or not urls:
        return {}, ["playwright unavailable for JS-rendered docs"]
    rendered, errors = {}, []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True, user_agent=USER_AGENT)
            for url in list(urls)[:MAX_RENDERED_DOCS]:
                try:
                    page = context.new_page()
                    # Attach credentials only when the docs are on the authorized target.
                    if scope is None or scope_mod.in_scope(url, scope):
                        headers = auth_headers(auth)
                        if headers:
                            page.set_extra_http_headers(headers)
                    page.goto(reachable_url(url), wait_until="networkidle", timeout=TIMEOUT * 1000)
                    rendered[url] = page.locator("body").inner_text(timeout=5000)
                    page.close()
                except Exception as exc:
                    errors.append(f"{url}: render failed: {exc}")
            browser.close()
    except Exception as exc:
        errors.append(f"playwright session failed: {exc}")
    return rendered, errors


def _doc_site_urls(entry, auth, scope):
    """Discover high-value pages from a documentation site's sitemap."""
    parsed = urlsplit(entry)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [
        urljoin(origin, "/sitemap.xml"),
        urljoin(entry.rstrip("/") + "/", "sitemap.xml"),
    ]
    for candidate in dict.fromkeys(candidates):
        try:
            raw, _ = _fetch_url(candidate, auth, scope)
            found = sitemap_mod.security_relevant(raw.decode("utf-8", "ignore"), limit=MAX_RENDERED_DOCS)
            if found:
                return found
        except Exception:
            continue
    return []


@worker_task(task_definition_name="ingest_docs", thread_count=2)
def ingest_docs(task):
    """Ingest how-to-use docs (local paths and/or URLs) into chunks for the
    per-run docs index, plus a bounded raw excerpt for the digest LLM. Never
    raises: per-source errors are recorded and the rest proceed."""
    inp = task.input_data or {}
    docs = inp.get("docs")
    if isinstance(docs, str):
        docs = [docs] if docs.strip() else []
    docs = [d for d in (docs or []) if isinstance(d, str) and d.strip()]
    auth = inp.get("auth") if isinstance(inp.get("auth"), dict) else {}
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    render_js = str(inp.get("render_js", "true")).lower() not in ("false", "0", "no")

    chunks, excerpts, errors, sources = [], [], [], []
    excerpt_budget = RAW_EXCERPT_TOTAL

    def _ingest_one(name, raw, content_type="", forced_kind=""):
        nonlocal excerpt_budget
        kind, text = _to_text(name, raw, content_type)
        if forced_kind:
            kind = forced_kind
        if not (text or "").strip():
            errors.append(f"{name}: empty or unparseable ({kind})")
            return
        sources.append({"source": name, "kind": kind})
        base = _slug(os.path.basename(name.rstrip("/")) or name)
        for i, ch in enumerate(_chunk_text(text, name)):
            if len(chunks) >= MAX_CHUNKS:
                break
            chunks.append({
                "docId": f"{base}-{i}",
                "text": ch["text"],
                "metadata": {"source": name, "section": ch["section"], "kind": kind},
            })
        if excerpt_budget > 0:
            take = text[:min(len(text), excerpt_budget, 6000)]
            excerpts.append(f"### SOURCE: {name} ({kind})\n{take}")
            excerpt_budget -= len(take)

    for entry in docs:
        try:
            if _is_url(entry):
                raw, ctype = _fetch_url(entry, auth, scope)
                _ingest_one(entry, raw, ctype)
                if render_js and ("html" in ctype.lower() or entry.rstrip("/").endswith(("/content", "/docs"))):
                    render_targets = [entry] + _doc_site_urls(entry, auth, scope)
                    rendered, render_errors = _render_urls(list(dict.fromkeys(render_targets)), auth, scope)
                    errors.extend(render_errors)
                    for rendered_url, text in rendered.items():
                        _ingest_one(rendered_url, text, "text/plain", "html-rendered")
            else:
                path = os.path.abspath(os.path.expanduser(entry))
                if not os.path.exists(path):
                    errors.append(f"{entry}: path not found")
                    continue
                for fp in _walk_docs(path):
                    try:
                        if os.path.getsize(fp) > MAX_DOC_BYTES:
                            continue
                        with open(fp, "rb") as fh:
                            _ingest_one(fp, fh.read())
                    except OSError as exc:
                        errors.append(f"{fp}: {exc}")
                    if len(chunks) >= MAX_CHUNKS:
                        break
        except Exception as exc:  # never raise out of the worker
            errors.append(f"{entry}: {exc}")

    return {
        "chunks": chunks,
        "raw_excerpt": "\n\n".join(excerpts)[:RAW_EXCERPT_TOTAL],
        "docs_available": len(chunks) > 0,
        "meta": {
            "chunks": len(chunks),
            "sources": sources,
            "errors": errors,
            "render_js": render_js,
            "rendered_sources": sum(
                1 for source in sources if source.get("kind") == "html-rendered"
            ),
        },
    }
