"""Playwright browser workers.

playwright_crawl  – scoped BFS crawl of the target with a real (headless)
                    browser: renders JS, extracts the URL/form/param/endpoint
                    surface, and emits passive DOM findings (password over HTTP,
                    forms missing CSRF tokens, autocomplete on credentials,
                    mixed content). The surface feeds the LLM attack-planner and
                    (in Phase 2) the active-scan fan-out.
playwright_login  – form-based login; returns a Playwright storage_state
                    (cookies + origins) the crawl reuses to explore
                    authenticated areas.

Conductor runs worker functions in a ThreadPoolExecutor, so each task drives
Playwright entirely within one `with sync_playwright()` block in its own thread
(no shared/global browser). Every navigated URL is scope-checked, so the crawl
can never leave the authorized target.
"""

import logging
import os
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse

from conductor.client.worker.worker_task import worker_task
from playwright.sync_api import sync_playwright

from common import scope as scope_mod
from common.auth import auth_headers
from common.findings import HIGH, LOW, MEDIUM, finding

log = logging.getLogger(__name__)

USER_AGENT = "security-conductor/0.1 (+authorized-scan)"
NAV_TIMEOUT = 20000  # ms
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _launch(p):
    return p.chromium.launch(headless=True, args=LAUNCH_ARGS)


def _apply_scoped_auth(router, auth, scope):
    """Attach identity credentials at the REQUEST layer, only for in-scope hosts.

    Setting ``extra_http_headers`` on the browser context/page leaks the credential to
    every subresource the page loads — CDNs, analytics, and any third-party origin — not
    just the target. Routing instead lets us add the auth header to in-scope requests and
    keep it off everything else. ``router`` is a Playwright context or page (both expose
    ``.route``). No-op when there is no credential, to avoid interception overhead."""
    hdrs = auth_headers(auth)
    if not hdrs:
        return
    drop = {k.lower() for k in hdrs}

    def _handler(route):
        req = route.request
        headers = dict(req.headers)  # Playwright lowercases header names
        if scope and scope_mod.in_scope(req.url, scope):
            headers.update(hdrs)
        else:
            for k in list(headers):
                if k.lower() in drop:
                    headers.pop(k)
        route.continue_(headers=headers)

    router.route("**/*", _handler)


# ─────────────────────────────────────────────────────────────────────────────
@worker_task(task_definition_name="playwright_crawl")
def playwright_crawl(task):
    inp = task.input_data or {}
    base_url = str(inp.get("base_url") or "").strip()
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    scope = scope or scope_mod.derive_scope(base_url)
    max_pages = int(inp.get("max_pages") or 25)
    max_depth = int(inp.get("max_depth") or 2)
    storage_state = inp.get("storage_state") if isinstance(inp.get("storage_state"), dict) else None

    scope_mod.enforce(base_url, scope)

    visited: set[str] = set()
    urls: list[str] = []
    forms: list[dict] = []
    params: set[str] = set()
    findings: list[dict] = []
    xhr: list[dict] = []
    queue = deque([(base_url, 0)])

    with sync_playwright() as p:
        browser = _launch(p)
        context = browser.new_context(
            user_agent=USER_AGENT, ignore_https_errors=True,
            storage_state=storage_state,  # None → fresh/anonymous session
        )
        # Credentials go only to in-scope hosts (never leak to third-party subresources).
        _apply_scoped_auth(context, inp.get("auth"), scope)

        def _on_request(req):
            try:
                if req.resource_type in ("xhr", "fetch"):
                    xhr.append({"url": req.url, "method": req.method})
            except Exception:
                pass

        context.on("request", _on_request)
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        while queue and len(visited) < max_pages:
            url, depth = queue.popleft()
            url = urldefrag(url)[0]
            if url in visited or not scope_mod.in_scope(url, scope):
                continue
            visited.add(url)
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                log.debug("crawl goto failed %s: %s", url, exc)
                continue
            urls.append(url)
            insecure = url.startswith("http://")

            _collect_forms(page, url, insecure, forms, params, findings)

            if depth < max_depth:
                _enqueue_links(page, url, scope, visited, queue, depth)

        context.close()
        browser.close()

    endpoints = _dedupe_endpoints(xhr, scope, params)
    surface = {
        "urls": urls,
        "forms": forms,
        "endpoints": endpoints,
        "params": sorted(params),
    }
    meta = {
        "pages_crawled": len(urls),
        "forms_found": len(forms),
        "endpoints_found": len(endpoints),
        "params_found": len(params),
        "authenticated": bool(storage_state),
    }
    return {"surface": surface, "findings": findings, "meta": meta}


def _collect_forms(page, url, insecure, forms, params, findings):
    try:
        form_els = page.query_selector_all("form")
    except Exception:
        return
    for form in form_els:
        try:
            action = form.get_attribute("action") or url
            method = (form.get_attribute("method") or "GET").upper()
            inputs, has_password, has_csrf = [], False, False
            for field in form.query_selector_all("input, select, textarea"):
                nm = field.get_attribute("name") or ""
                typ = (field.get_attribute("type") or "text").lower()
                auto = (field.get_attribute("autocomplete") or "").lower()
                inputs.append({"name": nm, "type": typ})
                if nm:
                    params.add(nm)
                low = nm.lower()
                if any(k in low for k in ("csrf", "token", "authenticity", "nonce")):
                    has_csrf = True
                if typ == "password":
                    has_password = True
                    if insecure:
                        findings.append(finding(
                            title="Password field served over plaintext HTTP",
                            source_tool="playwright_crawl", severity_hint=HIGH,
                            location=url, cwe="CWE-319",
                            owasp="A02:2021 - Cryptographic Failures",
                            evidence=f"<input type=password name={nm!r}> on {url}",
                            description="Credentials entered here are transmitted without TLS.",
                        ))
                    if auto not in ("off", "new-password", "current-password"):
                        findings.append(finding(
                            title="Autocomplete enabled on password field",
                            source_tool="playwright_crawl", severity_hint=LOW,
                            location=url, cwe="CWE-522",
                            owasp="A07:2021 - Identification and Authentication Failures",
                            evidence=f"<input type=password name={nm!r} autocomplete={auto!r}>",
                            description="Sensitive credential fields should disable autocomplete.",
                        ))
            forms.append({"url": url, "action": urljoin(url, action),
                          "method": method, "inputs": inputs})
            if method == "POST" and not has_csrf:
                findings.append(finding(
                    title="Form submits POST without an anti-CSRF token",
                    source_tool="playwright_crawl",
                    severity_hint=MEDIUM if has_password else LOW,
                    location=url, cwe="CWE-352",
                    owasp="A01:2021 - Broken Access Control",
                    evidence=f"POST form action={urljoin(url, action)} has no csrf/token field",
                    description="State-changing forms should include an unpredictable CSRF token.",
                ))
        except Exception:
            continue


def _enqueue_links(page, url, scope, visited, queue, depth):
    try:
        anchors = page.query_selector_all("a[href]")
    except Exception:
        return
    for a in anchors:
        try:
            href = a.get_attribute("href")
        except Exception:
            href = None
        if not href:
            continue
        nxt = urldefrag(urljoin(url, href))[0]
        if nxt not in visited and scope_mod.in_scope(nxt, scope):
            queue.append((nxt, depth + 1))


def _dedupe_endpoints(xhr, scope, params):
    endpoints, seen = [], set()
    for r in xhr:
        base = r["url"].split("?")[0]
        key = (r["method"], base)
        for kv in urlparse(r["url"]).query.split("&"):
            if "=" in kv and kv.split("=")[0]:
                params.add(kv.split("=")[0])
        if key in seen or not scope_mod.in_scope(r["url"], scope):
            continue
        seen.add(key)
        endpoints.append({"url": base, "method": r["method"]})
    return endpoints


# ─────────────────────────────────────────────────────────────────────────────
@worker_task(task_definition_name="playwright_login")
def playwright_login(task):
    inp = task.input_data or {}
    login_url = str(inp.get("login_url") or "").strip()
    username = inp.get("username") or ""
    password = inp.get("password") or ""
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    scope = scope or scope_mod.derive_scope(login_url)
    user_sel = inp.get("username_selector") or ""
    pass_sel = inp.get("password_selector") or ""
    submit_sel = inp.get("submit_selector") or ""

    scope_mod.enforce(login_url, scope)
    result = {"logged_in": False, "storage_state": None, "evidence": ""}

    with sync_playwright() as p:
        browser = _launch(p)
        context = browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        try:
            page.goto(login_url, wait_until="domcontentloaded")
            u = (page.query_selector(user_sel) if user_sel else None) or \
                page.query_selector('input[type="email"]') or \
                page.query_selector('input[name*="user" i]') or \
                page.query_selector('input[name*="email" i]') or \
                page.query_selector('input[type="text"]')
            pw = (page.query_selector(pass_sel) if pass_sel else None) or \
                page.query_selector('input[type="password"]')
            if u and pw:
                u.fill(username)
                pw.fill(password)
                btn = (page.query_selector(submit_sel) if submit_sel else None) or \
                    page.query_selector('button[type="submit"]') or \
                    page.query_selector('input[type="submit"]') or \
                    page.query_selector("button")
                if btn:
                    try:
                        with page.expect_navigation(timeout=NAV_TIMEOUT):
                            btn.click()
                    except Exception:
                        btn.click()
                        page.wait_for_timeout(2500)
                state = context.storage_state()
                result["storage_state"] = state
                result["logged_in"] = bool(state.get("cookies"))
                result["evidence"] = f"final_url={page.url} cookies={len(state.get('cookies', []))}"
            else:
                result["evidence"] = "could not locate username/password fields on the login page"
        except Exception as exc:
            result["evidence"] = f"login error: {exc}"
        finally:
            context.close()
            browser.close()
    return result


# ─────────────────────────────────────────────────────────────────────────────
def _dom_summary(page, scope, limit=22):
    """Compact list of interactive elements the agent can act on next."""
    out, seen = [], set()
    try:
        for a in page.query_selector_all("a[href]"):
            href = a.get_attribute("href")
            if not href:
                continue
            u = urldefrag(urljoin(page.url, href))[0]
            if u in seen or not scope_mod.in_scope(u, scope):
                continue
            seen.add(u)
            out.append({"kind": "link", "text": (a.inner_text() or "").strip()[:60], "url": u})
            if len(out) >= limit:
                break
    except Exception:
        pass
    try:
        for el in page.query_selector_all("input,textarea,select"):
            eid, name = el.get_attribute("id"), el.get_attribute("name")
            sel = f"#{eid}" if eid else (f'[name="{name}"]' if name else None)
            if not sel:
                continue
            out.append({"kind": "input", "selector": sel,
                        "type": (el.get_attribute("type") or "text").lower(),
                        "placeholder": (el.get_attribute("placeholder") or "")[:40]})
    except Exception:
        pass
    try:
        for b in page.query_selector_all("button,[type=submit],[role=button]"):
            text = (b.inner_text() or b.get_attribute("aria-label") or "").strip()[:40]
            bid = b.get_attribute("id")
            sel = f"#{bid}" if bid else (f'button:has-text("{text}")' if text else None)
            if sel:
                out.append({"kind": "button", "text": text, "selector": sel})
    except Exception:
        pass
    return out[:limit + 12]


@worker_task(task_definition_name="playwright_action", thread_count=2)
def playwright_action(task):
    """Execute ONE agent browser action and return the new page state.

    Two modes:
    - Persistent session (env SC_CDP_URL set): connect to a long-lived browser
      over CDP and act on its LIVE page, so in-page/SPA state carries across
      steps. Best for multi-step flows the stateless mode can't preserve.
    - Stateless (default): cookies (storage_state) + URL carry the session
      forward across calls; covers navigation and auth flows.
    """
    inp = task.input_data or {}
    base_url = str(inp.get("base_url") or "").strip()
    url = str(inp.get("url") or base_url).strip()
    scope = inp.get("scope") if isinstance(inp.get("scope"), dict) else None
    scope = scope or scope_mod.derive_scope(base_url or url)
    storage_state = inp.get("storage_state") if isinstance(inp.get("storage_state"), dict) else None
    action = inp.get("action") or {}
    atype = (action.get("type") or "observe").lower()
    cdp = os.environ.get("SC_CDP_URL", "").strip()

    result = {"url": url, "storage_state": storage_state, "title": "", "dom_summary": [], "note": ""}
    # Never raise: a bad agent action must not break the DO_WHILE loop.
    if not scope_mod.in_scope(url, scope):
        result["note"] = "current url out of scope"
        return result
    # Capability gate (spec 15.1): clicking/filling is a state-changing action (level 2);
    # navigate/observe are reads (level 1). The harness cannot exceed its authorized level.
    try:
        capability_max = int(inp.get("capability_max")) if str(inp.get("capability_max") or "").strip() else 1
    except (TypeError, ValueError):
        capability_max = 1
    if atype in ("fill", "click") and capability_max < 2:
        result["note"] = f"refused: capability ({atype} needs level 2, authorized to {capability_max})"
        return result
    auth = inp.get("auth")
    try:
        if cdp:
            return _do_action_cdp(cdp, url, scope, action, atype, result, auth)
        return _do_action(url, scope, storage_state, action, atype, result, auth)
    except Exception as exc:
        log.warning("playwright_action errored, returning prior state: %s", exc)
        result["note"] = f"worker error: {exc}"
        return result


def _perform(page, url, scope, action, atype):
    """Apply one action to a live page; return a status note. Never raises for action errors."""
    note = ""
    if atype == "navigate":
        tgt = urljoin(url, action.get("url") or action.get("value") or "")
        if tgt and scope_mod.in_scope(tgt, scope):
            page.goto(tgt, wait_until="domcontentloaded")
        else:
            note = "navigate target missing/out-of-scope"
    elif atype == "fill" and action.get("selector"):
        page.fill(action["selector"], str(action.get("value") or ""), timeout=6000)
        note = f"filled {action['selector']}"
    elif atype == "click" and action.get("selector"):
        try:
            with page.expect_navigation(timeout=8000):
                page.click(action["selector"], timeout=6000)
        except Exception:
            try:
                page.click(action["selector"], timeout=4000)
            except Exception as e:
                note = f"click failed: {e}"
            page.wait_for_timeout(1200)
    return note


def _capture(page, scope, result, note):
    result["url"] = page.url
    if scope_mod.in_scope(page.url, scope):
        try:
            result["title"] = (page.title() or "")[:120]
        except Exception:
            pass
        result["dom_summary"] = _dom_summary(page, scope)
    else:
        note = (note + " | landed out of scope; not exploring further").strip(" |")
    result["note"] = note
    return result


def _do_action(url, scope, storage_state, action, atype, result, auth=None):
    with sync_playwright() as p:
        browser = _launch(p)
        context = browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True,
                                      storage_state=storage_state)
        # Credentials go only to in-scope hosts (never leak to third-party subresources).
        _apply_scoped_auth(context, auth, scope)
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        note = ""
        try:
            page.goto(url, wait_until="domcontentloaded")
            note = _perform(page, url, scope, action, atype)
        except Exception as exc:
            note = f"action error: {exc}"
        _capture(page, scope, result, note)
        try:
            result["storage_state"] = context.storage_state()
        except Exception:
            result["storage_state"] = storage_state
        context.close()
        browser.close()
    return result


def _do_action_cdp(cdp_url, url, scope, action, atype, result, auth=None):
    """Persistent-session mode: act on the live page of a CDP-connected browser.
    The external browser is left running (we only disconnect)."""
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context(
            user_agent=USER_AGENT, ignore_https_errors=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        try:
            # Credentials go only to in-scope hosts (never leak to third-party subresources).
            _apply_scoped_auth(page, auth, scope)
        except Exception:
            pass
        note = ""
        try:
            if not page.url or page.url == "about:blank":
                page.goto(url, wait_until="domcontentloaded")
            note = _perform(page, page.url, scope, action, atype)
        except Exception as exc:
            note = f"action error: {exc}"
        _capture(page, scope, result, note)
        try:
            result["storage_state"] = context.storage_state()
        except Exception:
            pass
        # disconnect only — do NOT close the long-lived external browser
    return result
