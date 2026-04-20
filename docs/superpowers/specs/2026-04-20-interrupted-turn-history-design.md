# Interrupted Turn History Design

## Status

Proposed and approved for planning.

## Problem

MindRoom currently treats completed turns as canonical Agno replay history and treats interrupted self-turns as a special Matrix-side prompt overlay.

That split makes the read path harder to understand and causes interrupted tool state to depend on visible Matrix message reconstruction.

It also creates a mismatch between what the agent most needs to see next and what the trusted persisted history model actually contains.

## Goals

Interrupted self-turns should be visible to the next top-level turn in the same session.

The next turn should see partial assistant text and trusted tool-call state without depending on Matrix `io.mindroom.tool_trace` replay.

The canonical history model should be easier to explain as one trusted persisted lane.

The prompt prefix should change once when an interruption happens and then become stable again.

## Non-Goals

This design does not change how completed turns are replayed.

This design does not include `error` or `paused` runs in replay.

This design does not replay member child-runs inside teams.

This design does not trust room event payloads as the source of truth for interrupted self-history.

## Recommendation

Persist canonical interrupted replay snapshots into the same Agno session history used for normal completed replay.

Keep `unseen` thread context only for newer external messages that have not yet been persisted into trusted history.

Represent interrupted self-turns as replayable top-level runs with explicit interrupted markers instead of raw `RunStatus.cancelled` replay.

Keep the original cancellation fact in metadata for analytics and delivery behavior.

## Why This Approach

Agno already owns the main persisted history lane, so the cleanest mental model is that session history is the agent-visible truth.

Cancelled turns should be part of that truth for coding-style continuation workflows.

Agno's native `get_messages()` skips cancelled runs by default, so replaying raw cancelled runs would require patching vendor semantics and would still not solve hard-cancel-before-persist cleanly.

A canonical interrupted replay snapshot avoids that problem while keeping replay deterministic.

## Scope

The design applies only to top-level self-authored interrupted turns in the same agent or team scope and the same session.

For individual agents, that means one top-level interrupted turn for the same `room_id` or `room_id:thread_id` session.

For teams, that means the top-level team response only and not member child-runs.

## Data Model

Add one internal `InterruptedReplaySnapshot` runtime collector.

The collector stores partial assistant text, completed tool calls, interrupted tool calls, Matrix linkage, seen event IDs, and interruption reason.

Persist the snapshot into the session DB as one replayable top-level run in the same scope as a normal completed turn.

Set replay metadata such as `mindroom_replay_state=interrupted`.

Set lifecycle metadata such as `mindroom_original_status=cancelled`.

If Agno already persisted a raw cancelled run for the same top-level run, rewrite that run in place into the canonical interrupted replay form instead of keeping both versions.

## Canonical Interrupted Content

Interrupted replay content must be deterministic and minimal.

The canonical content order is partial assistant text first, tool replay blocks second, and one short terminal marker last.

The terminal marker should be a fixed string such as `[interrupted by user]`.

Completed tools should be rendered in the same trusted history form that the next prompt expects.

Started-but-not-completed tools should be rendered as interrupted and must never look completed.

If there is no partial text and no tool state, no interrupted replay snapshot should be persisted.

## Write Path

The response lifecycle should collect interrupted replay state from trusted runtime events rather than from Matrix message payloads.

Tool collection must happen even when `show_tool_calls` is false because user-facing visibility and replay correctness are different concerns.

On normal completion, the current completed-turn persistence path stays unchanged.

On interruption, the lifecycle persists one canonical interrupted replay snapshot into the same session and scope.

The snapshot becomes part of normal persisted replay for the next turn.

## Read Path

Normal Agno session replay becomes the source of truth for interrupted self-turns.

`execution_preparation.py` should stop reconstructing self partial replies from Matrix thread history.

`unseen` prompt context should be limited to newer external thread messages that happened after the last persisted trusted turn.

This removes the need to rehydrate the agent's own interrupted tool calls from Matrix `io.mindroom.tool_trace`.

## Compaction

Interrupted replay snapshots should be eligible for normal replay and normal compaction because they are canonical persisted history.

The interrupted marker must stay inside the assistant-visible content so compaction does not silently convert incomplete work into completed work.

Compaction summaries should preserve that the turn was interrupted rather than fully concluded.

## Security

Trusted interrupted self-history must come from internal runtime state and persisted session storage only.

Matrix room events should not be treated as authoritative for replaying self interrupted tool state.

This keeps the canonical replay lane separate from untrusted participant-authored event content.

## Testing

Add coverage for cancellation after one completed tool call and verify the next turn sees that tool call without rerunning it.

Add coverage for cancellation with `show_tool_calls=false` and verify hidden user-facing tool calls still become trusted replay state.

Add coverage for cancellation during an in-flight tool call and verify the next turn sees an interrupted tool marker rather than a completed result.

Add coverage that newer user follow-up messages still arrive through `unseen` context while the interrupted self-turn arrives through normal replay.

Add coverage that completed-turn behavior and non-cancelled replay stay unchanged.

## Simplification Outcome

This design adds a small amount of complexity at the write boundary where interruptions are persisted.

It removes larger distributed complexity from the read boundary where prompt assembly currently has to merge canonical replay with self partial reconstruction from Matrix state.

The resulting mental model is simpler because session history becomes the single source of truth for both completed and interrupted self-turns.

## Rejected Alternatives

Reject replaying raw cancelled Agno runs unchanged because Agno skips them by default and because raw cancelled status is not a good canonical replay shape.

Reject keeping the current split and only improving Matrix self-reconstruction because that preserves two competing history systems.

Reject one-off next-prompt notices like "your previous response was cancelled" because they create transient prompt branches instead of stable canonical history.
