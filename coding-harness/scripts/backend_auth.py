#!/usr/bin/env python3
"""Show which credential each agent backend will run with (a subscription login or an API key),
and how to configure the other. Never prints a key's value. ``make auth``."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workers"))
from common import backend_auth  # noqa: E402

rows = backend_auth.report()
for r in rows:
    how = {"login": f"login ({r['source']})", "key": f"API key ({r['source']})", "none": "NOT CONFIGURED"}[r["mode"]]
    print(f"{r['backend']:7} {how}")
    if r["detail"]:
        print(f"        {r['detail']}")
    else:
        other = backend_auth.LOGIN_HOWTO[r["backend"]] if r["mode"] == "key" else backend_auth.KEY_HOWTO[r["backend"]]
        print(f"        to use {'the login' if r['mode'] == 'key' else 'a key'} instead: {other}, then "
              f"{backend_auth.MODE_VARS[r['backend']]}={'login' if r['mode'] == 'key' else 'key'}")
print("\nCLAUDE_AUTH / CODEX_AUTH / GEMINI_AUTH = auto (login when present, else key) | login | key; set them in .env.")
sys.exit(0 if any(r["mode"] != "none" for r in rows) else 1)
