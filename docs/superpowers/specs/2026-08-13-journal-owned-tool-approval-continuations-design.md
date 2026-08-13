# Journal-Owned Tool Approval Continuations

## Goal

Suspend an Agno response while human tool approval is pending without retaining a response coroutine, typing indicator, or conversation lock.

Keep exact-call approval, fail-closed behavior, restart recovery, normal response lifecycle re-entry, and durable Matrix delivery while materially reducing the production code added by PR #1807.

## Constraints

Agno's persisted paused run remains the only execution boundary.

The original admitted Matrix source remains the durable work item until a final outbox handoff completes.

The event journal is the only MindRoom-owned continuation database.

The response outbox remains the only authority for whether a waiting or final Matrix message is durably owed.

The approval-card journal remains readable for the existing deployment and keeps its current legacy-card fail-closed startup behavior for one deployment cycle.

Approval-gated tools stay hidden or rejected on surfaces that cannot resume a persisted Agno run.

No legacy live waiter or in-memory human-decision future may return.

Tests may grow, but the redesign must remove at least 1,000 net production lines from the current PR branch before it is accepted as a simplification.

## Considered Designs

### Keep the Current Continuation Store and Dispatcher

This retains a separate Agno approval table, a seven-state continuation protocol, a transport dispatcher, startup recovery, and reconciliation against the card journal and response outbox.

It is the lowest-change option but does not address the user's production-size concern or the repeated multi-writer race class.

This option is rejected.

### Admit a Synthetic Continuation Journal Event

This would settle the original source and replace it with a synthetic event containing the paused-run snapshot.

It looks small, but the synthetic event needs separate final-turn linkage and a durable claimed marker to prevent replay after a tool may have executed.

Those additions recreate a second work protocol under a different name.

This option is rejected.

### Keep the Original Source Pending and Add a Narrow Journal Sidecar

The original source row stays pending and remains the work item already understood by `PendingEventWorker`.

A small sidecar table records only the paused-run snapshot, exact calls, and whether the source is waiting, ready, claimed, or failing.

Journal pending reads hide sources whose continuation is waiting or currently claimed, and expose only the primary source when it becomes ready or needs recovery.

Human decisions update the approval card and exact continuation call within one event-journal transaction.

This option removes the separate dispatcher and most cross-ledger reconciliation while preserving the existing durable worker and outbox invariants.

This is the selected design.

## Durable Model

The event-journal schema gains three narrow tables.

`approval_continuations` stores one row per paused response with a globally unique approval ID, its owning journal principal, primary source event ID, state, current generation, claimant runtime generation, failure reason, waiting response event ID, paused Agno run identity, and exact suspension-time response snapshot.

`approval_continuation_sources` stores every source event discharged by the response so coalesced turns remain one approval owner.

`approval_continuation_calls` stores the exact tool call ID, tool name, invoking agent, deadline in integer nanoseconds, decision, and reason for the current pause generation.

Approval cards gain nullable continuation and tool-call identity columns populated only for current-format cards.

Legacy cards keep those columns null and continue through the existing fail-closed settlement path.

The continuation has four nonterminal states.

- `waiting` means at least one exact call still requires a durable decision.
- `ready` means every call has a durable decision and the primary source may enter the normal response worker.
- `claimed` means a response lifecycle has begun continuing the paused Agno run and a crash must not execute it again.
- `failing` means execution is forbidden and the next worker pass must terminalize cards and the waiting response.

There are no durable completed or failed continuation rows.

Successful or failed terminal settlement verifies the final outbox acknowledgement, settles all owned source rows, and deletes the continuation and its child rows in one transaction.

## Work Dispatch

`PendingEventWorker` remains the only retry and per-room serialization mechanism for configured entity continuations.

The journal's pending query excludes every continuation-owned secondary source.

It excludes a primary source while its continuation is `waiting`.

It excludes a primary source while it is `claimed` by the current runtime generation.

It exposes a primary source when the continuation is `ready`, `failing`, or `claimed` by an earlier runtime generation.

A recorded final decision wakes the owning bot's existing journal worker by releasing the original source IDs.

The normal replay pipeline re-enters `ResponseRunner` under admission control, the conversation lock, stop tracking, hooks, and post-response effects.

`ResponseRunner` claims `ready` to `claimed` immediately before calling Agno.

A chained pause replaces the claimed generation with new calls and returns to `waiting` or `ready` without an inline resume loop.

The existing worker immediately sees a new ready generation without a separate dispatcher backoff.

## Suspension Ordering

The runner evaluates approval policy and renders the waiting content before it creates durable continuation ownership.

The waiting response is delivered through the existing INITIAL outbox path first.

Only after that delivery is acknowledged does one journal transaction create the continuation row, source links, and exact calls.

A crash before that transaction leaves the original source pending, so normal replay reruns the model and asks again before any tool execution.

This may repeat or reuse the waiting placeholder but cannot authorize or duplicate a tool side effect.

A crash after that transaction leaves the original source hidden or runnable according to the sidecar state and resumes the exact persisted Agno run.

Cards are claimed in the existing card journal before Matrix send.

Card rows carry continuation identity, so a crash between card send and event acknowledgement is repaired from the card journal without copying card event IDs into the continuation row.

## Decision Commit

Current-format card decisions use one event-journal write transaction.

That transaction validates the stored card, applies first-decision-wins to the exact current generation and call ID, rechecks the integer deadline, writes the final card resolution, and changes `waiting` to `ready` only when every call has a decision.

A late approval is atomically converted to expiry.

The transaction returns the durable winning status and reason for the Matrix edit.

Duplicate reactions only redeliver the stored resolution and never alter the continuation.

The existing two-phase `resolve_call` then `acknowledge_call` protocol and `decision_recorded` field are deleted.

## Final Delivery and Crash Recovery

Approval continuation final delivery uses the normal delivery gateway but delays source settlement until the continuation is explicitly finished.

The FINAL outbox row is still enqueued before Matrix I/O and its payload freezes on first attempt.

The continuation remains `claimed` or `failing` while FINAL delivery is unacknowledged, which prevents model replay even though the original journal source is still pending.

After acknowledgement, the runner atomically settles the source rows and deletes the continuation.

If the process crashes after enqueue or Matrix acceptance, the next worker pass sees an old-runtime claim, recovers the frozen FINAL through the normal outbox path, and finishes the same continuation without rerunning Agno.

Failure settlement first moves the row to `failing`, terminalizes every unresolved current-format card, and sends or edits one durable FINAL failure response.

It finishes only after the FINAL outbox row is acknowledged.

A successful FINAL already frozen in the outbox always wins over a later failure request.

## Unavailable Entities

A configured entity's own journal worker owns normal continuation execution and recovery.

When an entity is removed or permanently fails to start, the orchestrator performs a bounded scan for that entity's nonterminal continuations and atomically moves them to `failing`.

The router may then emit a principal-separated terminal notice, but it may never continue the Agno run or edit as the unavailable entity.

Startup performs the same bounded unavailable-owner scan after runtime configuration and router transport are ready.

This is the only continuation work outside the owning source worker because no owning worker can exist for a removed entity.

## Module Boundaries

`event_journal/approval_continuations.py` owns continuation rows, exact decisions, pending-source visibility, terminal handoff, and serialization.

`event_journal/approvals.py` continues to own Matrix card durability and delegates current-format decision commits to the continuation operation within the same transaction.

`approval_response.py` owns policy evaluation, waiting text, card publication, chained-pause publication, and visible failure settlement.

`approval_execution.py` and the team continuation code keep exact Agno reconstruction and confirmation application.

`response_runner.py` owns suspension handoff and lifecycle re-entry, but it does not run a continuation dispatcher or perform raw continuation SQL.

`approval_transport.py` owns Matrix approval transport, legacy-card startup settlement, and the small unavailable-entity fallback only.

`bot.py` and `orchestrator.py` remain wiring and lifecycle shells.

## Deletion Targets

Delete the Agno-table-backed `ApprovalContinuationStore`, its process locks, SQLAlchemy dependency, pagination, and seven-state CAS protocol.

Delete transport continuation task sets, named-task deduplication, dispatcher retry loops, generation backoff, startup continuation classifier, card attachment repair, claimed-delivery reconciliation, and store close ownership.

Delete response coordinator store wrappers, publication binding, decision acknowledgement, source lookup scans, and success/failure terminal state writers.

Delete runner-to-orchestrator continuation scheduling and failure-request plumbing.

Delete tests that exist only to pin the removed dispatcher or old store implementation, while retaining or replacing every behavior and crash invariant test.

The target is at least 1,000 fewer production lines than the current branch and an expected PR production delta near +1,800 to +2,200 lines versus main.

## Verification

Each state transition and crash boundary is developed test-first against both SQLite and PostgreSQL event-journal operations where backend concurrency matters.

Required focused behaviors include exact-call first decision, deadline expiry, duplicate reaction, auto-approved ready wake, human-approved ready wake, restart from waiting, restart from claimed without Agno replay, chained pause, transient FINAL failure, card-send crash recovery, cancellation after ownership, user STOP, removed entity failure, agent and team real Agno continuation, hook metadata, attachments, memory inputs, and source settlement exactly once.

The final branch must pass the focused approval and response suites with `-n auto`, the full suite with `-n auto`, all pre-commit hooks, Tach dependency checks, and `git diff --check`.

The final report must include exact production additions and deletions versus live main and the net reduction achieved by this redesign.
