# Plain Reply Thread Inheritance Design

## Goal

Preserve sensible threaded behavior for bridges and degraded clients without restoring the old reply-chain inference system.
If a plain `m.in_reply_to` reply targets an event already known to belong to explicit thread `T`, MindRoom should treat the new reply as belonging to `T`.

## Why This Is Needed

The current branch made explicit `m.thread` the only source of thread identity.
That removed a large bug surface, but it also created a bad compatibility gap.
When a bridge or limited client sends a plain reply to a message inside an existing thread, MindRoom currently falls out of the thread and continues as a room-level reply chain.
That behavior is hard to explain and produces mixed thread and room conversations.

## Non-Goals

Do not restore recursive reply-chain walking.
Do not infer a conversation root from an arbitrary reply chain.
Do not create a new thread root from a plain reply.
Do not add a new cache repair or preservation system.

## Target Rule

Use explicit `m.thread` when it is present.
Otherwise, if an event is a plain `m.in_reply_to` reply and its direct reply target already belongs to explicit thread `T`, inherit `T`.
Otherwise, keep the event room-level.

This is single-hop inheritance only.
MindRoom must inspect only the direct reply target.
MindRoom must not walk to a reply target of a reply target, or deeper.

## Behavioral Matrix

Top-level room message with no relations stays a room message until thread mode explicitly starts a new thread under it.
Plain reply to a normal room message stays room-level.
Plain reply to an explicit thread root inherits that thread.
Plain reply to an explicit thread reply inherits that same thread.
Explicit thread reply stays in its explicit thread.
Plain reply never becomes a new thread root.

## Thread History Behavior

Single-hop inheritance must affect both routing and local thread history.
If MindRoom promotes a plain reply into thread `T`, later thread reads for `T` should be able to include that promoted reply.
This keeps the agent view coherent across bridged or degraded clients.

The promotion is local behavior, not a claim that the homeserver or Matrix clients will treat the event as a native thread event.
Thread-aware clients may still render those plain replies as ordinary replies.
MindRoom should still keep the conversation coherent for routing, summaries, tags, and agent context.

## Failure Behavior

If the direct reply target cannot be fetched or its thread membership cannot be determined, fail open to room-level behavior.
Do not guess.
Do not invalidate unrelated thread state just because a plain reply could not be classified.

## Implementation Boundaries

`ConversationResolver` should own the single-hop inheritance decision.
It already has the right cache and event lookup seams for direct reply-target inspection.
`EventCache` and thread write paths should persist promoted event-to-thread membership once MindRoom knows it.
Thread utilities that normalize user-selected events to a canonical thread root should use the same single-hop rule so tags and summaries stay consistent.

## File-Level Impact

`src/mindroom/conversation_resolver.py` should add direct-reply inheritance to explicit thread resolution.
`src/mindroom/matrix/conversation_cache.py` and `src/mindroom/matrix/cache/event_cache.py` should provide or persist the minimal event-to-thread membership needed for later reads.
`src/mindroom/matrix/cache/thread_writes.py` should preserve promoted membership when live or sync events arrive.
`src/mindroom/thread_tags.py`, `src/mindroom/custom_tools/thread_tags.py`, and `src/mindroom/custom_tools/thread_summary.py` should normalize plain replies to threaded events the same way.

## Why This Is Better Than Restoring Reply Chains

The old reply-chain system tried to infer conversation identity from arbitrary chains of replies.
That created deep, ambiguous behavior and a large bug surface.
This design keeps thread identity mostly explicit while restoring the one compatibility rule that users actually expect.
It is a small and explainable exception instead of a second conversation model.
