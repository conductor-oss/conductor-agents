"""Launch a long-lived headless Chrome exposing the DevTools (CDP) protocol, for
the persistent-session browser agent.

Run it, then point the workers at it:
    workers/.venv/bin/python chrome_server.py            # listens on :9222
    SC_CDP_URL=http://127.0.0.1:9222 WORKER_MODULES=... python main.py

When SC_CDP_URL is set, playwright_action connects to this browser and acts on
its LIVE page, so in-page/SPA state persists across agent steps. Single live
browser → run one agent scan at a time against it.
"""

import sys
import time

from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9222

with sync_playwright() as p:
    p.chromium.launch(headless=True, args=[
        f"--remote-debugging-port={PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--no-sandbox", "--disable-dev-shm-usage",
    ])
    print(f"security-conductor: persistent CDP browser on http://127.0.0.1:{PORT}", flush=True)
    while True:
        time.sleep(3600)
