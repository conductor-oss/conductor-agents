## ADDED Requirements

### Requirement: A frozen contract is fixed and changed only as a controlled event
Before concurrent agents work on shared code, a concrete contract artifact SHALL be committed to the shared branch and treated as fixed. A diff that edits the frozen contract SHALL be classified as a controlled event (its own board item that re-plans dependents), never an ambient edit silently merged.

#### Scenario: Diff respecting the contract proceeds
- **WHEN** an agent's candidate diff builds against the frozen contract without editing it
- **THEN** it is eligible for integration

#### Scenario: Silent contract edit is flagged
- **WHEN** a candidate diff edits the frozen contract artifact
- **THEN** it is classified as a controlled event requiring re-plan of dependents
- **AND** it is not silently merged into the shared branch

### Requirement: Shared-file ownership is recorded on the board and is exclusive while live
Ownership of a shared file SHALL be recorded as a fact on the blackboard and folded into a current owner-map. A work item SHALL be dispatched against a shared file only when no live owner holds it, so two agents never hold the same file open concurrently.

#### Scenario: Exclusive live ownership
- **WHEN** two items concurrently claim the same shared file
- **THEN** exactly one becomes the live owner and the other waits
- **AND** resolution needs no lock, consistent with the substrate's per-author-unique-key guarantee

#### Scenario: Release makes the file grantable again
- **WHEN** the live owner releases a shared file
- **THEN** a waiting item can be granted it

### Requirement: Integration is continuous rebase, not a big-bang merge
Agents SHALL work in short-lived worktrees that re-sync frequently, and an integrator SHALL rebase each ready worktree onto the current shared-branch head continuously. There SHALL be no single big-bang merge of long-lived branches at the end. A rebase conflict SHALL re-open its item with the precise conflict detail attached, never dropping it silently.

#### Scenario: Overlapping worktrees converge without a big-bang merge
- **WHEN** three worktrees modifying overlapping code become ready
- **THEN** each is rebased onto the moving shared head in turn and the shared branch converges
- **AND** no step performs a big-bang merge of all branches at once

#### Scenario: Conflict re-opens the item with a named mechanism
- **WHEN** a continuous rebase hits an unresolvable conflict
- **THEN** the item is re-opened with the precise conflict mechanism recorded
- **AND** the work is not silently discarded

### Requirement: The convergence premise is proven or disproven by a pre-registered experiment
The model SHALL be validated by a controlled experiment on a task deliberately constructed so that at least three work items must touch the same file(s), against a pre-registered success metric, with the outcome reported honestly as GO or NO-GO. A NO-GO is a valid first-class outcome that scopes the swarm to file-disjoint work.

#### Scenario: GO outcome
- **WHEN** at least three agents concurrently modify overlapping code behind the frozen contract
- **THEN** their work lands on one shared branch assembled by continuous re-sync with zero big-bang merge, the branch is green under `test_cycle` with an honest verdict, and a reviewer finds the shared-code changes coherent
- **AND** the result is recorded as GO with the Conductor execution id cited

#### Scenario: NO-GO outcome is honest and decisive
- **WHEN** the agents cannot converge
- **THEN** the result is recorded as NO-GO naming the precise failure mechanism, the roadmap recommendation becomes "scope the swarm to file-disjoint work; do not build Phase 2's shared-code path," and no outcome is massaged (a green that required a hidden big-bang merge or weakened tests is recorded as NO-GO)

#### Scenario: A file-disjoint task does not count as proof
- **WHEN** the experiment task is found to be file-disjoint
- **THEN** it is rejected as invalid proof of the convergence premise before the run
