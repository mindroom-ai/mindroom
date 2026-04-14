# Matrix Conversation Cache Architecture Design

**Date:** 2026-04-13

**Status:** Approved for implementation.

## Problem

The current refactor has the right destination but not a complete boundary.
Cache lifecycle, cache coherence, homeserver fallback behavior, and post-response policy are still owned in multiple places.
That split ownership is what produced the review findings.

## Decision

MindRoom will use a required read-through Matrix conversation cache.
The cache is the primary read model for Matrix conversation data.
Homeserver reads remain available only as cache fill and repair mechanisms behind the cache service.
There is no degraded runtime mode where conversation features operate without a cache.

## Goals

- Make `mindroom.matrix.conversation_cache` the only public owner of conversation-cache policy.
- Treat the persisted event cache as the single durable source of truth for conversation data.
- Rebuild thread history, snapshots, reply-chain views, and latest-visible-event helpers from persisted events plus in-memory accelerators.
- Remove private cache implementation imports from callers outside `mindroom.matrix`.
- Make cache initialization mandatory for both standalone and orchestrated runtimes.
- Eliminate duplicate summary and handled-turn policy code from non-owner modules.
- Prevent stale reads after sync-write failures, edit events, or sync errors.

## Non-Goals

- This refactor does not redesign unrelated Matrix room state caching.
- This refactor does not introduce persisted thread-history snapshots as a second durable store.
- This refactor does not change user-facing message semantics outside the reviewed bugs.

## Architectural Principles

### 1. One public cache service

`MatrixConversationCache` is the only public service for Matrix conversation reads and conversation-cache writes.
Callers consume that service or an interface exported from that module.
Callers do not instantiate private cache backends directly.

### 2. One durable source of truth

Normalized persisted Matrix events are the only durable conversation store.
Everything else is derived.
The system may keep in-memory resolved-thread and reply-chain accelerators, but those accelerators are disposable and must be rebuildable from persisted events plus homeserver repair reads.

### 3. Read-through semantics

All event and thread reads first consult the persisted cache.
On miss, explicit repair, or freshness expiry, the cache service fetches from the homeserver, persists normalized results, and then serves the repaired data.
Normal callers never choose between cache and homeserver.

### 4. Required dependency

If the conversation cache cannot initialize, startup fails.
There is no fallback mode where callers receive `None` cache handles or advisory best-effort behavior.

### 5. Policy owned at one layer

Freshness, invalidation, versioning, repair triggers, and homeserver fallback rules live in `conversation_cache.py`.
Bot lifecycle code, API handlers, tool runtime context, and post-response effects may request reads or writes, but they do not reimplement cache policy.

## Module Responsibilities

### `src/mindroom/matrix/conversation_cache.py`

Public boundary.
Owns read-through behavior, thread-resolution policy, cache freshness policy, repair behavior, versioning, invalidation, and public type exports.

### `src/mindroom/matrix/_event_cache.py`

Private persistence backend.
Owns SQLite schema and primitive CRUD operations over normalized persisted events and relations.
Contains no caller-specific freshness policy.

### `src/mindroom/matrix/_event_cache_write_coordinator.py`

Private same-room write serialization.
Ensures write ordering only.
Contains no read policy.

### `src/mindroom/runtime_support.py`

Builds and initializes the required standalone cache service for standalone runtimes only.
Does not expose private cache backend details to callers.

### `src/mindroom/orchestrator.py`

Owns one shared cache service for orchestrator-managed bots.
Injects the public cache service into managed bots.
Does not let bots build shadow standalone caches first.

### `src/mindroom/bot.py`

Consumes injected runtime support.
Does not instantiate private event-cache backends eagerly.
Does not own duplicate thread-summary policy.

### `src/mindroom/post_response_effects.py`

Owns post-response summary scheduling.
Team and single-agent responses go through the same summary gate.

## Data Model

### Persisted durable data

- Normalized event payloads.
- Event-to-thread membership.
- Edit metadata required to resolve latest visible event state.
- Redaction effects reflected in persisted rows.

### In-memory derived data

- Resolved thread histories.
- Reply-chain caches.
- Thread versions.
- Repair-required markers for threads whose persisted state may be stale after failed writes.

The in-memory layer is disposable.
It must be safe to invalidate aggressively and rebuild.

## Freshness Model

### Immutable or explicitly invalidated event data

Individual event payloads are effectively permanent once written.
They only change through explicit edit and redaction handling.
They do not need TTL refresh by time alone.

### Derived thread views

Thread histories and snapshots are derived and may be cached in memory.
They are invalidated by new thread events, edits, redactions, or repair-required markers.
They may also expire by TTL for memory management, but TTL is not the primary correctness mechanism.

### Mutable non-conversation room state

Any separate cache for room membership or other mutable state should use explicit TTL or separate policy.
That is outside this refactor.

## Correctness Invariants

### Invariant 1

No public production code outside `mindroom.matrix` imports private cache modules.

### Invariant 2

A thread version only advances when the system has durably recorded the change, or when the thread is explicitly marked for repair.

### Invariant 3

If persisted cache state may be stale for a thread, the next read must force homeserver-backed repair.

### Invariant 4

Sync errors do not mark cached data as fresher.

### Invariant 5

Thread summaries are queued from one owner path only.

## Read Path

1. Caller requests an event, snapshot, or full thread history through `MatrixConversationCache`.
2. The service consults persisted cache state and disposable in-memory derived state.
3. If data is present and valid, the service serves it from cache.
4. If data is missing, stale, or marked for repair, the service fetches from the homeserver.
5. The service normalizes and persists fetched results.
6. The service rebuilds derived views from persisted data.
7. The caller receives the repaired result through the same interface.

## Write Path

### Live events

Live message, edit, and redaction updates flow through `MatrixConversationCache`.
The service persists the event update first, or marks the thread for repair if persistence fails.
Only then does it bump versions or invalidate in-memory derived views.

### Sync timeline

Sync processing collects candidate updates.
`MatrixConversationCache` persists them in room order.
Per-thread version bumps and invalidations happen after successful persistence.
If persistence fails for a thread-affecting update, that thread is marked repair-required so the next read bypasses freshness shortcuts and repairs from the homeserver.

## Error Handling

### Startup

Cache initialization failure is fatal.
Standalone and orchestrated startup both stop rather than degrade.

### Runtime persistence failure

Runtime write failure is not fatal to the whole process.
Instead, affected derived views are invalidated and the affected thread is marked repair-required.
Subsequent reads repair from the homeserver through the cache service.

### Runtime read failure

If persisted reads fail, the cache service attempts a repair read from the homeserver where possible.
If repair cannot succeed, the read fails explicitly rather than silently serving data known to be suspect.

## Boundary Changes Required

- Remove eager standalone cache construction from `AgentBot.__init__`.
- Stop instantiating `_EventCache` in API handlers.
- Export and inject only public cache interfaces from `conversation_cache.py`.
- Move duplicate summary helper usage back to `thread_summary.py`.
- Remove duplicate handled-turn metadata helper if an existing owner already provides it.

## Testing Strategy

### Unit tests

- Cache initialization is mandatory for standalone and orchestrated startup.
- `AgentBot` construction does not resolve cache paths or create private cache instances before runtime initialization.
- Sync-delivered edits that require original-event thread lookup invalidate resolved-thread state correctly.
- Failed sync persistence marks threads repair-required and forces homeserver-backed repair on next read.
- Sync errors do not update freshness clocks used to suppress repair reads.
- Team responses queue exactly one summary job through post-response effects.
- API schedule editing does not instantiate private cache backends directly.

### Integration tests

- Thread history remains correct across message, edit, and redaction flows.
- Cache misses populate persisted cache and future reads hit the cache path.
- Repairs repopulate persisted cache after forced write failures.

## Implementation Direction

This refactor should converge on a single cache boundary, not a patchwork of guardrails.
The correct fix is to remove duplicated ownership and make the architecture enforce the invariants above.
