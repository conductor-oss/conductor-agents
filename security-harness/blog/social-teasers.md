# Launch teasers — Security Harness

Short-form promos for the launch post *"We built an AI pentester that assumes it's wrong until proven right."*
Same guardrails as the post: no named production target, no live repro. Swap `<LINK>` for the published URL.

---

## LinkedIn (~150 words)

Point an LLM at a web app and ask it to find bugs, and it will happily invent a few — a `200 OK` mistaken for an exploit, a masked value read as a "secret leak."

The hard part of AI security isn't *finding* candidate vulnerabilities. It's *believing* them.

So we built **Security Harness** — an open-source autonomous web & API pentester — around the opposite posture: every finding is assumed wrong until it's proven. Blind bugs have to trigger an out-of-band callback. Cross-tenant claims need a second identity. And in a real run against a production target we own, it confirmed an SSRF *and threw out two of its own draft findings* that didn't survive scrutiny.

It runs as a durable, observable workflow on Conductor — the part that lets a multi-hour, massively parallel pentest survive the real world.

Read how it earns trust 👇
<LINK>

#AppSec #AISecurity #DevSecOps #OpenSource #Conductor

---

## X / Twitter

**Single post:**

Most AI security tools hallucinate findings. We built one that refutes its own.

Security Harness — an open-source autonomous pentester that assumes every bug is fake until proven. Blind bugs must call back out-of-band. Built on @conductor-oss 👇

<LINK>

**Thread (optional):**

1/ AI + security has a trust problem. Point an LLM at an app and it'll confidently report SQLi, IDOR, "leaked secrets" — some real, many plausible fiction. Generating candidate bugs is easy. Believing them is the hard part.

2/ So Security Harness (open source) starts from disbelief. A separate verifier tries to *refute* every finding and defaults to REJECTED under uncertainty. A finding survives only if the evidence forces it to.

3/ Blind bugs (SSRF, RCE) can't be faked into existence: they're confirmed only when the target calls back to an out-of-band listener we control. No callback → it stays an explicit *unconfirmed lead*, never a "finding."

4/ In a real run against a production target we own, it confirmed an SSRF via an IPv6-loopback egress bypass — proven with a block-vs-reach differential — and rated it High, not Critical, because the juicy endpoints were still auth'd. Then it deleted 2 of its own draft findings.

5/ It's a durable @conductor-oss workflow: multi-hour runs survive crashed workers, fan-out matches however many hypotheses the LLM plans, every step is replayable. Clone it, run it against something you own: <LINK>

---

## Hacker News

**Title:**

Show HN: Security Harness – an open-source AI pentester that refutes its own findings

**First comment (author context):**

Author here. The thing we kept hitting with LLM-driven security tooling is that generating plausible vulnerabilities is trivial and believing them is where it all falls apart — you end up with a confident report full of `200 OK`s dressed up as exploits.

So this is built inside-out around verification rather than generation:

- A separate verifier step tries to *refute* each candidate and defaults to rejected under uncertainty; ties don't survive a skeptic vote.
- Blind classes (SSRF/RCE/blind injection) are only marked confirmed on an out-of-band callback to a listener we control, or separate unambiguous in-band proof — otherwise they stay explicit unconfirmed leads.
- Cross-tenant/BOLA testing structurally refuses to report "clean" with a single identity; confirmation requires reading *another* tenant's distinctive data, not a self-read.
- It's capability-gated (levels 0–4, default read-only, can't self-escalate) behind an authorization manifest that fails closed, with a hash-chained audit log.

We run it against our own production and it's surfaced real, confirmed bugs (e.g. an SSRF via an IPv6-loopback egress-filter bypass, proven with a block-vs-reach differential). The part I'm actually proud of is that the same runs *reject their own weak findings* and report coverage honestly ("N of 31 objectives tested — absence of findings is not assurance").

It runs as a durable Conductor workflow, which is what makes the long, parallel, retry-heavy runs survivable and fully replayable. Repo, docs, and a one-command local demo against OWASP Juice Shop: <LINK>

Happy to answer questions on the verification model, the OOB confirmation, or the capability/authorization design.
