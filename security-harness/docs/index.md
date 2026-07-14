# Security Harness

An autonomous web-application and API **penetration-testing agent** orchestrated by [Conductor](https://conductor-oss.org/).

Point it at a web app and it crawls with a real browser, reasons about the attack surface with an LLM, runs a battery of scanners in parallel, **actively exploits** what it finds (multi-identity, out-of-band confirmed), triages to cut false positives, and produces a report — all as a durable, observable, retryable Conductor workflow. Optionally point it at the **source code** too, and it mines the code (SAST + route extraction) to find more to test.

!!! warning "Authorized testing only"
    Only scan systems you **own or have explicit written permission to test.** Unauthorized scanning may be illegal. You are responsible for use. See [Authorization & capability levels](authorization.md) for machine-enforceable controls.

## Walkthrough

<div class="dc-video">
  <div class="dc-video__inner">
    <span class="dc-video__play">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
    </span>
    <span class="dc-video__label">Video coming soon · 6-minute walkthrough</span>
  </div>
</div>

## The closed adversarial loop

At its core the harness runs an OODA / scientific-method loop driven by an LLM agent with real tools. Two bookends wrap an iterated core:

```text
UNDERSTAND ─▶ HYPOTHESIZE ─▶ EXPLOIT ─▶ VERIFY ─▶ REFLECT ─▶ REPORT
```

- **Understand** — build a model of the target (surface, docs, dependencies, CVE leads).
- **Hypothesize** — propose falsifiable attacks tied to security objectives and personas.
- **Exploit** — attempt the attack using the app's own features.
- **Verify** — adversarially refute the result; confirm blind bugs out-of-band.
- **Reflect** — decide the next pass from coverage gaps and confirmed-finding chains.
- **Report** — triage, dossier, residual-risk statement.

The loop is multi-pass and goal-directed: a confirmed finding becomes a precondition that seeds deeper, chained hypotheses, so the harness pursues a kill chain rather than a flat list of independent findings. See [Architecture](architecture.md) for the full design.

## Why Conductor

Security scans are long-running, parallel, and failure-prone — exactly the problem Conductor was built to solve. Running the harness as a Conductor workflow makes it:

- **Durable** — long campaigns survive worker restarts and retries.
- **Parallel** — surface gathering and active checks fan out with `FORK_JOIN` / `FORK_JOIN_DYNAMIC`.
- **Observable** — watch every task live in the Conductor UI.

## Next steps

<div class="grid cards" markdown>

- :material-rocket: **[Quickstart](quickstart.md)** — get a scan running in ~30s
- :material-compare: **[Scan vs Assess](scan-vs-assess.md)** — fast surface scan vs deep agentic pentest
- :material-shield-lock: **[Authorization](authorization.md)** — capability levels and the safety governor
- :material-key: **[Authentication & SSO](authentication.md)** — supply credentials, capture SSO sessions
- :material-sitemap: **[Architecture](architecture.md)** — the loop, the catalog spine, the worker layout
- :material-file-document: **[Outputs](outputs.md)** — reports, findings, SARIF, dossier
- :material-robot: **[Agent skill](https://github.com/conductor-oss/conductor-agents/blob/main/security-harness/SKILL.md)** — safe operating instructions for AI assistants

</div>
