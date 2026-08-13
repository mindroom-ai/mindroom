# Journal-Owned Interactive Questions Design

## Summary

Interactive questions move from a process-global JSON store into the existing event journal.
The event journal becomes the sole durable authority for question registration, selection ownership, membership invalidation, and terminal consumption.
Runtime objects will continue to own executing asyncio tasks, but they will no longer own durable question state.

## Problem

PR #1823 detached interactive reaction responses from the room journal lane so a long model response would not block later room events.
That exposed an existing ownership split between reaction sources in the event journal and questions in `interactive_questions.json` plus process-global dictionaries.
PR #1825 then accumulated restore logic, cross-process file locking, membership cleanup callbacks, shutdown waits, and replay reconciliation to keep those two durable systems consistent.
The resulting implementation is heavily tested but has the wrong atomic boundary.

## Goals

- Keep interactive reaction responses detached from the room journal lane.
- Preserve reaction and numeric text selection behavior.
- Make a question claim replayable by the same durable source after failure or restart.
- Consume a question atomically with terminal settlement of the source that selected it.
- Remove departed-room questions atomically with the membership fence.
- Remove the JSON question database, process-global claim ownership, and restore/commit reconciliation.
- Retain only lifecycle machinery that prevents two live runtimes from executing the same source concurrently.
- Reduce the production code and the number of cross-module invariants in PR #1825.

## Non-Goals

- This design does not add durable runtime execution leases.
- This design does not permit an old and replacement runtime to execute the same source concurrently.
- This design does not redesign Matrix departure-observation identity or ordering.
- This design does not move interactive response parsing or Matrix reaction-button rendering into the journal.
- This design does not add backward compatibility for `interactive_questions.json`, because the project explicitly does not require backward compatibility yet.
- This design does not solve the pre-existing post-send registration window for direct Matrix tool messages.

## Ownership Boundary

The event journal owns every durable fact about an interactive question.
`interactive.py` owns only pure response parsing, prompt construction, sender validation helpers, and Matrix reaction-button I/O.
`ReactionDispatcher` owns routing an admitted reaction to the appropriate semantic consumer.
`TurnController` owns executing the selected turn, but not restoring or consuming the question.
`ResponseRunner` owns the lifetime of the detached asyncio task, but not the durable selection.
`MembershipFence` translates Matrix membership observations into journal transitions, but does not invoke external cleanup callbacks.

## Data Model

Add separate active-question and source-selection tables to the event-journal schema.

```sql
CREATE TABLE IF NOT EXISTS interactive_questions (
    principal_id TEXT NOT NULL,
    question_event_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    creator_agent TEXT NOT NULL,
    question_json TEXT NOT NULL,
    membership_epoch BIGINT NOT NULL,
    created_at_ns BIGINT NOT NULL,
    PRIMARY KEY (principal_id, question_event_id)
)

CREATE TABLE IF NOT EXISTS interactive_selections (
    principal_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    PRIMARY KEY (principal_id, source_event_id),
    FOREIGN KEY (principal_id, source_event_id)
        REFERENCES journal_events (principal_id, event_id)
)
```

`question_json` stores the question text, option values, and option labels as one immutable payload.
Every `interactive_questions` row is active and available to a new selection.
Claiming transfers an immutable selection snapshot into `interactive_selections` and deletes the active row in the same transaction.
The source primary key makes one source unable to own multiple selections.
An edited Matrix event may then register a replacement active question at the same event ID without overwriting the pending source's selection.
Numeric text selection orders eligible questions by `created_at_ns` and then `question_event_id` so selection remains deterministic across processes.

## Registration

`PrincipalStore` exposes explicit registration methods for the two real proof forms.
A turn-backed registration verifies the membership epoch recorded on the admitted source turn.
A direct Matrix operation registration verifies the membership epoch captured before network I/O.
Both methods insert the question only when the room remains unfenced at the expected epoch.
The epoch check and insert happen in one writer transaction.
Callers add Matrix reaction buttons only after the insert succeeds.
Production code no longer accepts a synchronous callback that writes a different persistence system while the membership row is locked.

## Selection Claim

Reaction selection uses one store transaction that reads the pending reaction source, reads the target active question, validates the option, records `INTERACTIVE_REACTION`, stores the source-owned selection, and deletes the active question.
If the reaction already owns a stored selection, the transaction returns the same selection for replay.
If another source consumed the active question first, the transaction returns no selection.
If the source is terminal, stale, from another room, or from an old membership, the transaction returns no selection.
Authorization and managed-agent sender checks remain outside the store because they are runtime policy rather than durable state.

Numeric text selection uses the admitted message event ID as the durable source owner.
The store selects the oldest eligible active question in the same room, thread, agent, and current membership.
A replay of the same message returns its stored source-owned selection.

## Failure and Replay

There is no `restore_selection` operation.
Before terminal truth, the immutable selection remains durably bound to a pending journal source.
When execution fails or is cancelled, normal journal retry releases the deferred source and replay retrieves the same selection.
An enqueue or task-start failure therefore requires no question mutation.
The process may die at any point without leaving a process-local claim that hides durable work.

## Terminal Consumption

`journal.settle_many` deletes every stored selection whose source is being settled.
FINAL outbox enqueue already calls source settlement in the same writer transaction, so the answer handoff and question consumption become one commit.
Explicit intentionally-ignored settlement uses the same primitive and therefore converges on the same state.
A replay cannot observe a terminal reaction alongside its stored selection because both state changes share the transaction.
`TurnController` no longer probes terminal source state to choose between commit and restore.

## Membership Invalidation

`_advance_membership_epoch` deletes all active questions for the departed principal and room and removes selections owned by the stale sources it settles.
The same transaction advances the membership epoch, removes derived conversation state, and settles stale turn-backed sources.
`MembershipFence` no longer carries `clear_departed_room`.
The store no longer exposes cleanup callbacks, cleanup-on-rejoin, or an external cleanup retry transaction.
The reported-departure ledger and pre-fanout join ordering remain because delayed and replayed Matrix membership observations are independent of question persistence.
Matrix parsing and journal fencing exchange one immutable `ReportedDeparture` record per observation.
That record keeps room, observation identity, and same-response rejoin evidence together instead of exposing parallel tuples that can disagree in length.

## Runtime Shutdown

The current restart-only wait for source-owned detached tasks remains initially.
Durable question ownership prevents state loss, but it does not make concurrent model or tool execution safe.
The dispatcher must still quiesce before the restart barrier snapshots source-owned tasks.
The `PendingEventWorker` stopped flag and stop generation remain because they prevent external recovery drains from creating new lanes after shutdown or across stop-start ABA.
Durable runtime leases are rejected because they would require generation checks on every downstream side effect and would increase the scope of this PR.

## PR #1807 Compatibility

PR #1807 and this design use the same event journal as the durable workflow boundary but own disjoint records.
Approval continuations remain in their existing continuation tables, while interactive questions and selections use their two focused journal tables.
Membership invalidation deletes both kinds of membership-scoped state inside `_advance_membership_epoch` without coupling their modules.
PR #1807's runtime-generation filtering applies only to approval-owned sources, while an interactive source remains replayable through its question claim and the existing runtime quiescence barrier.
The overlapping schema, journal, store, response runner, turn controller, pending worker, and test files will require mechanical rebase conflict resolution.
After implementation, a merge-tree and focused combined test run against PR #1807's exact pushed head are required to verify semantic compatibility.

## File Structure

- Create `src/mindroom/interactive_models.py` for the leaf question and selection dataclasses.
- Create `src/mindroom/event_journal/interactive_questions.py` for question SQL and transactional operations.
- Create `src/mindroom/membership_models.py` for the leaf reported-departure record shared by sync parsing and journal fencing.
- Modify `src/mindroom/event_journal/schema.py` for the table and lookup indexes.
- Modify `src/mindroom/event_journal/store.py` and `views.py` for typed async APIs.
- Modify `src/mindroom/event_journal/journal.py` so settlement and membership invalidation delete question rows transactionally.
- Reduce `src/mindroom/interactive.py` to parsing, formatting, prompt construction, policy helpers, and button I/O.
- Modify `src/mindroom/reaction_dispatch.py` to claim a selection through the journal.
- Modify `src/mindroom/turn_controller.py` to execute durable selections without commit or restore callbacks.
- Modify `src/mindroom/post_response_effects.py` and `src/mindroom/custom_tools/matrix_conversation_operations.py` to register through `PrincipalStore`.
- Simplify `src/mindroom/event_journal/membership.py` and its bot wiring by removing external cleanup callbacks.
- Remove interactive JSON initialization from `AgentBot.start`.

## Testing Strategy

Journal-store tests cover registration guards, exclusive claims, same-source replay, deterministic text selection, settlement consumption, and membership deletion on SQLite and PostgreSQL.
Reaction-dispatch tests use the real store boundary and prove that semantic-consumer claim and question claim cannot split.
Turn-controller tests prove that failures leave the durable selection replayable without a restore callback.
Restart tests prove that a replacement process can reload and replay a stored selection without process-global state.
Membership tests prove that departure fencing removes active questions and stale source selections in the same transaction and that a rolled-back fence preserves them.
Existing parsing and Matrix-button tests remain in `test_interactive.py` without persistence fixtures.
Tests that exist only to exercise JSON corruption, advisory locks, dirty overlays, or cross-process dictionary reconciliation are deleted with that implementation.

## Acceptance Criteria

- No production reference to `interactive_questions.json` or its lock file remains.
- No process-global active-question or source-selection dictionary remains.
- No production `commit_selection` or `restore_selection` function remains.
- No membership transaction invokes an external question-cleanup callback.
- No public membership boundary represents one reported departure with parallel tuples.
- Claiming a reaction and its question is one transaction.
- Claiming transfers the selected payload away from the active question row so a later edit can register a replacement independently.
- Settling a selected source and consuming its stored selection is one transaction.
- Fencing a departure and removing its active questions and stale selections is one transaction.
- Same-source replay returns the same selection before terminal settlement.
- The focused SQLite and PostgreSQL suites pass.
- The complete non-Matrix test suite, static checks, dependency checks, and formatting checks pass before push.
- The final production diff is materially smaller and has fewer cross-module state transitions than the current PR head.
