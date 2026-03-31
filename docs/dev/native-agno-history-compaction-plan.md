# Native Agno History Compaction Plan

Last updated: 2026-03-30
Owner: MindRoom backend
Status: Implemented.

The code now uses destructive session compaction with native Agno replay.
The shipped implementation keeps `store_history_messages=False` so replayed raw history is not copied into newly persisted runs.

## Objective

Replace the current replay-injection design with a much simpler model.
MindRoom should decide when to compact history.
Agno should own normal history replay and session-summary replay through its public API.
MindRoom should stop injecting replay messages through `additional_input`.
MindRoom should stop monkey-patching Agno objects at runtime.

## Problem Statement

The feature we want is simple.
Older conversation history should be summarized after some threshold.
Recent conversation history should remain raw.

The current implementation is much more complicated than the product need.
It builds a custom replay layer on top of Agno.
It injects raw replay messages through `additional_input`.
It then has to stop those replay messages from leaking into persistence, learning, and future runs.
That is why `src/mindroom/history/runtime.py` currently patches Agno internals at runtime.

That design is not acceptable.
It creates invisible behavior changes on live objects.
It depends on private Agno methods.
It spreads history semantics across runtime glue, storage, persistence cleanup, learning cleanup, and tests.

## Chosen Direction

MindRoom should own compaction only.
Agno should own replay.

That means:

- MindRoom decides when compaction should run.
- MindRoom chooses which old runs to summarize.
- MindRoom rewrites the persisted session to contain:
  - a session summary for compacted history
  - only the remaining raw recent runs
- Agno then handles normal history replay using:
  - `add_history_to_context`
  - `num_history_runs`
  - `num_history_messages`
  - `max_tool_calls_from_history`
  - `add_session_summary_to_context`

This removes the entire raw replay injection layer.

## Hard Product Decisions

- Raw replay injection is deleted.
- `additional_input` is not used for persisted history replay.
- MindRoom no longer reconstructs exact raw replay across the compaction boundary.
- After compaction, older history exists only as `session.summary`.
- Recent un-compacted runs remain raw in `session.runs`.
- Compaction is destructive inside the active persisted session.
- There is no backward-compatibility migration for the old replay-injection format.
- `compact_context` remains a next-run trigger instead of same-run compaction.
- If compaction is unavailable for the current model setup, `compact_context` should return an error immediately.

## Why This Is Better

- No monkey-patching.
- No subclassing Agno just to intercept private lifecycle methods.
- No replay-message scrubbing from persistence or learning.
- No separate replay state, prepared replay state, and replay digest layer.
- No need to keep Agno and MindRoom replay behavior in sync.
- Team and agent runs use the same public history semantics.

This matches the actual product requirement.
Older turns become a summary.
Recent turns stay raw.

## Non-Goals

- Preserve exact raw history forever after compaction.
- Support two history systems in parallel.
- Maintain backward compatibility with the replay-injection design.
- Rebuild Agno's run pipeline inside MindRoom.

## End State

- `src/mindroom/history/` owns compaction decisions only.
- Agno owns history replay.
- `create_agent()` and `Team(...)` setup enables Agno history and session-summary replay using public configuration.
- `prepare_history_for_run(...)` becomes a pre-run compaction hook instead of a replay-preparation hook.
- There is no replay binding and no replay cleanup lifecycle.
- `src/mindroom/history/replay.py` is deleted.
- Most of the method-patching code in `src/mindroom/history/runtime.py` is deleted.

## Authority Boundary

MindRoom remains the source of truth for:

- compaction thresholds
- compaction model selection
- manual force-compaction flags
- deciding which old runs get summarized

Agno becomes the source of truth for:

- replaying remaining raw history
- replaying the persisted session summary
- trimming replay to `num_history_runs` or `num_history_messages`
- limiting tool calls from replayed history

MindRoom should not second-guess Agno once compaction is done.

## Data Model

The persisted session becomes the authoritative history state.

For agent scope:

- `AgentSession.runs` holds the remaining raw runs
- `AgentSession.summary` holds the compacted summary

For team scope:

- `TeamSession.runs` holds the remaining raw runs
- `TeamSession.summary` holds the compacted summary

MindRoom metadata should shrink to control and audit fields only.
It should no longer store summary text or cutoffs as a parallel source of truth.

Suggested metadata shape:

```yaml
version: 1
force_compact_before_next_run: false
last_compacted_at: 2026-03-30T21:00:00Z
last_summary_model: gpt-5.4
last_compacted_run_count: 12
```

The session itself is the history state.
Metadata is only control-plane state.

## Runtime Flow

Before each run:

1. Load the persisted `AgentSession` or `TeamSession`.
2. Resolve whether compaction is enabled and possible for this run.
3. If compaction is not required, do nothing to the session.
4. If compaction is required, rewrite the session before calling Agno.
5. Run Agno normally with native history and session-summary replay enabled.

No replay messages are injected.
No replay summary prefix is manually prepended.
No replay cleanup runs after the model call.

## Compaction Flow

When compaction triggers:

1. Load the current session.
2. Read the existing `session.summary`, if any.
3. Inspect `session.runs`.
4. Select the oldest compactable prefix.
5. Generate one new summary that covers:
   - the old existing summary, if present
   - the selected old raw runs
6. Write that merged summary back to `session.summary`.
7. Remove the compacted raw runs from `session.runs`.
8. Persist the session.
9. Clear `force_compact_before_next_run`.

After that, Agno sees:

- one persisted summary
- a smaller set of recent raw runs

That is the entire feature.

## Prefix Selection Rule

MindRoom still needs one explicit rule for which runs stay raw.

The simplest correct rule is:

- compact the oldest runs until the remaining session history is predicted to fit
- never compact the newest completed run
- prefer keeping at least the newest two completed runs raw when possible

This keeps compaction deterministic without recreating a custom replay engine.
The exact replay that the model sees after that is Agno's responsibility.

## Budgeting Rule

MindRoom should budget compaction against the actual prompt consumer before the run.

If a usable `context_window` cannot be resolved from the active model or the configured compaction model:

- auto-compaction is unavailable
- `threshold_tokens` should not shrink history budget
- `compact_context` should return an explicit error

This keeps the compaction contract honest.

## `compact_context`

`compact_context` should stay zero-argument.
Its meaning should remain "compact before the next reply for this scope."

The tool flow should be:

1. Resolve the current scope.
2. Resolve whether compaction is actually possible for that scope and model setup.
3. If not possible, return an error immediately.
4. If possible, set `force_compact_before_next_run = true`.
5. Return a confirmation message.

The next run then executes normal pre-run compaction.

## Agents

`create_agent()` should go back to public Agno history settings.

For normal agents:

- `add_history_to_context=True`
- `add_session_summary_to_context=True`
- `store_history_messages=False`
- `num_history_runs` or `num_history_messages` from config
- `max_tool_calls_from_history` from config

MindRoom should not disable Agno replay anymore.

## Teams

Team sessions should use the same model.
Compaction rewrites the team-owned `TeamSession`.
Agno team replay then uses the team session directly.

This removes:

- member replay injection
- bound replay propagation
- replay scrubbing from member persistence
- replay budgeting against injected history payloads

Named teams and ad hoc teams still need stable team storage identity.
That part stays.
Only the replay mechanism changes.

## OpenAI-Compatible Path

The OpenAI-compatible path should stop emulating MindRoom-owned replay too.
It should only:

- build the current user prompt
- trigger pre-run compaction if needed
- then run the team or agent with native Agno history enabled

This removes one more special-case path.

## What Gets Deleted

The redesign should delete or heavily simplify:

- `src/mindroom/history/replay.py`
- replay digest and replay cache-key fragments
- `PreparedReplay.history_messages`
- replay lifecycle cleanup code
- replay-message persistence scrubbing
- replay-message learning scrubbing
- replay-message binding for bound team members
- runtime monkey-patching in `src/mindroom/history/runtime.py`

## Migration Policy

There is no migration.
This redesign should start fresh from the new model.
Any session written by the replay-injection design may lose its compacted continuity after the switch.
That is acceptable.

## Validation Gates

Before committing to this redesign, validate two things explicitly.

### 1. Native Agno team behavior must be good enough

This redesign depends on Agno team history and session-summary replay behaving cleanly enough through the public API.
Do not assume that from code inspection alone.
Run a small spike and verify:

1. non-streaming team runs with native history enabled
2. streaming team runs with native history enabled
3. team session summary replay plus remaining raw team history
4. tool-call history behavior in replayed team context
5. no obvious duplicated context between team-level history and member execution

If native Agno team replay is not good enough, stop and reconsider the redesign before rewriting the current system.

### 2. Destructive compaction must be an accepted product decision

This redesign assumes that compacted raw runs are removed from the live persisted session.
That is what makes the system simple.
The active session contains:

- one merged summary for compacted history
- only the remaining raw recent runs

That means the session DB is treated as live runtime state, not as a forensic event log.
If later debugging or audit needs appear, add a separate archive artifact.
Do not keep a second live history representation inside the session just for possible future debugging.

For now, the accepted product decision is:

- live sessions are destructively compacted
- there is no archive path in this redesign
- if we are not comfortable with that, do not do this redesign yet

## Implementation Plan

1. Add a new design note to the current history plan saying it is superseded by this document.
2. Reduce `src/mindroom/history/runtime.py` to:
   - load session
   - decide whether compaction should happen
   - run compaction
   - persist the rewritten session
3. Delete `src/mindroom/history/replay.py`.
4. Delete replay-specific types from `src/mindroom/history/types.py`.
5. Re-enable native Agno history and session-summary replay in agent and team construction.
6. Remove replay injection from `ai.py`, `teams.py`, and `api/openai_compat.py`.
7. Remove replay cleanup from persistence and learning code paths.
8. Simplify tests around one model:
   - compaction rewrites session
   - Agno replays summary plus remaining raw history

## Acceptance Criteria

- There is no monkey-patching in `src/mindroom/history/runtime.py`.
- There is no MindRoom-owned replay injection through `additional_input`.
- Compaction rewrites `session.summary` and `session.runs`.
- Agno native history replay is enabled for agents and teams.
- `compact_context` fails fast when compaction is impossible.
- Team and agent paths share the same compaction model.
- The resulting code is materially smaller than the replay-injection design.

## Success Metric

This redesign is worth keeping only if it clearly reduces complexity.
The expected result is:

- fewer runtime branches
- fewer history-specific types
- fewer test fixtures
- no runtime monkey-patching
- a smaller total diff than the current replay-injection design

If the implementation does not substantially reduce code and concepts, revert it.
