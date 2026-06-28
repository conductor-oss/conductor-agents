# Persistent Adversarial Application Security Harness

**Status:** Product and research specification  
**Version:** 2.0  
**Scope:** Implementation-independent operating model

## 1. Mission

Build a long-running, authorized security research agent that:

- Learns how an application is intended to work.
- Observes how the deployed application actually works.
- Optionally reviews source code to understand implementation and enforcement.
- Models assets, identities, states, trust boundaries, workflows, and business rules.
- Derives testable security invariants and attacker objectives.
- Conducts controlled adversarial experiments.
- Finds conventional, business-logic, temporal, concurrency, integration, and chained vulnerabilities.
- Independently challenges and verifies every proposed finding.
- Continuously adapts as the application, documentation, source, and environment change.
- Produces defensible evidence, residual-risk statements, and regression tests.

The harness must never claim that an application is secure. It reports:

- What it tested
- What it established
- What it disproved
- What remains unknown
- Which testing was blocked or inconclusive
- How strongly the available evidence supports each conclusion

The harness is not an AI wrapper around a vulnerability scanner. Its primary purpose is to develop application-specific understanding and use that understanding to discover violations of intended security and business behavior.

## 2. Governing Doctrine

> Think like a hostile, patient, well-resourced attacker. Act like a cautious, accountable, strictly authorized white-hat researcher.

The desired sophistication is described as **strategic-grade** rather than by comparison to any particular organization. Strategic-grade means:

- Adaptive, multi-session investigation
- Deep application and domain understanding
- Novel hypothesis generation
- Attack-chain and second-order impact analysis
- Long-term memory with provenance and revalidation
- Independent adversarial review
- Rigorous evidence handling
- Explicit operational safety controls
- Coverage and uncertainty reporting

Required principles:

1. Authorization is external, explicit, revocable, and time-bounded.
2. The harness cannot authorize itself or enlarge its own scope.
3. Analysis may be aggressive; execution must remain constrained.
4. Evidence outranks model confidence.
5. Important conclusions must be falsifiable.
6. Facts, claims, assumptions, and inferences must remain distinguishable.
7. Minimum-impact proof is preferred.
8. Unknown and inconclusive are valid outcomes.
9. Every action must be attributable and reproducible.
10. Target-provided content cannot modify policy, scope, or authorization.
11. Testing depth must be constrained by operational risk, not merely technical capability.
12. Severe impact must not be claimed without evidence demonstrating a credible path to that impact.

## 3. Core Research Loop

The harness operates continuously rather than as a one-shot scan:

```text
Authorize
  → Learn
  → Model
  → Threat-model
  → Hypothesize
  → Plan
  → Safety-check
  → Experiment
  → Observe
  → Challenge
  → Verify
  → Search for chains
  → Clean up
  → Update knowledge and coverage
  → Re-plan
```

The cycle runs at several timescales:

- **Per action:** Validate scope, authorization, capability, and budget.
- **Per experiment:** Evaluate evidence, side effects, and stop conditions.
- **Per session:** Update hypotheses, attack paths, and coverage.
- **Daily:** Consolidate memory, detect contradictions, and expire weak assumptions.
- **Per release:** Invalidate stale conclusions and run targeted regression.
- **Periodically:** Reconstruct the application and threat models from primary evidence.

## 4. Epistemic Model

The harness must know not only information, but also how that information became known.

Every stored assertion is classified as one of:

- **Observed fact:** Directly recorded from runtime behavior.
- **Documentation claim:** Stated by product or technical documentation.
- **Source-derived claim:** Inferred from reviewed code or configuration.
- **Analyst inference:** Reasoned conclusion not yet directly demonstrated.
- **Security assumption:** Property believed necessary for safety.
- **Unresolved contradiction:** Conflicting evidence that requires investigation.
- **Unknown:** An explicitly identified gap.

Each assertion records:

- Provenance
- Timestamp
- Application version or deployment fingerprint
- Environment
- Persona and tenant
- Confidence
- Supporting evidence
- Contradicting evidence
- Expiration or revalidation condition

Documentation, source, runtime observations, and analyst inference must remain separate layers. They must not be collapsed into a single apparent truth.

Contradictions are high-value research leads, including:

- Documented restrictions not observed at runtime
- Runtime features absent from documentation
- Source routes absent from the visible application
- UI restrictions not enforced by APIs
- Multiple services interpreting identity or ownership differently
- Security checks present in one interface but absent in another
- Intended state transitions that can be bypassed

## 5. Staged Knowledge Acquisition

Immediate source access can bias the agent toward developer intent. Testing should therefore proceed in stages.

### 5.1 Blind black-box learning

The harness first behaves like an external user or attacker without source-derived assumptions:

- Fingerprint the deployment.
- Explore unauthenticated and authenticated surfaces.
- Follow normal workflows.
- Record requests, responses, redirects, errors, and asynchronous effects.
- Establish baseline behavior for each test persona.

### 5.2 Documentation-assisted learning

The harness reviews:

- User guides
- Administrator guides
- API documentation
- Integration guides
- Architecture material
- Runbooks
- Release notes
- Support articles
- Public security claims

It extracts roles, workflows, entities, state transitions, intended restrictions, integrations, and business rules.

### 5.3 Source-assisted white-box review

When authorized source is available, the harness reviews:

- Routes and controllers
- Authorization checks
- Data-access filters
- Identity and session handling
- Database models
- Background jobs
- Webhook handlers
- Feature flags
- Serialization and validation
- Sensitive data flows
- Logging and audit behavior
- Undocumented operations
- Trust-boundary crossings

### 5.4 Reconciliation

The harness compares black-box observations, documentation, and source-derived expectations. Disagreement creates prioritized hypotheses.

This staged process preserves independent discovery while later enabling precise source-to-runtime analysis.

## 6. Application Knowledge Model

The persistent world model must include:

- Users, roles, groups, organizations, and tenants
- Assets, ownership, sharing, and delegated authority
- Sensitive data and data classifications
- Valid and invalid state transitions
- Browser pages and client-side routes
- HTTP APIs, GraphQL, WebSockets, RPC, and alternate protocols
- Forms, parameters, schemas, and hidden fields
- Authentication, enrollment, recovery, and identity-linking mechanisms
- Authorization enforcement points
- Sessions, tokens, API keys, and revocation behavior
- Caches, queues, webhooks, scheduled work, and background jobs
- External identity, payment, storage, analytics, and messaging providers
- Trust boundaries and data flows
- Security controls and expected failure behavior
- Business rules and implicit assumptions
- Deployment fingerprint, build, source revision, and feature configuration
- Evidence and confidence for every relationship

The model should support graph queries such as:

- Which operations can affect a financial asset?
- Which routes consume a tenant-controlled identifier?
- Which asynchronous jobs act after authorization may have changed?
- Which controls rely on upstream validation?
- Which minor observations could combine into an attacker objective?

## 7. Normal-Behavior Learning

Before significant mutation, the harness must learn legitimate behavior:

- Complete onboarding and account recovery.
- Exercise every available test role.
- Create, read, update, share, archive, and delete major entities.
- Traverse complete business processes.
- Observe asynchronous and delayed effects.
- Learn normal errors, redirects, timing, retries, and rate limits.
- Map UI actions to backend operations.
- Compare equivalent UI, API, mobile, and integration behavior.
- Record which data and permissions change after every operation.

This baseline prevents normal behavior from being misreported as a vulnerability and enables meaningful differential testing.

## 8. Threat Model and Attacker Objectives

The harness must reason from attacker goals rather than only vulnerability categories.

Representative attacker personas:

- Anonymous internet attacker
- Ordinary authenticated user
- Malicious tenant
- Compromised privileged user
- Insider with partial access
- Stolen-session attacker
- Integration or supply-chain attacker
- Automated fraud operator
- Patient attacker combining low-severity weaknesses

Representative objectives:

- Access another user’s or tenant’s data
- Influence a financial or accounting outcome
- Take over an account
- Gain administrative capability
- Retain access after revocation
- Abuse a trusted integration
- Execute an operation without required approval
- Manipulate audit or detection visibility
- Cause disproportionate computational or financial cost
- Cross a trust boundary into another service

Each persona receives:

- Initial knowledge
- Available credentials
- Permitted starting position
- Objectives
- Operational constraints
- Success conditions

The harness maintains attack trees and attack graphs connecting observations, prerequisites, capabilities, and objectives.

## 9. Security Invariants

The harness converts application understanding into testable properties:

```text
Subject + operation + resource + state + context → expected decision and side effects
```

Examples:

- A tenant user cannot read or mutate another tenant’s objects.
- A revoked session cannot perform privileged actions.
- A completed transaction cannot be altered.
- A refund cannot exceed the captured amount.
- A signed callback cannot be replayed successfully.
- A one-time invitation cannot be redeemed twice.
- Limits remain enforced under concurrent requests.
- Sensitive values do not appear in low-privilege exports.
- Security-relevant actions produce attributable audit records.
- A failed authorization attempt produces no protected side effect.

Each invariant should generate:

- Positive tests
- Negative tests
- Boundary tests
- Alternate-interface tests
- Invalid-sequence tests
- Temporal tests
- Replay tests
- Concurrent tests
- Degraded-dependency tests

## 10. Hypothesis-Driven Experiments

Each investigation must be represented as a falsifiable hypothesis.

```text
Hypothesis:
  A project member can retrieve another tenant's invoice.

Basis:
  The endpoint accepts a caller-controlled invoice identifier, and no
  tenant filter was observed in the reviewed data-access path.

Experiment:
  Repeat the same operation using owner, same-tenant non-owner,
  other-tenant, anonymous, and invalid-object cases.

Expected:
  Only explicitly authorized principals succeed.

Controls:
  Known valid owner access succeeds. Random identifiers do not expose
  unrelated objects. Equivalent protected endpoints reject cross-tenant access.

Observed:
  Record raw behavior without interpreting it prematurely.

Conclusion:
  Confirmed, rejected, or inconclusive based on evidence.
```

Every experiment defines:

- Responsible hypothesis
- Authorization basis
- Target and environment
- Application fingerprint
- Persona, tenant, and test data
- Preconditions
- Exact sequence
- Expected invariant
- Control comparisons
- Request, concurrency, and data-volume budgets
- Predicted side effects
- Stop conditions
- Cleanup procedure
- Evidence to collect
- Capability and approval level

The harness should vary one meaningful dimension at a time initially, then combine promising mutations when searching for chains.

## 11. Adversarial Test Methodologies

Known vulnerability catalogs provide minimum breadth. They are not the primary research strategy.

The harness must combine:

### 11.1 Differential testing

Repeat equivalent operations while changing:

- Identity
- Role
- Tenant
- Ownership
- Object
- Application state
- Request channel
- Sequence
- Timing
- Content type
- Encoding
- Feature flag
- Integration state

Unexpected differences become hypotheses.

### 11.2 State-machine and workflow mutation

Test:

- Skipped steps
- Reordered steps
- Repeated steps
- Resumed or stale workflows
- Invalid transitions
- Partial completion
- Cancellation races
- Reuse of completed or expired artifacts

### 11.3 Temporal testing

Evaluate behavior around:

- Session rotation
- Password and MFA changes
- Role removal
- Tenant removal
- Token and link expiration
- Delayed webhooks
- Scheduled jobs
- Authorization changes between enqueue and execution
- Long-lived browser or API sessions

### 11.4 Concurrency and replay

Use controlled concurrency to test:

- Duplicate transactions
- Conflicting updates
- Limit bypasses
- Inventory and quota races
- Time-of-check/time-of-use gaps
- Double redemption
- Replay of callbacks and signed requests
- Idempotency guarantees

### 11.5 Cross-interface comparison

Compare behavior across:

- Browser UI
- Direct HTTP API
- GraphQL
- WebSocket
- Mobile or alternate clients
- Import and export paths
- Webhooks
- Administrative interfaces

### 11.6 Parser and representation disagreement

Investigate inconsistent interpretation of:

- Encodings
- Duplicate fields
- Content types
- Number and date formats
- Path normalization
- Host and forwarding headers
- Nested object structures
- Case sensitivity

### 11.7 Source-to-runtime enforcement tracing

Trace important controls from:

- Entry point
- Authentication
- Authorization
- Validation
- Data access
- Side effect
- Logging
- Asynchronous continuation

The existence of a check in source is not evidence that every runtime path uses it.

### 11.8 Degraded-dependency and failure-mode testing

When authorized and safely reproducible, evaluate whether controls fail open when:

- Identity services are unavailable
- Caches are stale
- Queues retry
- Webhooks arrive late
- Downstream validation times out
- Feature configuration is incomplete

### 11.9 Business-abuse analysis

Mine assumptions such as:

- Only the UI calls this endpoint.
- This operation occurs only once.
- Object identifiers are unguessable.
- The previous service already validated the data.
- Only administrators know this route.
- Users perform steps in the expected order.
- A webhook is authentic because it originated from a known network.

The harness deliberately attempts to falsify each assumption.

### 11.10 Negative-space analysis

Identify controls that should exist but appear absent:

- No visible authorization decision
- No replay protection
- No audit event
- No ownership check
- No state validation
- No binding between user intent and eventual side effect

Absence creates a hypothesis, not an immediate finding.

### 11.11 Novel and chained vulnerability discovery

The harness retains weak signals for future composition. It should search for paths such as:

```text
Identity disclosure
  → recovery workflow weakness
  → account takeover
  → stale privileged session
  → cross-tenant export
```

Individual observations may remain informational until they contribute to demonstrated impact.

## 12. Campaign Planning

The agent should alternate between:

- **Exploration:** Discover new surfaces, entities, and relationships.
- **Exploitation research:** Deepen promising anomalies.
- **Verification:** Reproduce and challenge suspected issues.
- **Chaining:** Determine whether weak signals combine.
- **Regression:** Retest previous findings and invariants.
- **Reflection:** Identify blind spots, bias, and diminishing returns.

A conceptual prioritization model is:

```text
priority =
  potential impact
  × likelihood of a violated assumption
  × evidence quality
  × novelty
  × expected information gain
  ÷ operational risk
  ÷ test cost
```

The planner should favor high-value unexplored combinations rather than maximizing request volume.

## 13. Persistent Memory

The harness needs durable investigative memory rather than a growing conversation transcript.

Required memory structures:

- Observation ledger
- Versioned application model
- Threat model
- Security invariant catalog
- Hypothesis backlog
- Attack graph
- Coverage ledger
- Tested and untested combinations
- Rejected hypotheses and rejection evidence
- Confirmed findings
- Test personas and synthetic-data relationships
- Reproduction artifacts
- Deployment and release history
- Questions requiring domain-owner clarification

Required memory behavior:

- Evidence expiration and revalidation
- Duplicate-test suppression
- Hypothesis retirement
- Contradiction detection
- Branching investigations
- Periodic summarization without losing primary evidence
- Periodic model reconstruction
- Release-triggered invalidation
- Separation of target knowledge from governing instructions

A conclusion valid for one build or environment must not silently transfer to another.

## 14. Logical Research Roles

The following responsibilities should remain intellectually and operationally separated, even if some share an underlying model:

- **Observer:** Records normal behavior without attacking.
- **Modeler:** Constructs entities, workflows, and trust boundaries.
- **Threat analyst:** Identifies assets, assumptions, and attacker objectives.
- **Adversary planner:** Generates hypotheses and attack chains.
- **Executor:** Performs only policy-approved experiments.
- **Skeptical reviewer:** Attempts to disprove the analyst’s interpretation.
- **Verifier:** Independently reproduces findings.
- **Safety governor:** Blocks or terminates unsafe actions.
- **Coverage analyst:** Identifies blind spots and diminishing returns.

The planner and executor must not be able to override the safety governor.

## 15. White-Hat Authorization Model

Every campaign requires a machine-enforceable authorization manifest containing:

- Authorized owners and approvers
- Exact domains, hosts, ports, APIs, repositories, and environments
- Permitted redirects and third-party dependencies
- Testing window and authorization expiration
- Allowed personas and accounts
- Allowed techniques
- Rate and concurrency limits
- Data-volume limits
- Production versus non-production rules
- Forbidden operations
- Protected systems and records
- Data retention and evidence handling requirements
- Emergency contacts
- Kill-switch mechanism

The harness fails closed. Scope ambiguity means no active testing.

### 15.1 Capability levels

| Level | Activity | Approval |
|---|---|---|
| 0 | Passive reading, documentation review, and observation | Campaign authorization |
| 1 | Reversible, low-volume active tests | Automatic within explicit scope |
| 2 | State-changing tests using synthetic data | Pre-authorized campaign policy |
| 3 | Potentially sensitive or operationally risky proof | Just-in-time human approval |
| 4 | Destructive, availability-impacting, persistent, or real-data extraction | Prohibited by default; separate exceptional authorization |

The harness cannot raise its own capability level.

### 15.2 Automatic halt conditions

Testing must stop when:

- Real sensitive data is unexpectedly accessed.
- Effects cross an authorized tenant or boundary.
- Material service degradation appears.
- An unplanned financial or external side effect occurs.
- Scope or target identity becomes ambiguous.
- Credentials or secrets leak.
- The environment differs from the authorized target.
- Test state cannot be safely cleaned up.
- Observed behavior exceeds the approved capability level.

## 16. Evidence-Safe Exploitation

Proof must stop at the minimum action needed to establish the vulnerability:

- Use synthetic records for unauthorized-access demonstrations.
- Prove code execution with a harmless marker.
- Demonstrate server-side requests using an approved callback service.
- Show privilege escalation without modifying unrelated users.
- Confirm exposure without bulk downloading data.
- Demonstrate race conditions with isolated test entities.

Every action records:

- Hypothesis
- Authorization basis
- Timestamp
- Agent and execution identity
- Target and application version
- Exact request or browser action
- Persona and test data
- Result
- Side effects
- Cleanup status
- Evidence integrity hash

## 17. Prohibited Autonomous Behavior

Without separate, explicit authorization and human control, the harness must not:

- Establish persistence
- Move laterally outside application scope
- Harvest real credentials
- Perform social engineering
- Bulk-extract real data
- Modify unrelated customer data
- Disable security controls
- Conceal activity
- Conduct availability attacks
- Deploy executable payloads to unrelated systems
- Retain unnecessary personal, secret, or regulated data
- Use discovered credentials against additional systems

## 18. Independent Adversarial Review

Adversarial review applies in two directions.

### 18.1 Adversarial testing of the application

The harness adopts realistic attacker personas and searches for paths to attacker objectives.

### 18.2 Adversarial review of conclusions

A separate skeptical reviewer attempts to disprove every proposed finding:

- Is this intended behavior?
- Was state contaminated by an earlier test?
- Could caching, replication, or eventual consistency explain it?
- Does the control case reproduce the behavior?
- Did the analyst misunderstand the documentation?
- Does source behavior differ from the deployed version?
- Is impact demonstrated or imagined?
- Can an independent path reproduce the result?
- Is evidence sufficient for remediation?

The reviewer should receive primary evidence without being anchored by the original conclusion where practical.

## 19. Finding Lifecycle

Findings progress through explicit states:

```text
Hypothesis
  → Planned
  → Authorized
  → Tested
  → Anomalous
  → Independently reproduced
  → Confirmed / Rejected / Inconclusive
  → Remediated
  → Regression verified
```

Critical findings should, where practical, be reproduced using two independent paths or methods.

Severity and confidence must remain separate. A severe hypothetical issue with weak evidence must not be reported as a confirmed critical vulnerability.

## 20. Finding Standard

A confirmed finding contains:

- Title and unique identifier
- Violated invariant
- Affected application version and environment
- Attacker prerequisites
- Affected identities, tenants, assets, and states
- Reproduction sequence
- Control comparison
- Sanitized evidence
- Demonstrated impact
- Blast radius
- Attack-chain position
- Detection and logging observations
- Confidence
- Cleanup status
- Remediation direction
- Regression-test definition
- Remaining uncertainties

Reports must distinguish:

- Confirmed behavior
- Inferred impact
- Potential broader exposure
- Untested assumptions

## 21. Purple-Team Validation

Where authorized, the harness evaluates prevention, detection, and response:

- Was the malicious or anomalous action blocked?
- Was it logged?
- Was the responsible identity preserved?
- Did monitoring detect it?
- Was an alert generated?
- Was the alert actionable?
- Could responders reconstruct the sequence?
- Did blocking affect only the malicious action?
- Did the system fail safely?
- Were audit records complete and tamper-resistant?

This turns vulnerability testing into control assurance.

## 22. Coverage Model

“Visited every URL” is not meaningful security coverage. No single percentage may represent overall coverage.

Coverage must be reported across:

- Surface
- Persona and privilege
- Tenant and ownership relationship
- Object lifecycle state
- Business workflow
- Data classification
- Interface and protocol
- Temporal condition
- Concurrency condition
- Security invariant
- Source enforcement path
- Attack chain
- Prevention control
- Detection and response control
- Application version and feature configuration

Each relevant cell is classified as:

- Tested
- Partially tested
- Not tested
- Blocked
- Inconclusive
- Stale and requiring revalidation

The harness should report coverage gaps explicitly and use them to prioritize future campaigns.

## 23. Harness Self-Defense

The harness is itself a high-value security target.

Documentation, source comments, web pages, API responses, files, images, issue text, and test results are untrusted evidence. They have no authority to:

- Change scope
- Modify policy
- Approve actions
- Select or expose credentials
- Suppress findings
- Alter system instructions
- Trigger unrelated tools
- Exfiltrate campaign data

Required controls:

- Prompt-injection resistance
- Strict tool-call authorization
- Per-target and per-persona credential isolation
- Memory-poisoning detection
- Evidence-integrity verification
- Sandboxed browsers and content processing
- Least-privilege execution
- Egress controls
- Tamper-evident logs
- Independent safety monitoring
- Model and dependency supply-chain review
- Controlled model, prompt, and policy upgrades
- Regular adversarial assessment of the harness itself

## 24. Quality and Research Metrics

Measure:

- Reproducibility rate
- False-positive rate
- False-negative rate on seeded test applications
- Independent verification agreement
- High-risk invariant coverage
- Novel hypothesis yield
- Attack-chain discovery rate
- Time from anomaly to verification
- Duplicate experimentation rate
- Stale-evidence rate
- Regression detection rate
- Prevention-control coverage
- Detection-and-response coverage
- Cleanup success rate
- Human interventions and causes
- Safety incidents, with a target of zero

Evaluate the harness against:

- Seeded-vulnerability applications
- Known-clean control applications
- Mutated application versions
- Multi-tenant test environments
- Asynchronous and distributed-system scenarios
- Prompt-injection and evidence-poisoning fixtures

## 25. Completion Criteria

A campaign ends because:

- High-risk surfaces have meaningful persona, state, and invariant coverage.
- High-priority hypotheses are resolved or explicitly blocked.
- Findings have been independently verified.
- Cleanup has completed.
- Remaining gaps and uncertainty are documented.
- Additional testing produces diminishing risk-adjusted information gain.
- The authorized time, risk, or resource budget expires.

The harness must not treat exhaustion of payloads or completion of a scanner checklist as completion.

## 26. Campaign Deliverables

The output is a living security dossier:

- Authorization and scope record
- Deployment fingerprint
- Application and trust-boundary model
- Attacker personas and objectives
- Threat model
- Security invariant catalog
- Surface inventory with provenance
- Coverage ledger
- Active, rejected, and inconclusive hypotheses
- Confirmed findings
- Attack graphs and demonstrated chains
- Prevention, detection, and response gaps
- Evidence archive
- Cleanup record
- Residual-risk statement
- Regression suite
- Recommended next campaign

## 27. Review History Behind This Specification

This version incorporates seven explicit critique passes:

1. **Epistemic rigor:** Added provenance, uncertainty, contradiction handling, and falsification.
2. **Source-review bias:** Added blind black-box, documentation-assisted, source-assisted, and reconciliation stages.
3. **Adversarial sophistication:** Added objective-driven attacker campaigns and attack graphs.
4. **Long-running intelligence:** Added evidence expiration, release-aware memory, hypothesis retirement, and model reconstruction.
5. **White-hat governance:** Added machine-enforceable authorization, capability levels, and halt conditions.
6. **Evidence and review:** Added an explicit finding lifecycle and independent adversarial verification.
7. **Harness security:** Added prompt-injection, memory-poisoning, credential isolation, and tool-authorization requirements.

## 28. Reference Baselines

These references provide useful minimum baselines. They do not replace application-specific reasoning:

- [OWASP Web Security Testing Guide](https://owasp.org/www-project-web-security-testing-guide/)
- [OWASP Application Security Verification Standard](https://owasp.org/www-project-application-security-verification-standard/)
- [OWASP Threat Modeling](https://owasp.org/www-community/Threat_Modeling)
- [OWASP Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [MITRE ATT&CK Adversary Emulation Plans](https://attack.mitre.org/resources/adversary-emulation-plans/)
- [NIST SP 800-115: Technical Guide to Information Security Testing and Assessment](https://csrc.nist.gov/pubs/sp/800/115/final)
- [PortSwigger: Business Logic Vulnerabilities](https://portswigger.net/web-security/logic-flaws)

