# Interrupted Turn History Design

## Status

Proposed and approved for planning.

This version replaces the earlier design that tried to retrofit interrupted replay across several existing layers.

## Problem

MindRoom currently spreads one product requirement across four different places.

`stop.py` decides when a turn is interrupted.

`ai.py` and `teams.py` hold the live partial execution state.

`history` code decides what gets persisted and replayed.

`execution_preparation.py` decides what the next prompt sees.

That split has no single owner for the invariant that matters.

The invariant is: every top-level self turn must become exactly one canonical history record, whether it completed or was interrupted.

Because no layer owns that invariant end to end, every fix keeps missing a combination such as agent versus team, streaming versus non-streaming, or graceful cancellation versus hard task cancellation.

## Product Requirement

After an interrupted self turn, the next top-level turn should see the same core facts it would see after a completed turn.

The next turn should see the triggering user message.

The next turn should see any assistant text that was actually produced.

The next turn should see completed tool calls and any known in-flight tool calls.

The next turn should see that the previous turn was interrupted rather than completed.

This should come from one trusted canonical history record, not from ad hoc prompt overlays or untrusted Matrix payload reconstruction.

## Recommendation

Create one response-lifecycle-owned interruption recorder and make it the only writer of canonical interrupted turn history.

Execution code should emit trusted runtime facts into that recorder.

Stop handling should signal interruption through that recorder before cancellation completes.

History replay should consume the recorder's finalized canonical turn records and nothing else for self-turn reconstruction.

The unseen Matrix overlay should go back to its narrower job of carrying only newer external messages that happened after the last persisted canonical turn.

## Why This Design Is Better

This design is better because it moves the guarantee to the only place that can actually enforce it.

The response lifecycle already sits at the boundary where a turn starts, streams or runs, completes or is interrupted, and hands control back to delivery and history code.

If that lifecycle owns the canonical turn record, then cancellation, state capture, persistence, and replay all line up behind one invariant.

That removes the current cross-product of edge cases where each layer partially understands interruption and no layer fully owns it.

It also gives the simplest mental model for future work.

Canonical history becomes the full self-authored past.

The unseen overlay becomes newer external Matrix messages only.

## Rejected Approaches

### Patch the current design in place

Reject this approach because it keeps the same ownership problem.

It can fix individual holes, but every new hole appears at a boundary between stop handling, execution, persistence, and prompt assembly.

### Replay raw cancelled Agno runs directly

Reject this approach because raw cancelled runs are not a good canonical replay shape.

They are also skipped by Agno replay today, and relying on vendor run status alone still does not solve hard task cancellation cleanly.

### Reconstruct interrupted self turns from Matrix message payloads

Reject this approach because Matrix room payloads are not the trusted source of truth for internal self-turn replay.

It also keeps replay correctness entangled with visible message formatting.

## Core Invariant

Every top-level self turn finalizes as exactly one canonical turn record.

The canonical turn record has an explicit terminal state of either `completed` or `interrupted`.

The next turn reads canonical turn records as the agent-visible self history.

No other subsystem should reconstruct interrupted self turns independently.

## Architecture

### Turn Recorder

Add one internal `TurnRecorder` for every top-level response run.

The recorder is created by the response runner before execution begins.

The recorder is owned by the response lifecycle rather than by `ai.py` or `teams.py`.

The recorder accumulates trusted runtime facts only.

Those facts are:

- the triggering user message
- the canonical session and scope identifiers
- assistant text deltas
- completed tool calls
- in-flight tool calls
- seen external event ids for the turn
- the final outcome of the turn

### Execution Adapters

`ai.py` and `teams.py` should stop deciding how interrupted replay is persisted.

Their job becomes emitting execution facts into the `TurnRecorder`.

For streaming runs, that means forwarding content deltas, tool-start events, tool-complete events, and completion or cancellation events.

For non-streaming runs, that means forwarding the final `RunOutput` or `TeamRunOutput` when one exists.

If a non-streaming run is hard-cancelled before any trusted runtime facts arrive, the recorder may still finalize an interrupted turn with only the user message and an interrupted marker.

That is acceptable because it is truthful.

The system must not invent partial assistant text that it never observed.

### Stop Boundary

`stop.py` should not be responsible for history persistence.

Its job is to signal interruption through the active response lifecycle and then cancel execution.

The response lifecycle should record that interruption was requested before the underlying task is cancelled.

That gives one consistent exit path for graceful cancellation and hard task cancellation.

### Finalization

The response lifecycle must finalize the recorder exactly once.

Finalization happens in one place regardless of whether the run:

- completed normally
- emitted an explicit cancelled event
- returned a cancelled run output
- was hard-cancelled at the task level

Finalization writes one canonical turn record into session history.

If the turn completed, the canonical record is a normal completed turn.

If the turn was interrupted, the canonical record is an interrupted turn with explicit interrupted content.

## Canonical Turn Record

The canonical turn record should be stored in the same trusted session history lane used for normal replay.

The shape should be stable and minimal.

For interrupted turns, the canonical record contains:

- the triggering user message as the user half of the turn
- assistant content assembled from observed assistant text and tool state
- one short deterministic terminal marker such as `[interrupted]`
- metadata marking the turn as interrupted

Completed tools should render using the same trusted replay format the next prompt already expects.

In-flight tools should render as interrupted and must never appear completed.

If no assistant text or tool state was observed before interruption, the assistant content should still include the interrupted marker so the turn remains explicit and stable.

## Persistence Model

Canonical interrupted turn records should be stored in the same session and scope as the completed turns they belong beside.

The persisted metadata should distinguish replay semantics from lifecycle facts.

The replay semantics should say that the record is replayable self history.

The lifecycle metadata should preserve that the original outcome was interrupted.

The exact metadata names can be finalized in implementation, but the distinction matters.

The persisted record must be the only source of truth for self interrupted replay.

## Read Path

Prompt preparation should read canonical persisted self history first.

Prompt preparation should not reconstruct interrupted self turns from Matrix visible message content.

The unseen Matrix overlay should only include newer external messages that happened after the last persisted canonical turn.

That means:

- no self interrupted replay from `io.mindroom.tool_trace`
- no self interrupted replay from visible streaming marker text
- no second self-history lane competing with canonical replay

## Scope For V1

This design should stay narrow in the first implementation.

V1 includes:

- top-level agent turns
- top-level team turns
- completed turns
- interrupted turns
- completed tools
- known in-flight tools

V1 excludes:

- child member runs as independent replay units
- `paused` and `error` as replay outcomes
- any attempt to reconstruct self interrupted state from room payloads
- any attempt to invent non-observed assistant content for hard-cancelled non-streaming runs

## Security

Trusted interrupted self history must come from internal runtime state and internal persisted session storage only.

Visible Matrix content is user-visible output, not authoritative replay state.

This keeps replay correctness independent from participant-authored room content.

## Migration Plan

The old Matrix self-interruption reconstruction path should be removed rather than kept as a fallback.

Keeping both systems would preserve the current ambiguity about which history lane is authoritative.

Migration should therefore happen as one behavior change:

1. add the lifecycle-owned `TurnRecorder`
2. route agent and team execution facts into it
3. finalize canonical interrupted records through one path
4. remove self interrupted reconstruction from prompt preparation

## Testing

Add coverage for completed agent turns and completed team turns to prove the normal path is unchanged.

Add coverage for streaming interruption after assistant text only.

Add coverage for streaming interruption after one completed tool call.

Add coverage for streaming interruption with one in-flight tool call.

Add coverage for hard task cancellation through the stop path for agent and team runs.

Add coverage for non-streaming interruption where no partial assistant state was observed and verify that the canonical record stays truthful and minimal.

Add coverage that the next prompt reads interrupted self turns from canonical history and that the unseen overlay contains only newer external messages.

## Simplification Outcome

This design adds one focused subsystem at the response lifecycle boundary.

In return, it removes distributed interruption logic from stop handling, execution code, replay helpers, and prompt assembly.

That is a good trade because write-boundary complexity is easier to isolate and test than read-boundary complexity spread across the system.

The resulting mental model is simpler.

A turn produces one canonical history record.

The next turn reads canonical history plus newer external messages.
