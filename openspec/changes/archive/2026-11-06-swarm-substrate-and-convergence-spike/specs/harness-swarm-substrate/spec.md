## ADDED Requirements

### Requirement: Generic substrate lives in one shared library imported by both harnesses
The generic swarm substrate — the append-only blackboard fact store, the artifact store, the `inbox`/`send` message bus, dedup, the per-author budget key family, the local-dir and S3 backends, and the non-security cross-run-learning plumbing — SHALL live in exactly one shared library that both `coding-harness` and `security-harness` import. No harness SHALL hold a second, divergent copy of that generic surface.

#### Scenario: Both harnesses import the same substrate
- **WHEN** either harness's workers start
- **THEN** they import the blackboard fact store, message bus, and budget keys from the one shared library
- **AND** neither harness contains a separately-maintained duplicate of those generic modules

#### Scenario: Append-only per-author-unique key contract is preserved
- **WHEN** any actor writes a fact via the shared library
- **THEN** the record is stored under a per-author-unique key (`facts/{cid}/{kind}/{author}__{uuid}.json`)
- **AND** no two actors ever write the same key, so concurrent writes need no lock and cannot lose an update

### Requirement: Security-specific logic stays in security-harness
The security-specific orchestration — `swarm_standup` credit-scoring, `oracle_router`, and the attempt-lesson *shapes* — SHALL remain in `security-harness` and SHALL NOT move into the shared library. Only the harness-agnostic substrate moves.

#### Scenario: Security orchestration is not lifted
- **WHEN** the shared library's contents are enumerated
- **THEN** `swarm_standup` scoring, `oracle_router`, and attempt-lesson shapes are absent from it
- **AND** those modules remain in `security-harness/workers/`

### Requirement: The lift does not change security-harness swarm behavior
After `security-harness` is re-pointed at the shared library, its swarm SHALL behave identically to before the lift. The lift is a move-and-re-point, not a rewrite.

#### Scenario: Security-harness regression gate passes unchanged
- **WHEN** the security-harness test suite runs after the substrate is lifted
- **THEN** it passes with no behavior change attributable to the lift
- **AND** any failure is reported with its precise mechanism, not a generic label

#### Scenario: Coding-harness can use the substrate
- **WHEN** a coding-harness worker posts a fact and reads it back through the shared library against a local-dir backend
- **THEN** the round-trip succeeds
- **AND** the coding-harness workers depend on the shared library even though no coding-harness workflow consumes it yet
