# Matrix Recovery Single-Checkpoint Design

## Status

This replacement design was validated locally across MindRoom and mindroom-nio on 2026-08-04.

It replaces PR 1783's restart coordinator and the first dual-checkpoint version of PR 1788.

The branches remain unmerged and MindRoom temporarily pins the exact reviewed mindroom-nio development commit.

## Problem

Classic Sync previously had two durable ingestion positions.

mindroom-nio persisted its transport token and recovery rows before MindRoom finished admitting the response.

MindRoom separately persisted the last token whose cache writes, source obligations, and lifecycle effects had completed.

A crash or local failure could therefore leave nio at `T2` while MindRoom trusted only `C1`.

Coordinating those positions required startup phases, recovery-generation handoffs, and special cases for cache failure, `M_UNKNOWN_POS`, transport switching, and tokenless history.

Agno sessions are downstream projections and must not participate in transport commitment.

## Ownership

| State | Owner | Role |
| --- | --- | --- |
| Matrix `next_batch` in flight | nio memory | Request position for the current Classic loop |
| Cache-certified Classic checkpoint | MindRoom | Sole durable Classic ingestion commit |
| Exact callback obligations | MindRoom | Durable retry after source admission |
| Sliding recovery rows | nio | Sliding-only recovery implementation |
| Agno session | Agno through MindRoom | Rebuildable downstream projection |

## Decision

MindRoom creates Classic clients with `store_sync_tokens=False` and `backfill_persist_recovery=False`.

nio parses and backfills one Classic response without durably committing its cursor or recovery generation.

MindRoom durably writes the event cache, admits exact source obligations, completes response-owned lifecycle effects, and then writes the checkpoint.

Only that MindRoom checkpoint acknowledges the Matrix response.

If any prerequisite fails, MindRoom asks nio to discard the uncommitted in-memory world and starts the next Classic loop from the retained checkpoint.

The reset clears room state, invited-room state, recovery gaps, pending and completed event markers, Sliding request caches, and transient cursors.

The reset retains Olm and encrypted-room persistence because replaying the same encrypted event against that transport state is idempotent.

The next Classic request uses `full_state=True` so the cleared nio room model is reconstructed before callbacks run.

At Classic startup, MindRoom removes legacy nio cursor, recovery, and Sliding-window rows left by older versions or a previous transport mode.

This cleanup is a migration boundary and is not another ongoing checkpoint protocol.

## Sliding Sync

MindRoom creates Sliding clients with `store_sync_tokens=False` and `backfill_persist_recovery=True`.

Sliding keeps its persisted recovery lane because its window and position semantics cannot be reconstructed from the Classic checkpoint model.

Classic never resumes from that lane, and entering Classic clears its residue before requesting data.

## Crash Semantics

| Failure point | Result |
| --- | --- |
| Before MindRoom admission | The checkpoint is unchanged and Matrix replay is requested |
| During partial admission | Replay is absorbed by event-ID and obligation-ID idempotency |
| After admission but before checkpoint write | The checkpoint is unchanged and replay is harmless |
| After checkpoint write | Every required source effect is already durable |
| During Agno execution | The Matrix checkpoint is unaffected and the durable obligation can retry |
| During transient reset | The old checkpoint remains authoritative and the next supervisor loop resets again |

`M_UNKNOWN_POS` clears the MindRoom checkpoint, resets nio's transient world, and performs a tokenless full-state rebuild.

A complete tokenless initial response is a valid baseline even when a room timeline is limited because it is a snapshot rather than an incremental gap.

## Explicit Boundary

The design provides at-least-once replay after MindRoom admission, not exactly-once Matrix delivery.

An event can still be lost if MindRoom never admitted it and Matrix omits it from every later replay or initial snapshot.

Eliminating that boundary would require another server-side acknowledgement or a second durable pre-admission journal, which would recreate the ownership problem.

## Removed Design

There is no durable Classic token in nio for MindRoom bots.

There is no startup comparison between a nio token and a MindRoom token.

There is no persisted Classic recovery generation to drain before replay.

There is no deferred rewind flag or cross-response recovery handoff in `SyncCacheTrust`.

There is no Agno influence on the Matrix transport cursor.

## Verification

The nio contract tests prove that reset drains callbacks already started, clears replay-suppression and room state, and permits the same event to be admitted on replay.

The migration test proves that legacy cursor, pending-event, gap, and Sliding-window rows are removed atomically.

The MindRoom continuity tests cover cache failures, callback-admission failures, cancellation, loop exit before response callbacks, `M_UNKNOWN_POS`, cold startup, room-member baselines, and Sliding-to-Classic residue.

The cross-repository contract test proves an unrecovered real nio gap is rejected, reset, replayed from MindRoom's checkpoint, and certified only after the replay succeeds.

Production rollout requires releasing mindroom-nio first and replacing the temporary Git pin with that released version.
