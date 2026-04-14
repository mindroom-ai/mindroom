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
- One thread-scoped freshness state per thread.
- A room-scoped candidate index for lookup failures whose thread cannot yet be resolved.

The in-memory layer is disposable.
It must be safe to invalidate aggressively and rebuild.
The per-thread freshness store and unresolved room-candidate index are bounded.

## Thread Freshness State

Each thread owns one in-memory freshness state record.
That state is the single mutable owner of:

- the current thread generation token exposed to callers,
- whether the thread requires authoritative repair,
- which lookup-failure event IDs have already been promoted into that thread,
- and the async lock used by both reads and mutations.

The resolved-thread cache entry is only a disposable value attached to that state.
Generation and repair state are not LRU-evicted and not reused within the process.
TTL applies only to resolved-thread entries, not to freshness identity.

When a lookup failure happens and the cache cannot yet resolve the affected thread, the event ID is stored only as a room-scoped candidate.
Reads do not treat that candidate as room-wide poisoning.
Instead, the next read for a concrete thread checks whether that candidate intersects the thread's cached source events.
Only matching candidates are promoted into that thread's freshness state and converted into a repair-required condition.

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

### Invariant 6

Thread-affecting cache mutations interpret persistence outcomes uniformly.
Successful local mutation means the persisted raw cache is usable.
Any failed or degraded mutation outcome marks the thread repair-required.

### Invariant 7

Repair-required state is only cleared after a verified successful homeserver-backed refill.
An empty or degraded fallback result does not count as repair success.

### Invariant 8

Resolved-thread cache reuse decisions observe generation, repair-required state, and promoted lookup repairs under the same per-thread lock that guards cache fill and reuse.

### Invariant 9

The branch remains Ruff-clean, formatter-clean, and test metadata reflects the current file layout.

### Invariant 10

Thread generation tokens are monotonic within the process and are never reused after LRU entry eviction.

### Invariant 11

Room-scoped lookup-failure candidates do not force unrelated threads down the full-history path.
Only threads whose cached source events intersect a candidate are promoted to repair-required.

### Invariant 12

Closing standalone runtime support discards the concrete support object and detaches it from the bot runtime so same-process restart rebuilds from the current config.
It also clears in-memory conversation-cache accelerators so no resolved thread history or pending lookup metadata crosses the restart boundary.

## Read Path

1. Caller requests an event, snapshot, or full thread history through `MatrixConversationCache`.
2. The service waits for any queued same-room cache writes that must precede a consistent thread read.
3. The service takes the per-thread freshness lock and consults persisted cache state plus the thread-scoped freshness state.
4. If room-scoped lookup-failure candidates exist, the service checks whether they intersect the requested thread and promotes only matching candidates into the thread state.
5. If data is present and valid, the service serves it from cache.
6. If data is missing, stale, or marked for repair, the service fetches from the homeserver.
7. The service normalizes and persists fetched results.
8. The service rebuilds derived views from persisted data.
9. The caller receives the repaired result through the same interface.

## Write Path

### Live events

Live message, edit, and redaction updates flow through `MatrixConversationCache`.
The service applies one shared mutation policy for thread-affecting writes.
The write result is classified as success, degraded failure, or exception.
Success means the persisted raw cache is usable for subsequent reads.
Any degraded failure or exception marks the thread repair-required and invalidates disposable derived state.
Versioning and invalidation behavior follows that shared policy instead of being open-coded at each call site.

### Sync timeline

Sync processing collects candidate updates.
`MatrixConversationCache` persists them in room order.
Per-thread version bumps and invalidations happen after successful persistence.
If persistence fails for a thread-affecting update, that thread is marked repair-required so the next read bypasses freshness shortcuts and repairs from the homeserver.
If a mutation cannot resolve the thread at all, the affected event ID is recorded as a durable lookup-repair obligation in the SQLite event cache.
That obligation is correctness state, not disposable cache metadata.
It remains until a concrete thread read can promote it to one thread-specific repair or an equivalent authoritative refill clears it.

## Error Handling

### Startup

Cache initialization failure is fatal.
Standalone and orchestrated startup both stop rather than degrade.

### Runtime persistence failure

Runtime write failure is not fatal to the whole process.
Instead, affected derived views are invalidated and the affected thread is marked repair-required.
Subsequent reads repair from the homeserver through the cache service.

### Runtime degraded persistence result

Primitive cache writes that return a non-exception failure signal are treated the same as runtime write failure.
They are not advisory success.
The cache service marks the thread repair-required and does not pretend the mutation produced a reusable persisted state.

### Runtime read failure

If persisted reads fail, the cache service attempts a repair read from the homeserver where possible.
If repair cannot succeed, the read fails explicitly rather than silently serving data known to be suspect.

### Runtime repair failure

If a forced repair read cannot produce a verified authoritative refill, the thread remains repair-required.
The system may still choose a conservative Matrix fallback relation such as the thread root for outbound sends, but it must not silently mark suspect history current.

### Runtime concurrent mutation

Concurrent thread-affecting reads and writes serialize through the same per-thread freshness lock.
If a thread-unknown lookup failure arrives during a read, the durable lookup-repair obligation remains pending until the cache has enough persisted event-thread membership to promote it.
Later post-lock freshness checks must still route the next use through the cache service instead of trusting raw generation equality alone.
The service does not cache an empty healed result just because one fallback path returned no events.
Repair completion requires proof that the homeserver-backed refill actually succeeded.

### Runtime outbound threading

MSC3440 fallback event selection is part of the same freshness boundary.
High-level delivery code must not read raw cached thread history directly from `matrix.client`.
Instead, outbound send paths must ask `MatrixConversationCache` for the latest visible thread event so the same room-write draining, repair-required checks, and per-thread locking rules apply before deciding which fallback event to reference.

## Enforcement Pass

This implementation pass finishes the architecture at the thread-cache seam.
It does not redesign the whole cache stack.
It enforces the chosen architecture everywhere that currently advertises the wrong pattern.

### Mutation contract

All thread-affecting cache writes in `conversation_cache.py` must route through shared helpers.
Those helpers own how boolean results, exceptions, version bumps, resolved-cache invalidation, and repair-required markers interact.
Live append, live redaction, sync append, and sync redaction paths do not encode their own partial variants of that policy.

### Repair contract

Homeserver repair paths must distinguish between:

- cache hit
- verified homeserver-backed refill
- degraded or failed refill

Only the verified refill path may clear repair-required state.
The repair path must not silently replace a broken thread with an empty cached thread after a transient homeserver failure.
Only the cache service may decide when outbound fallback threading can trust a cached tail event.

### Read-side concurrency contract

Resolved-thread cache reuse decisions must be made after acquiring the per-thread entry lock.
The code may not snapshot thread version or repair-required state before lock acquisition and then use that stale snapshot to decide whether to reuse a cached entry.

### Repo-policy contract

This pass also removes repo-level examples that would teach future agents the wrong standard.
That includes stale dependency metadata, lint violations, formatter violations, and any touched helper that encodes the wrong ownership or contract shape.
That also includes high-level callers importing low-level `matrix.client` fallback helpers when `conversation_cache` is already available.

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
- `matrix_api.get_event` returns the raw Matrix event payload and does not route through edited-view cache projection.
- Sync-delivered edits that require original-event thread lookup invalidate resolved-thread state correctly.
- Failed sync persistence marks threads repair-required and forces homeserver-backed repair on next read.
- Live thread mutations that receive degraded write results mark the thread repair-required through the same shared mutation contract.
- Sync errors do not update freshness clocks used to suppress repair reads.
- Forced repair reads do not clear repair-required after degraded homeserver fallback results.
- Resolved-thread cache reuse rechecks version and repair-required state after taking the per-thread entry lock.
- Durable unresolved lookup repairs are not silently evicted when many failures occur in one room.
- Outbound send paths use `conversation_cache` for latest-thread fallback selection instead of reading cached thread history directly from `matrix.client`.
- Team responses queue exactly one summary job through post-response effects.
- API schedule editing does not instantiate private cache backends directly.
- Dependency metadata and lint checks match the renamed private cache modules and touched files.

### Integration tests

- Thread history remains correct across message, edit, and redaction flows.
- Cache misses populate persisted cache and future reads hit the cache path.
- Repairs repopulate persisted cache after forced write failures.

## Implementation Direction

This refactor should converge on a single cache boundary, not a patchwork of guardrails.
The correct fix is to remove duplicated ownership and make the architecture enforce the invariants above.
