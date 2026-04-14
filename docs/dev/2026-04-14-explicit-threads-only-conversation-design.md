# Explicit Threads Only Conversation Design

Last updated: 2026-04-14

## Goal

Remove reply-chain-to-thread inference from MindRoom.
Treat only explicit `m.thread` relations as threaded conversations.
Keep plain `m.in_reply_to` replies only as immediate reply targets for outbound UX.

## Product Rule

`m.thread` means thread conversation state.
`m.in_reply_to` without `m.thread` means plain reply only.
MindRoom must no longer walk plain reply chains to infer a conversation root.
MindRoom must no longer promote plain replies into thread context.
MindRoom must no longer cache or invalidate reply-chain traversal state.

## Why This Change

Reply-chain inference is now the largest remaining complexity hotspot in the Matrix conversation stack.
It still drives substantial logic in `matrix/reply_chain.py`, `conversation_resolver.py`, `thread_writes.py`, `thread_tags.py`, and a large specialized test surface.
This feature tries to preserve thread semantics for clients and bridges that do not send `m.thread`.
That behavior is costly, hard to reason about, and no longer aligned with the desired product model.
The simpler product model is that explicit threads are threads and plain replies are just replies.

## Current Problem Areas

`src/mindroom/matrix/reply_chain.py` is almost entirely dedicated to traversing `m.in_reply_to` chains, probing thread snapshots, merging reply-chain history with thread history, and caching traversal nodes and roots.
`src/mindroom/conversation_resolver.py` carries `ReplyChainCaches`, exposes reply-chain-derived conversation APIs, and still drives `requires_full_thread_history` through plain-reply preview paths.
`src/mindroom/matrix/cache/thread_writes.py` invalidates reply-chain caches on edits and redactions.
`src/mindroom/thread_tags.py` still canonicalizes thread roots by walking plain reply chains.
Many tests lock in behavior where plain replies inherit thread context, keep a synthetic stable root, or reuse reply-chain traversal caches.

## Target Architecture

Conversation identity becomes simpler.
Only `EventInfo.thread_id` and `EventInfo.thread_id_from_edit` define threaded context.
Plain replies keep their direct `reply_to_event_id` for outbound reply targeting.
Plain replies do not imply `is_thread=True`.
Plain replies do not produce `thread_id`.
Plain replies do not trigger thread-history reads.

`ConversationResolver` becomes an explicit-thread resolver.
For full-history reads, it returns thread history only for direct thread events.
For preview reads, it returns lightweight thread snapshots only for direct thread events.
For plain replies, it returns non-thread context with no history hydration and no deferred full-history marker.

`reply_chain.py` is removed.
There is no replacement traversal module.
The system stops maintaining process-local reply-chain node and root caches.

`thread_writes.py` stops invalidating reply-chain state on edits and redactions.
Its responsibilities shrink back to durable event cache mutation and thread invalidation only.

`thread_tags.py` stops walking plain reply chains to normalize a thread root.
Thread-tag operations should only accept explicit thread roots or explicit thread replies that already carry a real thread root in their event metadata.
If an event does not identify a real thread root, thread-tag helpers should return `None` or reject the request instead of inferring one.

## Behavioral Changes

An inbound plain reply without `m.thread` is no longer treated as part of a conversation thread.
If a user replies to a threaded message from a client that strips `m.thread`, MindRoom will treat that message as a plain reply in the room.
MindRoom may still answer with `m.in_reply_to` to the triggering event for local UX, but it will not route that response into the parent thread unless the incoming event itself is explicitly threaded.

Reply-chain preview and fallback logic disappear.
`requires_full_thread_history` remains only for direct-thread preview snapshots.
Plain replies no longer participate in that deferral path.

Coalescing and message targeting for plain replies remain immediate-event based.
The system should still visually reply to the source message when that is the desired output behavior.
It should not derive a synthetic thread root from that reply target.

## Files Expected To Shrink

`src/mindroom/matrix/reply_chain.py` should be deleted.
`src/mindroom/conversation_resolver.py` should lose the `ReplyChainCaches` field, reply-chain imports, plain-reply traversal paths, and related hydration branches.
`src/mindroom/matrix/conversation_cache.py` should lose reply-chain cache binding methods.
`src/mindroom/matrix/cache/thread_writes.py` should lose reply-chain cache getters, invalidation helpers, and redaction/edit invalidation paths that only exist for reply-chain state.
`src/mindroom/thread_tags.py` should simplify its normalization path.
Related tests in `tests/test_threading_error.py`, `tests/test_thread_mode.py`, `tests/test_preformed_team_routing.py`, `tests/test_thread_tags.py`, and other reply-chain-specific files should shrink significantly.

## Compatibility Stance

This intentionally drops compatibility behavior for non-thread-aware clients and bridges.
That is acceptable for this codebase.
The system should not add compatibility shims to preserve reply-chain-as-thread behavior.

## Error Handling

No new recovery mechanism is needed.
Removing reply-chain inference removes a large class of traversal failures, cycle handling, fallback history merging, and cache invalidation edge cases.
Thread reads continue to use the existing explicit-thread cache and homeserver fallback behavior.

## Testing Strategy

Delete tests that only prove reply-chain traversal, cycle handling, synthetic stable plain-reply roots, reply-chain cache invalidation, or plain-reply-to-thread promotion.
Keep and update tests that prove explicit `m.thread` behavior still works.
Add focused regressions that prove plain replies are now treated as plain replies.
Add coverage that direct-thread preview still uses snapshots and direct-thread full context still hydrates full history.
Add coverage that outbound plain replies still keep `m.in_reply_to` targeting without creating thread context.

## Success Criteria

There is no reply-chain traversal module in `src/`.
Conversation resolution no longer depends on `m.in_reply_to` chain walking.
Plain replies do not produce thread context.
Reply-chain caches and invalidation hooks are gone.
The remaining thread model is explicit, smaller, and easier to reason about.
