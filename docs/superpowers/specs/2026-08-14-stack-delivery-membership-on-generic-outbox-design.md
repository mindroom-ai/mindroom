# Stack Delivery Membership on the Generic Matrix Outbox

## Goal

Restack PR #1836 on the latest PR #1837 head and make #1837's generic Matrix delivery outbox the sole durable owner of both response and approval transport.

The stacked PR must preserve every useful #1836 membership, recovery, projection, and migration invariant without retaining the deleted response-specific delivery architecture.

## Branch Shape

PR #1836 will use `refactor/unify-approval-delivery-outbox` as its GitHub base.

Its branch will be rebuilt from the latest exact PR #1837 head and force-pushed as a small semantic stack rather than merging two sibling branches with conflict resolutions.

The rewritten history will contain only the #1836 behavior that remains necessary after #1837 and the tests and documentation that prove it.

Neither PR will be merged by this work.

## Durable Ownership

`matrix_delivery_outbox` is the only steady-state owner of a physical Matrix delivery.

Each row will retain its generic #1837 identity and transport facts and add the membership facts required by #1836:

- `delivery_id`
- `stage`
- `event_type`
- `room_id`
- `membership_epoch`
- immutable payload and Matrix transaction ID
- attempted and sending-device state
- acknowledged Matrix event ID
- retired identity-tombstone state

`approval_cards` will continue to own only approval-domain facts such as continuation identity, exact tool call, generation, deadline, and actionability.

Response and approval callers will both use `MatrixDeliveryWorker` and `MatrixDeliveryView` without response-specific transport methods or approval-specific send state machines.

### Logical obligations and physical attempts

An approval continuation owns the logical obligation to show one unavailable-owner notice before its sources are released.

One outbox row normally owns one physical attempt under one immutable room-membership epoch.

The deliberate exception is a visible approval card whose already-decided terminal cleanup survives router departure: the card and both of its delivery stages transfer together to the successor membership.

If that attempt becomes stale, its row remains an identity tombstone and the still-live continuation creates a distinct delivery ID for the current membership epoch.

The current membership epoch must be claimed in the same transaction that derives the delivery ID and enqueues the row, so departure cannot put a new epoch behind an old generation name.

Unavailable-owner cleanup atomically derives the current membership's exact delivery ID and releases sources only when that generation is acknowledged and still belongs to that membership.

## Membership and Delivery Transitions

The enqueue transaction will claim the room membership row and freeze the active epoch onto the first delivery stage.

Later stages for the same delivery ID must retain the same room and epoch.

A room departure will retire unsent rows instead of deleting their ownership identity.

Attempted rows will remain recoverable because Matrix may already hold their physical event.

Recovery will adopt an exact existing event when proven, retire an obsolete absent delivery, and never resend an old-membership delivery into a rejoined room.

Acknowledgement, retirement, and live echo admission will lock in membership-row then delivery-row order so their outcomes are serializable on SQLite and PostgreSQL.

## Stable Matrix Identity and Projection

Every new generic delivery payload will carry `io.mindroom.delivery_id` with principal, delivery ID, and stage.

The marker will identify response messages, approval cards, terminal approval edits, and source-less notices across device changes.

Projection will accept a marker-bearing event only when its outbox owner belongs to the event room, sender principal, current membership epoch, and non-retired delivery.

The same ownership check will cover sync admission, acknowledgement projection, hydration, history recovery, and point refetch.

A stale acknowledgement will settle the durable delivery identity without installing old-membership visible state.

Retiring an edit that raced into projection will remove that revision and invalidate hydration so the current membership can reconstruct legitimate history.

## Migration

PR #1837's `event_journal/migrations.py` remains the only schema-migration module.

The stacked migration will move the released `response_outbox` directly into the final membership-owned `matrix_delivery_outbox`.

Legacy approval delivery rows will not be promoted into the generic outbox.

The migration will expire their undecided calls, preserve delivered card IDs as action tombstones, wake waiting continuations, and drop the obsolete transport table in the same transaction.

Existing response deliveries may derive their epoch only from an exact admitted journal source in the same room.

Never-attempted rows whose membership cannot be proven will be deleted because they never reached Matrix.

Acknowledged rows with no provable membership will remain as retired event-ID tombstones.

Attempted, unacknowledged legacy rows will make startup fail with a clear journal-recreation error even when their epoch is derivable, because their old physical payload has no stable delivery marker and cannot be reconciled safely after a device change.

The migration will commit atomically on both backends and leave the steady-state worker free of compatibility conditionals.

## Test Preservation

Before implementation, every test added by #1836 will be inventoried as ported, consolidated, or obsolete.

A test may be removed only when it exclusively exercises a deleted response-specific facade or duplicated approval transport protocol and a named generic test proves the same invariant.

All useful tests will be ported to `MatrixDelivery`, `MatrixDeliveryWorker`, `MatrixDeliveryView`, and `matrix_delivery_outbox` terminology.

The retained suite will cover at least:

- source-backed and source-less epoch freezing
- shared INITIAL and FINAL ownership
- departure before and after claim
- send, acknowledgement, echo, and retirement orderings
- same-device and changed-device recovery
- ordinary messages, edits, approval cards, terminal edits, and unavailable notices
- stable marker sender, principal, room, and stage scoping
- hydration, recovery, and point-refetch rejection of stale events
- SQLite and PostgreSQL lock ordering
- exact migration backfill and explicit refusal of unverifiable rows

Mechanical duplicates that differ only by the former response or approval facade will be consolidated at the generic store boundary.

A small number of end-to-end gateway and approval tests will remain to prove that both domains use the shared worker correctly.

## Non-Goals

This work will not redesign approval policy, continuation semantics, interactive question ownership, sync checkpoint storage, or handled-turn storage.

It will not add a second outbox, compatibility wrapper, runtime fallback, or parallel delivery worker.

It will not merge PR #1837 or PR #1836.

## Acceptance Criteria

- PR #1836 is based on the latest exact PR #1837 head and its GitHub base names the #1837 branch.
- No steady-state `response_outbox`, `ResponseDelivery`, or approval-specific send protocol remains in the stacked diff; the released table name appears only at the one-time migration boundary.
- Responses and approvals share one membership-owned generic delivery state machine.
- A stale unavailable-owner notice cannot release sources or prevent a current-membership notice generation from completing the logical obligation.
- Every useful #1836 test is ported or has an explicitly identified generic equivalent.
- Focused SQLite and PostgreSQL delivery, approval, projection, hydration, recovery, and migration suites pass.
- Ruff, formatting, Tach, pre-commit, and `git diff --check` pass.
- The final stacked diff is reviewed for net deletion and contains no avoidable compatibility machinery.
