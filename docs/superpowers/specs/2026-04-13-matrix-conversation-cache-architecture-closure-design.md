# Matrix Conversation Cache Architecture Closure Design

**Status:** Proposed.
**Date:** 2026-04-13.
**Owner:** Codex.

## Purpose

Finish the Matrix conversation-cache refactor by closing the remaining authority and lifecycle seams instead of patching individual review findings.
The split into `thread_reads.py` and `thread_writes.py` already landed.
This design finishes the contracts those modules were meant to enforce.

## The Remaining Architectural Smell

The current code no longer has one giant `conversation_cache.py`, but it still has soft boundaries where it needs hard contracts.
The remaining bugs all come from one of these seams:

- outbound write-through still caches caller intent instead of the exact event content Matrix actually stored,
- advisory cache bookkeeping can still leak into authoritative delivery or read success,
- runtime-local conversation state is still owned by more than one object,
- some durable cache writes still bypass the room-ordered visibility contract,
- the post-lock authoritative reply phase still has split error normalization.

The refactor is therefore not “unfinished because the files are still ugly”.
It is unfinished because truth, visibility, and advisory bookkeeping are still not completely separated.

## Design Goals

- Preserve the current ownership map:
  - `_event_cache.py` owns durable event truth and durable repair obligations,
  - `thread_cache.py` owns process-local resolved-thread reuse,
  - `thread_reads.py` owns repair-aware thread reads,
  - `thread_writes.py` owns thread-affecting cache mutations,
  - `MatrixConversationCache` stays the public facade.
- Make cache write-through reflect the exact delivered Matrix payload.
- Make advisory cache bookkeeping fail open everywhere after successful Matrix delivery.
- Make room-idle waits a visibility barrier, not an exception transport.
- Make runtime restart clear all process-local conversation state.
- Make the post-lock authoritative reply phase succeed or fail through one normalized boundary.

## Non-Goals

- Do not migrate old event-cache schemas.
  Stale on-disk cache state is rebuildable and should be discarded rather than migrated.
- Do not reintroduce TTL pruning for durable `pending_lookup_repairs`.
- Do not collapse all thread reads into one API in this pass.
- Do not rewrite the whole Matrix client layer.

## Core Invariants

### 1. Delivered Event Truth

Every local send or edit that writes through to the conversation cache must use the exact content Matrix stored after `prepare_large_message(...)` or any later transport-time transformation.
Caching the pre-send draft is incorrect because it creates same-runtime divergence between the local cache and Matrix state.

This means `send_message()` and `edit_message()` need to return more than an event ID.
They need to return a small typed result that includes:

- `event_id`
- `content_sent`

All write-through call sites must use `content_sent`.

### 2. Advisory Cache Bookkeeping Must Fail Open

After a successful Matrix send, edit, or redact, advisory cache updates must never turn that operation into an application-level failure.
This is a public API contract, not a caller convention.

`record_outbound_message()` and `record_outbound_redaction()` therefore must be non-raising for post-delivery failures.
They should log and continue.
Callers should not need local `try/except` to preserve delivery semantics.

The same rule applies to room-idle waits.
The barrier exists to wait for visibility, not to inherit advisory queue exceptions from unrelated work in the same room.

### 3. One Runtime-State Reset Owner

Process-local conversation state must reset in one place on runtime shutdown or restart.
That includes:

- resolved-thread cache,
- process-local generation state,
- reply-chain caches.

If `ConversationResolver` continues to hold reply-chain caches, `MatrixConversationCache.reset_runtime_state()` must still be able to clear them through one explicit seam.
No process-local conversation cache should survive same-instance restart.

### 4. One Room-Ordered Visibility Contract

Any durable write that affects thread truth, thread membership, edit projection, or repair obligations must either:

- go through the room-ordered write coordinator,
or
- be explicitly classified as non-authoritative and prevented from mutating those indexes.

Point-event lookups are the current ambiguity.
They should not update authoritative thread or repair indexes outside the barrier.
They may cache raw event blobs, but not mutate thread-membership or repair-state truth opportunistically.

### 5. One Post-Lock Failure Boundary

The entire authoritative post-lock reply preparation phase must fail through one normalized exception path.
That includes:

- post-lock thread refresh,
- any after-lock prompt or payload hydration.

`TurnController` should not need to know which subphase raised.

## Module Responsibilities After Closure

### `_event_cache.py`

- durable event blob storage,
- durable thread membership mappings,
- durable edit mappings,
- durable repair obligations,
- no silent expiry of correctness obligations.

Point lookups may populate event blobs.
They must not create or mutate authoritative thread/edit/repair indexes outside the room barrier.

### `thread_reads.py`

- authoritative thread-history reads,
- repair-aware thread-history fallback selection,
- resolved-thread cache reuse,
- latest-visible-thread-event queries that are repair-aware,
- no direct mutation of durable truth outside explicit write seams.

### `thread_writes.py`

- live append and redaction handling,
- sync timeline thread-affecting persistence,
- local outbound send/edit/redaction write-through,
- reply-chain invalidation tied to thread-affecting mutations,
- advisory failure swallowing and logging for post-delivery bookkeeping.

### `conversation_cache.py`

- public facade and protocol,
- collaborator composition,
- runtime-state reset,
- thin coordination helpers only.

`conversation_cache.py` should stop acting as a private forwarding dump.
Anything left there should clearly belong to facade composition or public protocol surface.

## API Changes

### Delivered Matrix Event Result

Introduce a typed result for send and edit helpers.

```python
@dataclass(frozen=True)
class DeliveredMatrixEvent:
    event_id: str
    content_sent: dict[str, Any]
```

`send_message()` returns `DeliveredMatrixEvent | None`.
`edit_message()` returns `DeliveredMatrixEvent | None`.

This is the canonical post-transport artifact for:

- delivery gateway sends,
- delivery gateway edits,
- streaming sends and edits,
- hook sends,
- scheduled sends,
- summary sends,
- tool-authored threaded sends and edits.

### Fail-Open Outbound Cache API

`ConversationCacheProtocol.record_outbound_message()` and `record_outbound_redaction()` remain `-> None`, but their contract becomes enforced, not merely documented.
They must not raise on advisory failures after authoritative Matrix success.

## Testing Strategy

Add direct coverage for the remaining seams.

- large-message send write-through caches `content_sent`, not the pre-send draft,
- large edit write-through caches `content_sent`, not the pre-send draft,
- scheduled sends remain successful when advisory cache bookkeeping fails,
- room-idle waits do not inherit advisory post-send failures,
- post-lock thread-refresh failures route through `_finalize_dispatch_failure()`,
- runtime reset clears reply-chain caches as well as resolved-thread state,
- point event lookups do not mutate authoritative thread/relation indexes outside the barrier.

Use `pytest -n auto --no-cov`.

## Migration And On-Disk Cache Policy

This pass may require Matrix event-cache schema changes.
There is intentionally no migration story for old cache contents.
The cache is rebuildable advisory state.

If the on-disk schema is stale, the code should reset it and rebuild through normal usage.
This should be stated plainly in comments near the schema/version handling so future reviewers do not assume migration is intended.

## Relationship To Existing Docs

This design supersedes the remaining “closure” work implied by:

- `docs/superpowers/specs/2026-04-13-matrix-conversation-cache-response-path-simplification-design.md`
- `docs/superpowers/plans/2026-04-13-matrix-conversation-cache-architecture.md`
- `docs/superpowers/plans/2026-04-13-matrix-conversation-cache-response-path-simplification.md`

Those documents describe earlier phases that have partly landed.
This document is the authority for the final seam-closing pass.
