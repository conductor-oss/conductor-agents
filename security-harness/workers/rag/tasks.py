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
try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    async_playwright = None

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
MAX_RENDERED_DOCS = 48  # ranked (see common/sitemap.py): budget lands on surface-bearing leaves first


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


# Resource types that carry no documentation text — blocking them cuts JS-doc render time from
# ~130s/page (orkes.io: heavy fonts, images, analytics, websockets that never let networkidle
# settle) to a few seconds. We only need the rendered DOM text.
_BLOCK_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
_BLOCK_URL_HINTS = ("google-analytics", "googletagmanager", "segment.", "hotjar", "intercom",
                    "doubleclick", "facebook", "hubspot", "fullstory", "mixpanel", "sentry",
                    "/gtm.js", "analytics", "cdn.cookie", "optimizely")
_CONTENT_SELECTORS = ("main", "article", "[role='main']", ".markdown", ".theme-doc-markdown")
_RENDER_CONCURRENCY = 6         # pages rendered in parallel (async)
_GOTO_TIMEOUT_MS = 15000
_SELECTOR_TIMEOUT_MS = 5000
_HYDRATE_SETTLE_MS = 600


def _render_urls(urls, auth, scope):
    """Render JS documentation pages concurrently, blocking non-text resources. Falls back to the
    serial sync renderer if the async API is unavailable."""
    urls = list(urls)[:MAX_RENDERED_DOCS]
    if not urls:
        return {}, []
    if async_playwright is not None:
        try:
            import asyncio
            return asyncio.run(_render_urls_async(urls, auth, scope))
        except Exception as exc:  # pragma: no cover - fall back to serial
            serial, errs = _render_urls_serial(urls, auth, scope)
            return serial, errs + [f"async render failed, used serial: {exc}"]
    return _render_urls_serial(urls, auth, scope)


async def _render_urls_async(urls, auth, scope):
    """Concurrent renderer: one browser+context, a bounded pool of pages, heavy resources blocked."""
    rendered, errors = {}, []
    headers = auth_headers(auth) or {}
    sem = __import__("asyncio").Semaphore(_RENDER_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True, user_agent=USER_AGENT)

        async def _block(route):
            req = route.request
            u = req.url.lower()
            if req.resource_type in _BLOCK_RESOURCE_TYPES or any(h in u for h in _BLOCK_URL_HINTS):
                try:
                    await route.abort()
                except Exception:
                    await route.continue_()
            else:
                await route.continue_()
        await context.route("**/*", _block)

        async def _one(url):
            async with sem:
                page = await context.new_page()
                try:
                    if headers and (scope is None or scope_mod.in_scope(url, scope)):
                        await page.set_extra_http_headers(headers)
                    await page.goto(reachable_url(url), wait_until="domcontentloaded",
                                    timeout=_GOTO_TIMEOUT_MS)
                    try:
                        await page.wait_for_selector(", ".join(_CONTENT_SELECTORS),
                                                     timeout=_SELECTOR_TIMEOUT_MS)
                    except Exception:
                        pass
                    await page.wait_for_timeout(_HYDRATE_SETTLE_MS)
                    text = ""
                    for sel in _CONTENT_SELECTORS:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() > 0:
                                text = await loc.inner_text(timeout=_SELECTOR_TIMEOUT_MS)
                                if len((text or "").strip()) > 400:
                                    break
                        except Exception:
                            continue
                    if len((text or "").strip()) <= 400:
                        text = await page.locator("body").inner_text(timeout=_SELECTOR_TIMEOUT_MS)
                    rendered[url] = text
                except Exception as exc:
                    errors.append(f"{url}: render failed: {exc}")
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass

        await __import__("asyncio").gather(*[_one(u) for u in urls])
        await browser.close()
    return rendered, errors


def _render_urls_serial(urls, auth, scope):
    """Serial sync renderer (fallback). Blocks heavy resources; bounded waits."""
    if sync_playwright is None or not urls:
        return {}, ["playwright unavailable for JS-rendered docs"]
    rendered, errors = {}, []
    headers = auth_headers(auth) or {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True, user_agent=USER_AGENT)

            def _block(route):
                req = route.request
                u = req.url.lower()
                if req.resource_type in _BLOCK_RESOURCE_TYPES or any(h in u for h in _BLOCK_URL_HINTS):
                    try:
                        route.abort()
                    except Exception:
                        route.continue_()
                else:
                    route.continue_()
            context.route("**/*", _block)

            for url in urls:
                try:
                    page = context.new_page()
                    if headers and (scope is None or scope_mod.in_scope(url, scope)):
                        page.set_extra_http_headers(headers)
                    page.goto(reachable_url(url), wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
                    try:
                        page.wait_for_selector(", ".join(_CONTENT_SELECTORS), timeout=_SELECTOR_TIMEOUT_MS)
                    except Exception:
                        pass
                    page.wait_for_timeout(_HYDRATE_SETTLE_MS)
                    text = ""
                    for sel in _CONTENT_SELECTORS:
                        try:
                            loc = page.locator(sel).first
                            if loc.count() > 0:
                                text = loc.inner_text(timeout=_SELECTOR_TIMEOUT_MS)
                                if len((text or "").strip()) > 400:
                                    break
                        except Exception:
                            continue
                    if len((text or "").strip()) <= 400:
                        text = page.locator("body").inner_text(timeout=_SELECTOR_TIMEOUT_MS)
                    rendered[url] = text
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
    # Try sitemap.xml at the entry path AND every parent path up to the origin. A docs site often
    # hosts its sitemap at the docs root (e.g. /content/sitemap.xml), not the domain root — the old
    # code only tried / and the exact entry, so it missed the real docs sitemap entirely.
    candidates = [urljoin(entry.rstrip("/") + "/", "sitemap.xml")]
    segs = [s for s in parsed.path.split("/") if s]
    for i in range(len(segs), 0, -1):
        candidates.append(f"{origin}/{'/'.join(segs[:i])}/sitemap.xml")
    candidates.append(urljoin(origin, "/sitemap.xml"))
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

    # Persist the chunks to a per-run docs store so the LEXICAL `docs_search` worker can serve live
    # per-agent lookups WITHOUT a vector DB (pgvector/embeddings are optional, not required). Keyed by
    # run_id (= the campaign correlation_id the agents also carry). Best-effort: never fatal.
    run_id = str(inp.get("run_id") or "").strip()
    if run_id and chunks:
        try:
            _save_docs_store(run_id, chunks)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"docs-store persist: {exc}")

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


# ---------------------------------------------------------------------------
# Lexical docs retrieval (no vector DB required) -- backs the agents' live
# `search_docs` action so the swarm can look up "how is this feature meant to
# be used" mid-run even when pgvector/embeddings are not configured.
# ---------------------------------------------------------------------------
import json as _json
import math as _math

try:
    from common import memory as _memory  # state_dir()
except Exception:  # noqa: BLE001
    _memory = None

_WORD_RE = re.compile(r"[a-z0-9_]+")


def _docs_store_path(run_id: str) -> str:
    base = _memory.state_dir() if _memory else os.path.join(os.path.dirname(__file__), "..", "state")
    d = os.path.join(base, "docs")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(run_id))[:120]
    return os.path.join(d, f"{safe}.json")


def _save_docs_store(run_id: str, chunks: list) -> None:
    slim = [{"docId": c.get("docId"), "text": c.get("text", ""),
             "source": (c.get("metadata") or {}).get("source", ""),
             "section": (c.get("metadata") or {}).get("section", "")}
            for c in (chunks or []) if (c.get("text") or "").strip()]
    with open(_docs_store_path(run_id), "w") as fh:
        _json.dump(slim, fh)


def _tokens(s: str) -> list:
    return _WORD_RE.findall((s or "").lower())


def _bm25_rank(chunks: list, query: str, k: int) -> list:
    """Tiny BM25 over the run's doc chunks. No deps, deterministic. Returns top-k chunks."""
    q = [t for t in _tokens(query) if len(t) > 2]
    if not q or not chunks:
        return []
    N = len(chunks)
    docs_tok = [_tokens(c.get("text", "")) for c in chunks]
    avgdl = (sum(len(d) for d in docs_tok) / N) or 1.0
    df = {}
    for t in set(q):
        df[t] = sum(1 for d in docs_tok if t in d) or 0
    k1, b = 1.5, 0.75
    scored = []
    for c, d in zip(chunks, docs_tok):
        if not d:
            continue
        dl = len(d)
        score = 0.0
        for t in q:
            n = df.get(t, 0)
            if n == 0:
                continue
            idf = _math.log(1 + (N - n + 0.5) / (n + 0.5))
            tf = d.count(t)
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:k]]


@worker_task(task_definition_name="docs_search", thread_count=4)
def docs_search(task):
    """Lexical (BM25) retrieval over the per-run ingested doc chunks — the no-vector-DB backing for
    the agents' `search_docs` action. Returns the top matching documentation snippets for a query so
    an agent can learn how a feature is MEANT to be used mid-exploit. Never raises."""
    inp = task.input_data or {}
    run_id = str(inp.get("run_id") or inp.get("correlation_id") or "").strip()
    query = str(inp.get("query") or "").strip()
    k = int(inp.get("maxResults") or inp.get("k") or 4)
    if not run_id or not query:
        return {"available": False, "results": [], "digest": "", "query": query}
    try:
        path = _docs_store_path(run_id)
        if not os.path.exists(path):
            return {"available": False, "results": [], "digest": "", "query": query,
                    "note": "no docs store for this run (docs not ingested)"}
        with open(path) as fh:
            chunks = _json.load(fh)
        top = _bm25_rank(chunks, query, k)
        results = [{"source": c.get("source", ""), "section": c.get("section", ""),
                    "text": (c.get("text") or "")[:1200]} for c in top]
        digest = "\n\n".join(
            f"[docs: {r['source']}{(' #' + r['section']) if r['section'] else ''}]\n{r['text']}"
            for r in results)
        # `result` mirrors `results` so consumers reading `${docs_search.output.result}` (the shape the
        # old vector task produced) iterate the hits directly.
        return {"available": bool(results), "result": results, "results": results, "digest": digest,
                "query": query, "hits": len(results)}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "result": [], "results": [], "digest": "", "query": query, "error": str(exc)}


@worker_task(task_definition_name="enrich_docs_digest", thread_count=2)
def enrich_docs_digest(task):
    """Backfill the LLM docs digest with DETERMINISTIC doc-surface extraction so the SYSTEM's
    documented security model is always available downstream — not just when the LLM happened to
    populate it. Run 05b95dd9's digest returned 0 `documented_invariants` and recipes for only the
    core workflow; doc-surface finds the invariants (audit admin-only, tag inheritance, masking…)
    and every documented endpoint (incl. the /api/a2a and /api/audit surface). Merges, never
    overwrites non-empty LLM fields. Feeds both the feature-complete sweep (features.build_inventory
    reads `surface_endpoints`) and the hypothesizer. Never raises."""
    inp = task.input_data or {}
    digest = inp.get("digest")
    if isinstance(digest, str):
        try:
            digest = _json.loads(digest)
        except Exception:
            digest = {}
    digest = digest if isinstance(digest, dict) else {}
    run_id = str(inp.get("run_id") or "").strip()
    try:
        from common import doc_surface as _ds
        surface = _ds.surface_for_run(run_id) if run_id else {"endpoints": [], "invariants": [], "families": [], "seeds": []}
    except Exception as exc:  # noqa: BLE001
        return {**digest, "_enrich_error": str(exc)}

    out = dict(digest)
    # 1) surface_endpoints: always expose the deterministic endpoint list for the feature sweep.
    out["surface_endpoints"] = [{"method": e.get("method", ""), "path": e.get("path", "")}
                                for e in (surface.get("endpoints") or [])]
    # 2) documented_invariants: backfill ONLY when the LLM returned nothing usable.
    existing_inv = digest.get("documented_invariants") or digest.get("invariants") or []
    if not existing_inv:
        out["documented_invariants"] = [
            {"invariant": iv.get("text", ""), "where": iv.get("source", ""),
             "how_to_test": "attempt the action as a lower-privileged / different-tenant identity and "
                            "confirm the documented rule is not enforced",
             "kinds": iv.get("kinds", [])}
            for iv in (surface.get("invariants") or [])]
    # 3) doc_surface_seeds + families: carry the grounded probes for the hypothesizer/report.
    out["doc_surface_seeds"] = surface.get("seeds") or []
    out["doc_surface_families"] = surface.get("families") or []
    out["_enriched"] = {"endpoints": len(out["surface_endpoints"]),
                        "invariants_backfilled": 0 if existing_inv else len(out.get("documented_invariants") or []),
                        "seeds": len(out["doc_surface_seeds"])}
    return out
