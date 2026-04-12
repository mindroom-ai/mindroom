# Prompt Cache Maximization Plan

Last updated: 2026-04-11
Owner: MindRoom backend
Status: Proposed

## Objective

Make warm follow-up requests reuse the entire previously sent prompt prefix on the provider side so that `request N+1` is effectively `request N + appended tail`.
The practical target is to drive warm-thread cache-read above 90% for Claude-on-Vertex conversations.

## Current Problem

The main system prompt is now stable enough that it is no longer the dominant blocker.
Real warm threads still miss the 90% target because the front of the user-visible prompt changes between turns.
The largest remaining sources of churn are prompt-time interruption rewriting and volatile context injected ahead of stable history.

## Evidence

The `openclaw` thread `!l3DXzqtF5lrDRsNeOO:mindroom.chat:$_1bczR4s98ZmeUqP872acla_LHxqT3Bmmff8Uvujgcw` reused cache after restart, which proves interruption does not inherently kill provider caching.
That thread still only achieved 83.0% cache-read on the first warm continuation and 70.4% on the next run.
The stored `input_content` for those two runs had common prefix 0 because the second run started with auto file memories while the first started with the interruption wrapper and interrupted draft.
The weaker `mindroom_dev` thread only achieved about 24.6% cache-read because the user-input prefix was rebuilt even more aggressively.

## Non-Negotiable Invariant

Every durable conversational fact that the model may need again must be represented in persisted history exactly once and then replayed verbatim.
Every volatile request-scoped fact must be appended only at the end of the current turn.
Nothing may rewrite older prompt-visible history during request preparation.

## Design Principles

- Prefer append-only request growth.
- Preserve persisted history byte-for-byte whenever possible.
- Keep volatile runtime context out of the stable prefix.
- Use metadata for status when the model does not need explicit text.
- If the model needs explicit text, persist one stable synthetic message instead of reconstructing dynamic wrappers later.
- Make the final prompt shape simple enough that exact prefix-extension is easy to test.

## Main Sources Of Prefix Churn

1. `execution_preparation.py` rewrites interrupted replies into a synthetic unseen-message wrapper.
2. `matrix/stale_stream_cleanup.py` injects a synthetic restart-resume note that later interacts with prompt-time rewriting.
3. Auto file-memory snippets can appear at the front of the latest user prompt.
4. Volatile enrichment such as location is currently rendered into the model-facing prompt body.
5. Attachment guidance and Matrix tool metadata are appended opportunistically rather than through one dedicated final-tail block.

## Desired Prompt Shape

The stable prefix should contain the system prompt and the persisted conversation history exactly as previously sent.
If a response was interrupted, the partial assistant reply should remain in history exactly as stored.
If the model needs explicit interruption context, one stable persisted synthetic note should follow the interrupted reply in history.
The current turn should then append one final volatile tail block containing request-scoped context such as location, auto memory, attachment guidance, and Matrix tool metadata.

## Workstream 1: Remove Prompt-Time Interruption Rewriting

Change `src/mindroom/execution_preparation.py` so it no longer prepends synthetic text such as `Messages since your last response` or `Your previous response was interrupted before completion`.
Stop relabeling replayed self-authored partial replies as `You (interrupted reply draft)`.
Replay the partial assistant reply verbatim from persisted history instead.
If unseen-message grouping is still needed for ordinary user messages, keep that logic narrowly scoped to unseen external messages and exclude special rewriting of self-authored partial replies.

## Workstream 2: Normalize Restart Continuation Handling

Review `src/mindroom/matrix/stale_stream_cleanup.py` and decide whether the restart note should remain a persisted synthetic message or become metadata-only.
If explicit continuation guidance is needed for model quality, keep one stable persisted synthetic note after the interrupted reply.
Do not depend on later prompt-building code to reinterpret restart state and regenerate a different wrapper.
Do not prepend any continuation-specific text ahead of replayed history.

## Workstream 3: Introduce One Dedicated Volatile Tail Block

Create one clear append-only place for volatile current-turn context.
That block should contain location, transient enrichment, attachment guidance, Matrix reply metadata, and other request-scoped hints.
This block must appear only at the end of the current turn.
Nothing from this block should be inserted ahead of stable replayed history.
Stable enrichment and volatile enrichment should be ordered deterministically inside this block.

## Workstream 4: Demote Auto File Memories Out Of The Stable Prefix

Treat auto file-memory output as volatile request context rather than durable conversation content.
Do not allow auto file memories to become the first text in the user prompt for one turn and disappear or move for the next turn.
Either append the file-memory snippet inside the dedicated volatile tail block or fetch it on demand through a tool.
If auto file memory must remain inline text, it must always be in the same position relative to the current user message.

## Workstream 5: Keep Voice And Attachments Stable

Voice and attachment handling should preserve stable attachment identifiers and deterministic ordering across turns.
Attachment guidance from `src/mindroom/attachments.py` should be emitted through the dedicated volatile tail block instead of ad hoc prompt appends.
The same principle applies to any future media hints.
Media itself is not the main cache breaker, but unstable textual guidance around media is.

## Workstream 6: Audit All Front-Of-Prompt Injectors

Audit every place that can modify the model-facing prompt after persisted history is known.
The primary modules are `src/mindroom/execution_preparation.py`, `src/mindroom/attachments.py`, `src/mindroom/hooks/enrichment.py`, `src/mindroom/inbound_turn_normalizer.py`, and any prompt builders called from `src/mindroom/ai.py` or `src/mindroom/teams.py`.
The audit rule is simple.
If a dynamic block is inserted before stable replayed history, move it.
If a historical block is rephrased or relabeled during request preparation, remove that transformation.

## Workstream 7: Add Exact Prefix Tests

Add regression tests that compare normalized provider payloads across adjacent turns.
The primary assertion should be that a warm continuation is an exact prefix extension of the previous request after ignoring only the moving `cache_control` marker.
Cover at least these cases.

- Normal threaded continuation.
- Interrupted assistant continuation.
- Service restart continuation.
- Voice message follow-up in the same thread.
- Attachment-bearing follow-up in the same thread.
- Threads with volatile enrichment such as location.

The tests should fail if any request-scoped block moves ahead of previously stable history.

## Workstream 8: Validate With Real Metrics

Keep using `scripts/testing/prompt_cache_review.py` with both JSONL and session DB data.
Use JSONL to verify prompt-shape invariants per `session_id`.
Use the session DB to verify actual `cache_read_tokens` and `cache_write_tokens`.
Warm-thread success criteria are:

- exact prefix-extension shape for the main model request in JSONL.
- warm-turn cache-read above 90% for real Claude-on-Vertex threads.
- interrupted and restarted threads staying above 90% instead of falling into the 70% range.

## Implementation Order

1. Remove prompt-time interruption rewriting in `execution_preparation.py`.
2. Simplify restart continuation semantics in `stale_stream_cleanup.py`.
3. Introduce a dedicated volatile tail block and route attachment, enrichment, and Matrix metadata through it.
4. Move auto file-memory snippets into that tail block.
5. Add exact-prefix regression tests.
6. Re-run live validation on voice, attachment, interrupted, and restarted threads.

## Success Criteria

The plan is complete when a restarted or interrupted Claude thread still looks like an append-only extension of the previous request and the DB reports warm-turn cache-read consistently above 90%.
If any durable history must still be transformed at request time, that transformation should be treated as a bug and justified explicitly.
