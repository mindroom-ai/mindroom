# ISSUE-206 Thread Root Coalescing

## Summary

MindRoom merged two independent Matrix thread roots into one coalesced LLM turn after restart.
The merged turn replied in the newer root thread, so the `message_extras` thread received an answer that also handled an unrelated Gmail setup request.
This was not an LLM hallucination.
MindRoom explicitly built a prompt that told the model to treat both root messages as one quick-succession turn.

## Observed Incident

Room: `!l3DXzqtF5lrDRsNeOO:mindroom.chat`.
Gmail root: `$9mUisb-D4mms3q3H32R1CQ8ndKnTztg14VpYNamAZ8Y`.
Message extras root: `$w4K5mUPagiihrkWYWE0v5wBrcJJrCag05_xE32Bll5c`.
Reply event: `$xNdQDjhmacUgZwdJ4RB8P-vMc0vtcATf8E7Y3DPqtss`.

The Gmail root body was `Let's set up gmail in mindroom. you have oauth settings in .env right?`.
The message extras root body was `I have a new feature in mindroom where you can include message_extras when sending a matrix message. lets try it out`.
The reply was sent in thread `$w4K5mUPagiihrkWYWE0v5wBrcJJrCag05_xE32Bll5c`.

## Timeline

The Gmail root had Matrix timestamp `2026-05-10 23:32:36 PDT`.
The message extras root had Matrix timestamp `2026-05-11 22:20:40 PDT`.
MindRoom received both backlog events after restart around `2026-05-11 22:31:45 PDT`.
Both events were logged as `thread_id: null` at ingress because root events have no `m.relates_to`.
The coalescing gate enqueued `$9mUisb...` with `pending_count=1`.
The coalescing gate enqueued `$w4K5...` with `pending_count=2`.
The coalescing gate flushed both events together with `source_event_ids` containing both roots.
The generated response used `$w4K5...` as the reply target because coalesced batches choose the last pending event as the primary event.

## Evidence

The structured service log at `/home/basnijholt/.mindroom-chat/mindroom_data/logs/mindroom_20260512_052959.log` shows both roots entering the same room-level coalescing bucket.
The key evidence lines are the two `coalescing_gate_message_enqueued` entries with `thread_id: null` and the following `coalescing_gate_flush_started` entry with both source event IDs.
The LLM JSONL log at `/home/basnijholt/.mindroom-chat/mindroom_data/logs/llm_requests/llm-requests-2026-05-11.jsonl` confirms the prompt included both unrelated user requests.

The JSONL user message began:

```text
The user sent the following messages in quick succession. Treat them as one turn and respond once:

Let's set up gmail in mindroom. you have oauth settings in .env right?
I have a new feature in mindroom where you can include message_extras when sending a matrix message. lets try it out
```

This prompt text comes from `coalesced_prompt()` in `src/mindroom/coalescing_batch.py`.
The response target came from `primary_pending_event = ordered_pending_events[-1]` in `build_coalesced_batch()`.

## Root Cause

The coalescing key is `(room_id, thread_id, requester_user_id)`.
For root messages, Matrix does not mark the event as a thread root in the event itself.
A new root has no `m.relates_to`, so it is indistinguishable from a normal room-level message at the ingress boundary.
That means independent roots can legitimately share `thread_id=None` until a client or later reply uses them as thread roots.
The bug was that the coalescing gate allowed multiple room-level normal Matrix events to become one candidate batch.
That is unsafe at room scope because two unrelated top-level messages from the same sender share `thread_id=None`.
After downtime, Matrix delivered independent root messages together, and the room-level queue merged them before dispatch.

## Expected Behavior

Two independent root messages must not coalesce into one model turn merely because they arrived together after downtime.
Follow-up messages inside an existing thread may still coalesce by their canonical thread root.
The debounce delay may still be used for scheduling and for narrow media upload grace handling, but room-level normal candidate batches must be capped at one event.

## Test Plan

Start with a regression test that queues two room-level text events from the same sender.
The test fails on the previous code because both events flush as one coalesced batch and the generated prompt contains the quick-succession preamble.
Add a companion test that queues two thread-scoped text events with the same sender and thread key.
That companion test protects the intended same-thread quick-succession behavior.
Keep the production fix in the coalescing gate so prompt assembly remains a pure representation of whatever batch the gate intentionally selected.
