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

After the checkpoint write, the same cancellation-drained publication step synchronously acknowledges that exact token in nio.

nio makes a token acknowledgeable only after all internal response processing succeeds, so an old or partially advanced cursor cannot clear dirty state from a failed response.

If nio suppresses an ordinary same-token response as a clean no-op, MindRoom may republish its continuity record but skips acknowledgement because no transient state was staged.

nio's acknowledgement state is volatile and can only report that a reset is required, so it is not a second durable cursor authority.

If any prerequisite fails, MindRoom asks nio to discard the uncommitted in-memory world and starts the next Classic loop from the retained checkpoint.

The reset clears room state, invited-room state, recovery gaps, pending and completed event markers, Sliding request caches, and transient cursors.

The reset first waits for non-sync membership cleanup and every active or queued room-state operation, so it never clears a room behind an operation using an older per-room gate.

The reset retains Olm and encrypted-room persistence because replaying the same encrypted event against that transport state is idempotent.

The next Classic request uses `full_state=True` so the cleared nio room model is reconstructed before callbacks run.

Transient sync errors retain the initial cursor, filter, and full-state request until one successful response completes the rebuild.

The first response after reset bypasses same-token suppression because Matrix tokens are opaque and a valid full-state response may return the restored checkpoint unchanged.

Transport rebuild state is separate from application first-sync readiness, so `bot:ready` remains once-only across replay.

A live rebuild from a certified checkpoint dispatches unseen state-block joins as well as catch-up timeline joins through the exact durable obligation path.

Encrypted sends attempted during a real rebuild receive nio's retryable recovery error and use the existing bounded delivery retry.

When the room cache is absent, the remote encryption-state check is passed through large-message preparation so an oversized JSON sidecar is encrypted before upload.

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
| After checkpoint write but before nio acknowledgement | Every required source effect is durable and an in-process loop exit conservatively rebuilds |
| After checkpoint write and nio acknowledgement | The clean room cache survives an ordinary transport restart |
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

The nio contract tests prove that reset drains callbacks and room-state operations already started, waits membership cleanup and queued room operations, clears replay-suppression and room state, and permits the same event to be admitted on same-token replay.

The acknowledgement tests prove that partially applied state and retained callbacks stay dirty, only the exact fully applied token clears it, and clean loop exit preserves the acknowledged room cache.

The migration test proves that legacy cursor, pending-event, gap, and Sliding-window rows are removed atomically.

The MindRoom continuity tests cover cache failures, callback-admission failures, cancellation-atomic acknowledgement, loop exit before response callbacks, clean transport restarts, `M_UNKNOWN_POS`, cold startup, room-member state-only replay, once-only readiness, and Sliding-to-Classic residue.

The cross-repository contract tests prove an unrecovered real nio gap is rejected, reset, replayed from MindRoom's checkpoint, and certified only after the replay succeeds, while transient sync errors retain the requested full-state rebuild.

The delivery tests prove oversized send and edit sidecars remain encrypted while nio reconstructs an encrypted room cache.

Production rollout requires releasing mindroom-nio first and replacing the temporary Git pin with that released version.
