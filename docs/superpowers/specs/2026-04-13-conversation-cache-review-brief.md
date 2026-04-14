# Conversation Cache Review Brief

**Date:** 2026-04-13.

**Status:** Updated review brief reflecting the current branch direction.

**Audience:** Another agent reviewing whether the current `conversation_cache` direction genuinely reduces complexity.

## Purpose

This note is not asking whether the work should be split into multiple PRs.
This note is asking whether the current direction is genuinely improving the code and what the next cleanup step should be.
The main target of review is `src/mindroom/matrix/conversation_cache.py`.

## Short Version

The current direction is mostly right.
MindRoom should have one public Matrix conversation boundary and one durable event store.
The latest corrective changes moved several important responsibilities into better owners.
The implementation is still too hard to reason about because one file owns too many different kinds of behavior.
The goal is not to split a large file for cosmetic reasons.
The goal is to give each kind of truth and each kind of state transition one obvious owner.
Durable repair obligations should stay durable.
Only process-local reuse and locking state should remain in memory.

## What Is Good About The Current Direction

- `mindroom.matrix` is becoming the only public owner of Matrix conversation cache policy.
- Callers outside `mindroom.matrix` are moving away from private `_EventCache` imports.
- Thread freshness and repair semantics are being made explicit instead of remaining implicit side effects.
- Runtime ownership of cache services is getting clearer.

## What Now Appears To Be In The Right Place

These points no longer look hypothetical.
They are largely the direction of the current code.

- Durable event-to-thread membership now belongs in `_event_cache.py`.
- Durable unresolved lookup-repair obligations now belong in `_event_cache.py`.
- Durable thread-level repair-required state now belongs in `_event_cache.py`.
- Process-local generation tokens and per-thread locks now belong in `thread_cache.py`.
- Call sites are starting to use the public conversation facade for latest-thread-event resolution instead of reaching into lower-level helpers.
- Raw point-event semantics were corrected again in `matrix_api.get_event`, which reinforces that raw and visibility-normalized reads should remain explicit concepts.

## What Is Still Hard To Reason About

`MatrixConversationCache` currently mixes multiple concerns that change for different reasons.

- Raw event read-through.
- Thread snapshot and full-history read policy.
- Freshness state transitions.
- Live event mutation handling.
- Sync timeline mutation handling.
- Reply-chain invalidation side effects.
- Event projection semantics such as raw event versus latest visible edited event.

The file is large because it is acting as both public facade and policy engine and mutation coordinator and partial state machine.
The main problem is not file length by itself.
The main problem is that too many different invariants are enforced in one place by open-coded interactions.

## Review Question

Given the current ownership model, would the system become easier to reason about if we kept one public facade but split policy underneath it by kind of state transition.

## Current Direction And Next Step

Keep `MatrixConversationCache` as the single public boundary.
Treat the current ownership model as mostly correct.
The next step is to make `MatrixConversationCache` a thinner orchestration facade instead of the place where every policy detail lives.

### 1. Durable Event Store

File: `src/mindroom/matrix/_event_cache.py`.

- Owns SQLite schema and primitive CRUD over normalized Matrix events.
- Owns durable event-to-thread membership.
- Owns durable edit and redaction persistence details.
- Owns durable repair obligations that must survive restart.
- Owns durable unresolved lookup-repair candidates.
- Owns durable thread-level repair-required markers.
- Does not own freshness policy.
- Does not own repair policy.
- Does not decide whether a caller should trust cached data.
This ownership now appears directionally correct in the current branch.

### 2. Write Ordering

File: `src/mindroom/matrix/_event_cache_write_coordinator.py`.

- Owns same-room write serialization only.
- Does not own mutation semantics.
- Does not own freshness or repair policy.

### 3. Process-Local Thread Runtime State

Suggested home: `src/mindroom/matrix/thread_cache.py`.

- Owns per-thread generation tokens.
- Owns the per-thread async lock used by both reads and mutations.
- Owns only process-local resolved thread history entries.
- Owns TTL and LRU behavior for resolved payloads.
- Does not own durable repair-required state.
- Does not own durable unresolved lookup obligations.
- May keep small helper metadata needed to make post-lock reuse safe, but not restart-relevant obligations.
This also now appears directionally correct in the current branch.

### 4. Thread Read Policy

Suggested file: `src/mindroom/matrix/thread_reads.py`.

- Owns snapshot versus full-history read behavior.
- Owns incremental refresh behavior.
- Owns authoritative repair checks.
- Owns the rule for when repair can clear dirty state.
- Consumes durable repair obligations from `_event_cache`.
- Consumes process-local generation, lock, and resolved payload reuse state from `thread_cache`.
- Does not own either store directly.
This is the main split that still appears worth doing.

### 5. Thread Write Policy

Suggested file: `src/mindroom/matrix/thread_writes.py`.

- Owns live append handling.
- Owns live redaction handling.
- Owns sync timeline thread-affecting mutation handling.
- Owns the single mutation contract for success, degraded failure, and repair-required outcomes.
- Triggers reply-chain invalidation through one explicit side-effect interface.
- Writes durable repair obligations through `_event_cache` instead of keeping them in memory.
This is the other main split that still appears worth doing.

### 6. Sync Timeline Collection

Suggested file: `src/mindroom/matrix/sync_timeline.py`.

- Owns pure helpers that collect and group sync timeline updates.
- Contains no read policy.
- Contains no freshness policy.
- Contains no SQLite write policy.
This split is optional but likely helpful if sync helpers keep growing.

### 7. Point Event Semantics

This remains the place where ownership must stay explicit.

- A raw event fetch and a visibility-normalized event fetch are not the same API.
- The code should expose those as separate concepts.
- `matrix_api.get_event` should call a raw event API unless its contract is intentionally changed and documented.
- If the system wants a latest-visible-event helper, that helper should be named explicitly instead of being hidden behind generic `get_event`.

## Why This Reduces Complexity Instead Of Just Moving It

This design reduces complexity only if it reduces the number of places that can mutate the same state.

Today the hardest code to reason about is not where durable state lives.
That part is getting better.
The hardest part is that read policy, write policy, sync handling, and side-effect glue are still concentrated in one file.
If durable state and process-local state keep their current owners, then reads and writes can become clients of a smaller policy surface instead of partially reimplementing one large coordinator.

The expected simplifications are:

- One owner for durable repair obligations.
- One owner for process-local generation and lock state.
- One owner for the rule that decides when a repair is authoritative enough to heal a thread.
- One public facade that composes those owners instead of encoding all of their internals.

That is a real reduction in complexity because each bug should have one obvious owner instead of three plausible owners.

## Change Budget

This refactor should not add a large amount of new code.
The expected outcome is that `conversation_cache.py` gets smaller and policy is redistributed into narrower modules without materially increasing total `src/` LOC.
Small net growth is acceptable when it buys sharper ownership or removes duplicated policy.
Large net LOC growth is a design smell and requires explicit justification.
If the split adds substantial new scaffolding, wrappers, or coordination layers without deleting comparable complexity from `conversation_cache.py`, then it is not meeting the goal of this refactor.

## Invariants That Should Hold After The Refactor

- `_event_cache` is the only durable source of Matrix conversation truth.
- `_event_cache` also owns durable repair obligations that must survive restart.
- Resolved thread history is disposable and never authoritative.
- `thread_cache` owns only process-local generation, lock, and resolved payload reuse state.
- Every thread-affecting mutation goes through one shared mutation contract.
- Every reuse-versus-repair decision happens under the same per-thread lock.
- Repair clears dirty state only after an authoritative refill.
- Point-event APIs declare whether they are raw or visibility-normalized.
- Callers outside `mindroom.matrix` do not import private cache modules.
- Restarting a bot against rebuilt runtime support cannot reuse stale in-memory conversation state.

## Non-Goals

- This proposal is not asking to add another durable store.
- This proposal is not asking to change user-facing Matrix semantics unless a behavior change is made explicit and documented.
- This proposal is not asking for generic abstractions that hide Matrix-specific rules.
- This proposal is not asking for a split that only renames helpers without clarifying ownership.

## Specific Things To Challenge In Review

- Is `conversation_cache.py` still doing too many jobs even after the recent ownership corrections.
- Is `thread_cache.py` still the right name if it owns generation and locking as well as resolved payload reuse.
- Should reply-chain invalidation stay inside thread write policy or be pushed to an explicit side-effect adapter.
- Should the public facade expose separate raw and visible point-event APIs.
- Is any proposed module still doing more than one job.

## What A Good Review Response Should Say

A useful review should answer all of the following.

- Does this split reduce the number of places that can mutate the same state.
- Does each proposed module have one clear reason to change.
- Does the design preserve the existing architectural direction of one public Matrix conversation boundary.
- Does the current branch already place durable and process-local state under the right owners.
- Would this design have made bugs like stale thread reuse or `matrix_api.get_event` semantic drift easier to catch.
- Which part of the proposal still feels over-designed or under-specified.

## Recommendation

Keep the current ownership direction.
Do not reopen the question of where durable repair state lives unless there is a stronger alternative than `_event_cache`.
Focus the next cleanup on thinning `conversation_cache` by extracting read policy, write policy, and possibly sync collection helpers.
Do not accept a split that only redistributes helpers without reducing shared mutable state or reducing the policy load inside `conversation_cache.py`.
