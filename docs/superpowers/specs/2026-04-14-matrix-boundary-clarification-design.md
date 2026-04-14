# Matrix Boundary Clarification Design

## Goal

Clarify the remaining Matrix conversation boundaries so thread identity, reply anchoring, cache reads, and outbound cache bookkeeping each have one obvious meaning.
Reduce future bug clusters by deleting overloaded concepts instead of adding more wrappers or flags.

## Why This Is Needed

The branch is already directionally better than `origin/main`.
Explicit thread handling is clearer, reply-chain thread inference is gone, and the cache no longer tries to self-heal in complicated ways.
The remaining review issues are now clustered around three soft seams.

The first seam is thread identity versus reply and relation anchoring.
`safe_thread_root` still mixes “what event is this about?” with “what conversation is this in?”
That is why edits, reactions, and references against a plain reply can still leak into `resolved_thread_id` and `session_id`.

The second seam is advisory outbound cache bookkeeping versus blocking room ordering.
The public contract says post-send cache writes are advisory and must fail open.
The implementation still awaits room-ordered queue work after the homeserver already accepted the send, edit, or redaction.

The third seam is normal advisory cache reads versus dispatch-safe reads.
`allow_durable_cache` currently controls too many things at once.
It is being used to decide healthy durable-cache reuse, stale-cache fallback, dispatch-path safety, and post-lock refresh behavior.

These are not random bugs.
They are repeated leaks from overloaded concepts.

## Design Principles

Do not add another generic collaborator layer just to hide the current ambiguity.
Do not add another options bag or policy object if a method name can express the contract directly.
Prefer splitting one overloaded concept into two explicit concepts over threading another boolean through the same call stack.
Prefer deleting fallback behavior over preserving it with another branch.
Keep the current module structure unless a move directly reduces semantic ambiguity.

## Non-Goals

Do not redesign the whole Matrix cache subsystem again.
Do not reintroduce repair tracking, generation logic, or reply-chain traversal.
Do not replace the existing write coordinator with a new queueing abstraction.
Do not broaden behavior for plain replies or non-thread-aware clients.

## Smell 1: Thread Identity Versus Reply Anchors

### Current Problem

`EventInfo.safe_thread_root` currently carries relation-target information for `m.replace`, `m.annotation`, and `m.reference`.
`ConversationResolver.build_message_target()` forwards that field into `MessageTarget.resolve()`.
`MessageTarget.resolve()` then uses it to build `resolved_thread_id` and `session_id`.

That means a relation event that targets a plain reply can still create thread identity.
This violates the explicit-thread-only model.

### Target Model

Separate these meanings completely.

- `thread_id` means explicit conversation thread identity.
- `thread_start_root_event_id` means “if this event is a room-root reply target and thread mode wants to start a new thread, use this root.”
- `reply_to_event_id` means immediate reply UX only.
- `relation_target_event_id` means the low-level event a reaction, edit, or reference points at.

`MessageTarget.resolve()` must derive `resolved_thread_id` only from:
- explicit `thread_id`, or
- explicit `thread_start_root_event_id`

It must never derive `resolved_thread_id` from `reply_to_event_id` or from relation targets.
`session_id` must follow the same rule.

### Result

Plain replies, edits on plain replies, reactions to plain replies, and references to plain replies all stay non-threaded unless they already carry explicit `m.thread`.
The system keeps reply UX without smuggling thread identity through relation metadata.

## Smell 2: Advisory Bookkeeping Versus Blocking Delivery

### Current Problem

After Matrix accepts an outbound send, edit, or redaction, the code still awaits room-ordered cache bookkeeping.
That means advisory cache maintenance can still add latency to a successful delivery path.
It also means `CancelledError` can still escape unless every caller treats advisory bookkeeping like part of delivery.

### Target Model

Make the contract literal.

- Matrix delivery success returns success immediately.
- Post-send cache bookkeeping is scheduled separately through the existing room write coordinator.
- Failures and cancellations in that bookkeeping are logged and dropped.
- Callers do not await advisory bookkeeping.

This is not a new queueing design.
It is just a change in how the existing queue is used.

### API Shape

The public boundary should make the contract obvious.
Use notify-style names for outbound advisory bookkeeping instead of record-style names that imply synchronous completion.

Examples:
- `notify_outbound_message(...)`
- `notify_outbound_redaction(...)`

The exact names can be refined during implementation, but the important point is that the public method no longer returns the result of the cache write.

### Result

Successful sends, edits, and redactions stop waiting on local cache bookkeeping.
The public contract and runtime behavior finally match.

## Smell 3: One Boolean Controls Multiple Cache Policies

### Current Problem

`allow_durable_cache` currently covers:
- healthy durable-cache reuse
- stale-cache fallback after homeserver failure
- dispatch-path safety
- post-lock refresh behavior

This makes every new caller risky.
A path that should bypass durable cache can still re-enter stale-cache fallback through a lower layer.
A path that should use advisory durable cache can accidentally become dispatch-sensitive.

### Target Model

Replace the overloaded boolean at the public boundary with explicit read entrypoints.

Public cache reads should become:
- `get_thread_snapshot(...)`
- `get_thread_history(...)`
- `get_dispatch_thread_snapshot(...)`
- `get_dispatch_thread_history(...)`

The first two are normal advisory reads.
They may use healthy durable cache and may fall back to stale cache if homeserver refresh fails.

The dispatch-prefixed methods are strict.
They must not use durable cache reuse.
They must not fall back to stale durable cache.
They must refetch or fail.

Implementation may still share a private helper internally to avoid duplication.
That helper must not be exposed as another policy object or options bag.

### Result

The call site states intent directly.
No caller has to remember what a boolean permits.
Dispatch safety becomes an explicit API contract instead of a convention.

## File-Level Design

### `src/mindroom/matrix/event_info.py`

Remove `safe_thread_root`.
Keep relation target metadata only as relation metadata.
Add an explicit field only if needed for “start a new thread under this root event.”
Do not let relation-target metadata imply thread identity.

### `src/mindroom/message_target.py`

Change `MessageTarget.resolve()` so it accepts explicit thread identity and explicit thread-start identity separately.
Do not let reply anchors or relation targets contribute to `resolved_thread_id`.

### `src/mindroom/conversation_resolver.py`

Stop constructing thread identity from relation metadata.
Use explicit dispatch cache methods in dispatch paths.
Use normal cache methods in non-dispatch callers.
Keep resolver logic direct and local.
Do not insert another resolver-layer policy object.

### `src/mindroom/matrix/conversation_cache.py`

Expose the four explicit read entrypoints.
Rename outbound advisory write-through methods to notify-style names.
Those methods schedule bookkeeping and return immediately.
The cache facade should own the contract boundary and keep the implementation detail hidden.

### `src/mindroom/matrix/cache/thread_reads.py`

Replace boolean-driven public policy with explicit dispatch and non-dispatch methods.
Use one small internal helper only if it clearly removes duplication.
Keep dispatch reads strict and keep normal reads advisory.

### `src/mindroom/matrix/client.py`

Keep durable-cache and homeserver fetch behavior, but split it by explicit read mode instead of boolean branching across many callers.
Durable-cache read failures continue to fail open for normal reads.
Dispatch reads do not return stale durable history.

### `src/mindroom/matrix/cache/thread_writes.py`

Keep the existing room-ordered write sequencing.
Stop exporting synchronous semantics for outbound advisory bookkeeping.
Move any necessary logging to the scheduled task boundary so caller paths stay clean.

### Outbound Callers

Update:
- `src/mindroom/hooks/sender.py`
- `src/mindroom/delivery_gateway.py`
- `src/mindroom/streaming.py`
- `src/mindroom/custom_tools/matrix_api.py`
- `src/mindroom/custom_tools/matrix_message.py`
- `src/mindroom/custom_tools/subagents.py`
- `src/mindroom/scheduling.py`
- `src/mindroom/thread_summary.py`
- `src/mindroom/matrix/stale_stream_cleanup.py`
- `src/mindroom/matrix/client.py`
- `src/mindroom/bot.py`

These callers should treat advisory cache bookkeeping as detached notification, not part of successful delivery.

## Why This Is Better Than More Indirection

This design does not add another generic router, coordinator, or policy object.
It removes ambiguity where the bugs actually come from.

The improvement is not “more layers.”
The improvement is:
- fewer meanings per field
- fewer meanings per method
- fewer meanings per flag

That is the kind of simplification that lowers future bug risk.
Another abstraction layer would only help if it shrank the public contract.
Right now the bigger win is to make the existing contracts honest.

## Testing Strategy

Add targeted regressions for:
- edit, reaction, and reference events against plain replies not affecting `resolved_thread_id` or `session_id`
- outbound notify methods not blocking successful send paths
- outbound notify methods swallowing `CancelledError`
- dispatch preview and post-lock refresh using strict dispatch reads
- normal non-dispatch callers still using advisory durable-cache reads and stale-cache fallback when appropriate

Verify the two largest rename and fallout surfaces explicitly before implementation:
- `rg -n "record_outbound_message\\(|record_outbound_redaction\\(" src tests`
- `rg -n "safe_thread_root=|\\.safe_thread_root\\b" src tests`

Delete or rewrite tests that only prove the old overloaded semantics.
Prefer end-to-end contract tests over shape tests.

## Success Criteria

Thread identity is derived only from explicit thread metadata or explicit thread-start behavior.
Reply anchors and relation targets no longer affect thread identity.
Successful outbound delivery does not await advisory cache bookkeeping.
Dispatch reads use strict APIs without durable-cache reuse or stale-cache fallback.
Normal reads use advisory APIs without dispatch-specific flags.
The code is clearer because the overloaded meanings are gone, not because they were hidden behind more wrappers.
