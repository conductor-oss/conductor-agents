## Purpose

An aggregate, per-run real-cost spend ceiling for swarm-mode runs: every agent's spend accumulates on the shared board, the run warns as it nears the ceiling and halts new work at it, a swarm run without an enforceable ceiling is not startable, and the limit is a spend limit, never a wall-clock deadline (roadmap H5).

## Requirements

### Requirement: A run carries an enforceable aggregate spend ceiling
A swarm-mode run SHALL carry a configured **aggregate** real-cost spend ceiling covering all of its agents, distinct from the existing per-agent turn/dollar caps. Cumulative spend SHALL be accounted from token usage per backend over the substrate's per-author budget keys.

#### Scenario: Cumulative spend is tracked across all agents
- **WHEN** multiple agents in a run consume tokens
- **THEN** their real costs accumulate into one run-level total via the per-author budget keys
- **AND** the total is readable at any time without locking

#### Scenario: Not startable without an enforceable ceiling
- **WHEN** a swarm-mode run is requested with no enforceable spend ceiling
- **THEN** the run refuses to start
- **AND** a single-agent `code_parallel` run (which keeps its per-agent caps) is unaffected by this rule

### Requirement: Early-warning threshold and clean halt at the ceiling
The gate SHALL surface an early-warning state as cumulative spend approaches the ceiling, and SHALL halt the run cleanly when the ceiling is reached — dispatching no new work and letting in-flight work reach a durable boundary — then emit a truthful cost report.

#### Scenario: Warning fires below the ceiling
- **WHEN** cumulative spend crosses the configured warning threshold but is below the ceiling
- **THEN** the gate reports state `warn` with the current spend and percent consumed
- **AND** the run continues

#### Scenario: Clean halt at the ceiling
- **WHEN** cumulative spend reaches the ceiling
- **THEN** the gate reports state `halt`, no new work is dispatched, and in-flight work is allowed to reach a durable boundary
- **AND** a truthful cost report (per-backend/per-author spend, total, ceiling, percent consumed) is produced

### Requirement: The ceiling is a spend limit, never a wall-clock deadline
The halt SHALL be triggered only by accumulated spend, never by elapsed time. The gate SHALL NOT introduce any execution deadline mechanism (`timeout`, `asyncio.wait_for`, shell `timeout`, curl `--max-time`/`--connect-timeout`, or equivalent), per the repository timeout policy. The gate SHALL NOT claim a precise up-front cost estimate for a scope.

#### Scenario: No deadline mechanism is introduced
- **WHEN** the budget-gate module is inspected
- **THEN** it contains no wall-clock deadline or timeout mechanism
- **AND** every halt it causes is attributable to accumulated spend reaching the ceiling

#### Scenario: No false up-front estimate
- **WHEN** a run is configured with a ceiling
- **THEN** the gate records the ceiling as an approved limit to prove or disprove during the run
- **AND** it does not emit a predicted "this scope ≈ $X" figure
