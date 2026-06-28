#!/usr/bin/env python3
"""Remediation re-test (ROADMAP E9): re-run a prior dossier's confirmed-finding PoCs and
report which are FIXED vs STILL VULNERABLE. Standalone (no Conductor server needed).

    python retest.py reports/<scan-id>/dossier.json \
        --id 'key:<K>,secret:<S>,tokenurl:<U>'   # or  --id 'token:<JWT>'
        [--profile conductor]

Reads the dossier's `regression` bundle, re-issues each `poc_request` with the supplied
credential (auth scheme auto-detected like the harness), and scores reproduction.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "workers"))

import requests  # noqa: E402
import urllib3  # noqa: E402

from common import auth as auth_mod  # noqa: E402
from common import profiles as profiles_mod  # noqa: E402
from common import regression as regression_mod  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _parse_id(spec: str, profile: dict) -> dict:
    out = {}
    for kv in spec.split(","):
        k, _, v = kv.partition(":")
        out[{"key": "auth_key", "secret": "auth_secret", "tokenurl": "token_url",
             "token": "auth_token", "header": "auth_header", "scheme": "auth_scheme"}.get(k, k)] = v
    te = (profile.get("auth") or {}).get("token_exchange")
    if te:
        out["token_exchange"] = te
    if (profile.get("auth") or {}).get("header") and not out.get("auth_header"):
        out["auth_header"] = profile["auth"]["header"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dossier")
    ap.add_argument("--id", required=True, help="credential spec (key:..,secret:..,tokenurl:.. or token:..)")
    ap.add_argument("--profile", default="")
    args = ap.parse_args()

    with open(args.dossier) as fh:
        dossier = json.load(fh)
    items = dossier.get("regression") or regression_mod.bundle(dossier.get("confirmed_findings") or [])
    if not items:
        print("no regression items (no confirmed findings with a PoC) in this dossier.")
        return 0

    profile = profiles_mod.load(args.profile)
    inp = _parse_id(args.id, profile)
    base = ""
    for it in items:
        u = (it.get("poc_request") or {}).get("url") or ""
        if "://" in u:
            base = "/".join(u.split("/")[:3]); break
    auth = auth_mod.resolve_auth(inp, base_url=base)
    headers = auth_mod.auth_headers(auth)
    print(f"re-testing {len(items)} finding(s) against {base}  (auth header: {auth.get('header','none')})")

    replays = {}
    for it in items:
        poc = it["poc_request"]
        try:
            r = requests.request(poc.get("method", "GET"), poc["url"], headers=headers,
                                 json=poc.get("json"), timeout=20, verify=False, allow_redirects=False)
            # PoC "reproduces" (still vulnerable) if it still succeeds (not denied/missing).
            reproduced = r.status_code < 400 and r.status_code not in (401, 403, 404)
            replays[it["id"]] = {"reproduced": reproduced, "status": r.status_code}
        except requests.RequestException as exc:
            replays[it["id"]] = {"reproduced": False, "error": str(exc)}

    score = regression_mod.score_retest(items, replays)
    for res in score["results"]:
        mark = {"fixed": "FIXED", "still_vulnerable": "STILL VULNERABLE", "unknown": "?"}[res["verdict"]]
        print(f"  [{mark}] {res['title']}  (status {replays.get(res['id'],{}).get('status','?')})")
    print("\n" + score["summary"])
    return 1 if score["still_vulnerable"] else 0


if __name__ == "__main__":
    sys.exit(main())
