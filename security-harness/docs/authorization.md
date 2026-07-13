# Authorization & capability levels

!!! warning "Authorized testing only"
    Only scan systems you **own or have explicit written permission to test.** Unauthorized scanning may be illegal.

## `--authorized` vs `--manifest`

Use `--authorized` for quick tests and CI — it synthesizes a minimal, capability-bounded manifest automatically.

For real engagements, use `--manifest <file>` instead. A manifest specifies: approvers, in-scope hosts, testing window, capability ceiling, rate/data-volume budgets, allowed techniques, and forbidden operations / protected records. It is validated at startup and **fails closed**.

## Capability levels

**Capability levels gate every action.** The ceiling comes from the manifest (`--capability <0-4>`, default **1**); every worker refuses an action whose required level exceeds it, and the harness can never raise its own level.

| Level | Permits | HTTP verbs / actions | Mutates the target? |
|---|---|---|---|
| **0** | Passive reading / observation | recon only, no active requests | No |
| **1** *(default)* | Reversible, low-volume active probes | `GET` / `HEAD` / `OPTIONS` | **No — writes & `code_exec` refused at the gate** |
| **2** | State-changing tests with **synthetic data**; product-feature exploitation | `POST` / `PUT` / `PATCH` / `DELETE`, `code_exec` | Creates only its own `sc-pentest-<run>-` objects, ledgered and auto-cleaned |
| **3** | Potentially sensitive / operationally risky proof | as L2, just-in-time approved | As L2 (sensitive scope) |
| **4** | Destructive / availability-impacting / real-data extraction | — | **Prohibited by default; cannot self-escalate** |

The bounded availability / denial-of-wallet tier (load probing) is **off** unless you pass `--resilience`.

## Running without mutating the target

- **Guaranteed read-only:** use `--capability 1` (the default). Writes and `code_exec` are refused by the capability gate in every worker — zero mutations, zero destruction — while you still get the full read-based active surface (GET-param SQLi/XSS/traversal/open-redirect/CORS, recon, crawl, SAST). Trade-off: state-changing classes (e.g. SSRF that requires *creating and running* a workflow) aren't reachable.
- **Capability 2 without touching existing resources:** level 2 operates only on synthetic, prefixed objects it creates and then cleans up; destructive/availability actions are level 4 (off). To turn that convention into a *hard* fence, add `forbidden_operations` / `protected_records` to a `--manifest` — they are enforced on direct HTTP calls **and inside the `code_exec` sandbox**. (Avoid a blanket `DELETE *`, which would also block cleanup of the synthetic objects.)

## Safety governor & audit log

An independent safety governor halts the campaign on window expiry, a `--kill-switch` file, a rate/data-volume budget breach, or a policy breach. A tamper-evident audit log records every action.
