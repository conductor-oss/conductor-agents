#!/usr/bin/env python3
"""Interactive SSO session capture — close the SSO gap with a "log in, then hand off" flow.

The harness never automates the IdP login (Google/Okta/SAML) — that's brittle and usually against
the IdP's ToS. Instead this opens a REAL browser you complete the SSO in; while you do, it sniffs
the auth header your app sends on its own authenticated requests (scoped to the target's domain, so
the IdP's own cookies are ignored). When you're done it captures the browser session, distills the
strongest credential (sniffed bearer > localStorage JWT > session cookie) via ``common.session``,
writes ``state/sessions/<label>.json``, and prints the ready ``--id 'label=session:<file>'`` line.

    python workers/sso_capture.py https://app.example.com --label userA
    # (log into Google/Okta in the window, then press Enter here)

Then:  ./assess https://app.example.com --authorized --id 'userA=session:state/sessions/userA.json'

Options: --label NAME (identity label), --scope h1,h2 (extra in-scope hosts to sniff),
--out FILE, --wait-seconds N (non-interactive: capture after N s instead of waiting for Enter).
Env: SC_CAPTURE_HEADLESS=1 forces headless (CI/smoke only — you can't complete SSO headless).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import session as session_mod  # noqa: E402

_AUTH_HEADERS = ("authorization", "x-authorization", "x-api-key", "x-auth-token")


def _apex(host: str) -> str:
    parts = (host or "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _in_target_domain(url: str, target_host: str, extra_hosts: set) -> bool:
    """Only sniff auth headers the APP sends to ITS OWN domain — never the IdP's (so the SSO
    provider's cookies/tokens are not mistaken for the app credential)."""
    h = (urlparse(url).hostname or "").lower()
    if not h:
        return False
    if h == target_host or h in extra_hosts:
        return True
    ap = _apex(target_host)
    return bool(ap) and (h == ap or h.endswith("." + ap))


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="sso_capture", description="Capture an SSO browser session into an --id session file.")
    ap.add_argument("url")
    ap.add_argument("--label", default="user")
    ap.add_argument("--scope", default="", help="extra in-scope hosts (comma-separated) to sniff auth headers from")
    ap.add_argument("--out", default="")
    ap.add_argument("--wait-seconds", type=int, default=0, help="capture after N seconds instead of waiting for Enter")
    args = ap.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("ERROR: Playwright not installed. Run 'make venv' first.", file=sys.stderr)
        return 1

    target_host = (urlparse(args.url).hostname or "").lower()
    extra_hosts = {h.strip().lower() for h in args.scope.split(",") if h.strip()}
    headless = bool(os.environ.get("SC_CAPTURE_HEADLESS"))
    sniffed: dict[str, str] = {}   # header(lower) -> latest value (freshest token wins)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(ignore_https_errors=True)

        def _on_request(req):
            try:
                if not _in_target_domain(req.url, target_host, extra_hosts):
                    return
                hdrs = req.headers  # lowercased keys
                for name in _AUTH_HEADERS:
                    if hdrs.get(name):
                        sniffed[name] = (name, hdrs[name])  # keep original-cased name + value
            except Exception:
                pass

        context.on("request", _on_request)
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"⚠  initial navigation issue (continue logging in anyway): {exc}")

        if args.wait_seconds > 0:
            print(f"ℹ  capturing in {args.wait_seconds}s (non-interactive) …")
            time.sleep(args.wait_seconds)
        else:
            print("\n👉  Complete your SSO login in the browser window (Google/Okta/etc.),")
            print("    navigate so the app makes an authenticated request, then press Enter here to capture.")
            try:
                input()
            except EOFError:
                pass

        storage_state = context.storage_state()
        browser.close()

    sniffed_list = [{"header": v[0], "value": v[1]} for v in sniffed.values()]
    cred = session_mod.pick_credential(sniffed_list, storage_state, target_host)
    if cred.get("kind") == "none":
        print("✗ No credential captured. Did you finish login AND trigger an authenticated request "
              "(reload an in-app page) before capturing? Try again.", file=sys.stderr)
        return 2

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    doc = session_mod.build_session_doc(cred, label=args.label, target=args.url, captured_at=now,
                                        storage_state=storage_state)
    out = args.out or os.path.join(os.path.dirname(HERE), "state", "sessions", f"{args.label}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2)

    print(f"\n✓ captured {cred['kind']} credential for '{args.label}' "
          f"({doc['auth_header']}{'/' + doc['auth_scheme'] if doc['auth_scheme'] else ''}) → {out}")
    print(f"  cookies: {len(storage_state.get('cookies') or [])} | "
          f"use it:\n    --id '{args.label}=session:{out}'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
