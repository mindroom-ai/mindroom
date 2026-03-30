# History Compaction Simplification Plan

Last updated: 2026-03-30
Owner: MindRoom backend
Status: Implemented.

## Objective

Simplify history replay and compaction so one feature module owns the behavior and the core runtime files only call it at run boundaries.
Fix the current architectural smell by removing the queue-and-apply protocol, multi-pass compaction, and session-global replay state.
Prefer deterministic behavior and small integration points over best-effort history preservation.

## Hard Product Decisions

- Compaction runs only before a model call.
- The `compact_context` tool remains, but it no longer compacts inside the current run.
- Compaction is single-pass only.
- MindRoom no longer preserves turns that arrive after compaction planning because there is no longer a plan/apply gap.
- MindRoom no longer does multi-pass compaction to squeeze oversized history through repeated summary merges.
- When replay still does not fit after compaction, MindRoom drops the oldest raw history deterministically.
- Stored raw runs remain non-destructive in SQLite.
- Mixed-scope sessions are supported by scope-keyed compaction state instead of by session-global rejection guards.
- `keep_recent_tokens` is removed as a feature.
- After compaction, MindRoom keeps the newest two completed runs raw before applying the configured replay policy.

## Why This Simplifies The System

- Removing post-run apply deletes `PendingCompaction`, `pending_compaction_buffer`, and the retry and streaming cleanup that exists only to support delayed apply.
- Removing multi-pass compaction deletes partial-prefix bookkeeping, repeated summary merges, and the remaining-run state machine.
- Scope-keyed persistence removes the mismatch between session-global stored state and scope-aware replay selection.
- A fixed raw suffix rule is easier to reason about than a token-based kept-suffix rule.
- A single pre-run entrypoint means `ai.py`, `teams.py`, and `api/openai_compat.py` no longer need to understand compaction policy.

## End State

- MindRoom has a dedicated `src/mindroom/history/` package.
- `ai.py`, `teams.py`, and `api/openai_compat.py` call one `prepare_history_for_run(...)` function before each run.
- `bot.py` only receives completed compaction outcomes for notice rendering.
- There is no post-run compaction apply step.
- There is no queued manual compaction state.
- There is no multi-pass compaction progress state.
- Compaction state is stored by `(session_id, scope)` inside session metadata.
- Each scope owns its summary and cutoff together.
- MindRoom owns replay semantics instead of delegating them to Agno history knobs.
- `compact_context` only sets a scoped `force_compact_before_next_run` trigger that is consumed by the normal pre-run pipeline.
- Core runtime code no longer infers “has prior history” or “cache must be disabled” from `session.summary`, `add_history_to_context`, or `add_session_summary_to_context`.

## Authority Boundary

MindRoom becomes the source of truth for replay selection and compaction state.
Agno is no longer the source of truth for `num_history_runs`, `num_history_messages`, or `add_session_summary_to_context`.
Agno history replay is disabled for normal runs.
MindRoom injects a scoped summary prefix into the prompt before the current user input.
MindRoom passes the selected raw replay suffix as structured Agno `Message` objects through `additional_input`.
Replay messages are per-run inputs only and must never survive past the current invocation.
Replay messages must not create new memories.
The existing config fields `num_history_runs` and `num_history_messages` remain as authored policy inputs, but MindRoom interprets them itself.
`create_agent()` no longer enables Agno history replay for normal runs and no longer uses compaction or tool presence to toggle `add_session_summary_to_context`.

## Proposed Module Layout

- `src/mindroom/history/types.py`
  - `HistoryScope`
  - `HistoryPolicy`
  - `CompactionState`
  - `ReplayPlan`
  - `PreparedHistory`
  - `CompactionOutcome`
- `src/mindroom/history/storage.py`
  - Read and write scoped compaction state in session metadata.
- `src/mindroom/history/replay.py`
  - Resolve scope.
  - Select completed runs for one scope.
  - Apply cutoff.
  - Build the raw replay suffix under the configured history policy as structured Agno `Message` objects.
  - Render the scoped summary prompt prefix.
- `src/mindroom/history/compaction.py`
  - Choose the compactable prefix.
  - Run one summary pass.
  - Persist the new summary and cutoff.
- `src/mindroom/history/runtime.py`
  - Public pre-run entrypoint used by `ai.py`, `teams.py`, and `api/openai_compat.py`.
  - Return `PreparedHistory` with the summary prompt prefix, structured replay messages, a cache key fragment, and any compaction outcomes.

## State Model

Keep using `MINDROOM_COMPACTION_METADATA_KEY`, but change the value shape to a scoped map.

```yaml
version: 2
states:
  agent:<agent_id>:
    summary: <plain text summary>
    last_compacted_run_id: <run id>
    compacted_at: <iso timestamp>
    summary_model: <model id>
    force_compact_before_next_run: <bool>
  team:<team_id>:
    summary: <plain text summary>
    last_compacted_run_id: <run id>
    compacted_at: <iso timestamp>
    summary_model: <model id>
    force_compact_before_next_run: <bool>
```

`session.summary` stops being authoritative MindRoom compaction state.
MindRoom reads and writes only its own scoped metadata records.
The old session-global format is ignored.
Sessions that only have the old format start fresh under the scoped model.

## Replay Policy

MindRoom keeps the existing authored policy surface:

- `all`
- `runs(N)`
- `messages(N)`

The raw replay suffix is selected by that policy after applying the scope-specific cutoff.
The compaction system always keeps the newest two completed runs available for raw replay before policy trimming.
The fixed newest-two-runs rule is a deliberate product simplification for v1.
If later tuning is needed, add a simple authored `keep_recent_runs` config instead of a tool parameter or token-aware suffix rule.
The replay state then contains:

- the scoped summary, if one exists and fits
- the raw replay suffix as structured Agno `Message` objects

## Budget Policy

Use one explicit fallback chain when the replay envelope is too large:

1. Try the normal replay envelope.
2. If over threshold, run single-pass compaction on everything older than the newest two completed runs.
3. Rebuild the replay envelope from the new summary plus the raw suffix.
4. If still over threshold, drop the oldest raw runs until the envelope fits.
5. If the remaining raw run still exceeds the replay policy budget, drop the oldest raw messages until the envelope fits.
6. If the envelope is still too large, keep the summary only.
7. If the summary alone is too large, run with no history and log a warning.

This is intentionally lossy.
The design goal is determinism and correctness, not maximum history retention.

## Compaction Algorithm

At run start:

1. Resolve the execution scope for the current run.
2. Load the scoped compaction state for that scope.
3. Collect completed top-level runs for that scope only.
4. Apply the stored cutoff for that scope only.
5. Build the current replay envelope and estimate its size.
6. If the replay envelope is under threshold, return it unchanged.
7. If auto-compaction is disabled for this agent, skip compaction and apply only the oldest-first drop policy.
8. If `force_compact_before_next_run` is set for this scope, treat compaction as required for this run even if the normal threshold is not yet crossed.
9. If there are more than two visible completed runs, compact the prefix older than the newest two runs with one summary pass.
10. Persist the new scope summary and cutoff and clear `force_compact_before_next_run`.
11. Rebuild the replay envelope.
12. Apply the oldest-first drop policy if needed.
13. Return the final summary prompt prefix, structured replay messages, and any compaction notice metadata.

There is no second compaction pass.
There is no pending state waiting for the current run to finish.
There is no attempt to hide or preserve runs created after planning because planning and applying are one step.

## Prompt Construction

The history module returns two things.
It returns a deterministic summary prompt prefix string.
It also returns the raw replay suffix as copied Agno `Message` objects.
The summary prompt prefix is prepended to the user prompt before the current run is sent to Agno.
The structured replay messages are passed through `Agent.additional_input` so Agno preserves roles, tool calls, and tool results for recent history.
The summary prompt prefix should use stable headings so summaries remain merge-friendly and prompts stay predictable.

Suggested shape for the summary prompt prefix:

```text
<history_context>
<summary>
...
</summary>
</history_context>
```

This gives MindRoom full ownership of replay semantics without spreading message-selection logic through core runtime files.

## Prompt Ownership And Ordering

The history module owns only persisted replay state from stored completed session runs.
It does not own Matrix thread-history diffing, unseen-message extraction, interrupted partial-reply handling, or the no-session thread-stuffing fallback.

Those rules stay outside the history module because they depend on live thread payloads rather than stored replay state.
The history module returns one `summary_prompt_prefix` string and one `history_messages` sequence.

Prompt assembly order becomes:

1. Build the base enhanced user prompt.
2. Ask the history module for the persisted replay summary prefix and structured replay messages for the current session and scope.
3. Prepend that summary prefix to the enhanced user prompt.
4. On Matrix paths, prepend unseen thread messages with `_build_prompt_with_unseen(...)`.
5. Pass the structured replay messages through Agno `additional_input`.
6. Only when there is no stored session replay state at all, keep the existing no-session fallback that stuffs the full thread via `build_prompt_with_thread_history(...)`.

The model-visible order is therefore:

- structured raw replay messages
- one final user-message block containing:
  - unseen thread messages, if any
  - persisted replay summary
  - current user prompt

This matches how Agno inserts `additional_input` before the final user message.
This avoids duplication because the history module never re-renders live `thread_history` entries and the Matrix unseen layer never re-renders persisted replay state.

## Replay Message Lifecycle

`PreparedHistory.history_messages` are ephemeral per-run inputs.
They are never persisted as MindRoom compaction state and never reused across requests.
They must not be persisted into the current run's stored session history.
They must not be included in learning extraction input.
If a caller sets `Agent.additional_input`, it must clear that field in a `finally` block immediately after the run completes, errors, or is cancelled.
No implementation may rely on a fresh agent object as the only protection against replay-message leakage.

Replay messages injected through `additional_input` must be excluded from memory extraction.
Replay messages injected through `additional_input` must be excluded from run persistence and learning extraction.
The refactor must not ship with replay messages flowing through Agno's default `extra_messages` memory path.
The refactor must not ship with replay messages flowing through Agno's default run-persistence or learning paths either.
If Agno cannot exclude those messages directly, MindRoom must bypass or replace the relevant memory, persistence, and learning paths so only the current user turn contributes new state.

## Manual Compaction

The `compact_context` tool stays, but its semantics change.
Its new public API is `compact_context() -> str`.
The current `keep_recent_runs` argument is removed.
It no longer performs compaction during the current run.
Instead it sets `force_compact_before_next_run = true` for the current scope in scoped compaction metadata and returns a short confirmation message.
The next run for that same scope then enters the normal pre-run history pipeline, sees the flag, compacts if possible, and clears the flag.
If no compactable prefix exists, the flag is still cleared and the run continues with the normal oldest-first drop policy.

This keeps the user-facing ability to ask for compaction without bringing back:

- queued compaction state
- delayed post-run apply
- special streaming and retry cleanup
- same-run visibility drift bugs

## Core Module Touch Points

The intended core-runtime diff should stay small.

- `src/mindroom/ai.py`
  - Replace the current prepare and post-run compaction plumbing with one pre-run `prepare_history_for_run(...)` call.
  - Stop using `session.summary` or Agno history flags as proxies for prior replay state.
  - Switch cache decisions from `agent.add_history_to_context` and `agent.add_session_summary_to_context` to `PreparedHistory.cache_key_fragment` or an equivalent history-owned cache policy.
  - Pass `PreparedHistory.history_messages` through Agno `additional_input`.
  - Clear `agent.additional_input` after every run in a `finally` block or equivalent lifecycle guard.
  - Keep `_build_prompt_with_unseen(...)` and `build_prompt_with_thread_history(...)`, but compose them after the history module returns its summary prefix.
- `src/mindroom/history/runtime.py`
  - Own the replay-message lifecycle contract.
  - Ensure replay messages do not participate in memory extraction.
  - Ensure replay messages do not participate in stored run persistence or learning extraction.
- `src/mindroom/history/cache.py` or equivalent helper
  - Own replay-aware cache-key construction or replay-aware cache bypass decisions.
- `src/mindroom/teams.py`
  - Use the same pre-run history preparation call as normal agent runs.
- `src/mindroom/api/openai_compat.py`
  - Use the same pre-run history preparation call as normal agent runs.
- `src/mindroom/bot.py`
  - Keep existing notice rendering behavior, but consume only finished pre-run compaction outcomes.
- `src/mindroom/agents.py`
  - Stop configuring normal runs through Agno history fields.
  - Stop enabling session-summary replay because auto-compaction is authored or because `compact_context` is installed.
- `src/mindroom/custom_tools/compact_context.py`
  - Simplify it to set `force_compact_before_next_run` for the current scope and return a confirmation message.
  - Remove the `keep_recent_runs` parameter and any logic that tries to choose a kept raw suffix from tool input.

## Runtime Heuristics That Must Be Deleted

The plan is not complete unless these existing heuristics are removed or replaced.

- “Has prior replay state” checks based on `bool(session.runs) or session.summary is not None`
- cache disabling based on `agent.add_history_to_context` or `agent.add_session_summary_to_context`
- prompt-path branching that assumes Agno will replay stored history natively
- `create_agent()` wiring that enables summary replay because compaction is authored or because the tool is present
- any code that treats `session.summary` as the authoritative persisted compaction state instead of scoped metadata

## Cache Contract

No cached run may ignore replay state.
`PreparedHistory.cache_key_fragment` is required whenever the history module returns replay-affecting state that is not already present in the final user prompt string.
At minimum, that fragment must cover the structured replay messages digest and the scoped persisted replay summary state or an equivalent canonical digest of the whole replay plan.
If the history module cannot produce a stable replay-aware cache fragment for a run, the caller must bypass cache for that run.
Cache correctness may not depend on `agent.add_history_to_context`, `agent.add_session_summary_to_context`, or `session.summary`.

## Implementation Plan

### Phase 1

Create the new `history/` package and add scoped compaction storage with a versioned metadata format.

### Phase 2

Make the replay takeover, compaction rewrite, and runtime wiring one atomic cutover.
Do not merge any intermediate state where Agno and MindRoom both partly own replay semantics.
Implement replay selection inside `history/replay.py`.
Render the scoped summary prompt prefix there.
Return the raw replay suffix as structured Agno `Message` objects via `additional_input`.
Implement single-pass pre-run compaction in `history/compaction.py`.
Add the explicit oldest-first drop policy.
Wire `ai.py`, `teams.py`, and `api/openai_compat.py` to `history/runtime.py`.
Stop relying on Agno history knobs for replay semantics.
Add the explicit prompt-composition contract with Matrix unseen-message handling and the no-session thread-stuffing fallback.
Add the replay-message lifecycle guard that clears `Agent.additional_input` after each run.
Add the replay-safe memory contract so replay messages do not create new memories.
Add the replay-safe persistence and learning contract so replay messages are not stored into the current run history and do not feed learning extraction.
Add replay-aware cache-key construction or replay-aware cache bypass.
Delete `PendingCompaction`, the queued apply path, and the streaming and retry cleanup that only exists to support delayed apply.
Delete the old cache and prior-history heuristics that key off `session.summary` and Agno history flags.

### Phase 3

Delete multi-pass compaction code.
Delete `keep_recent_tokens`.
Update config docs, dashboard text, and tests to match the simplified product.
Simplify `compact_context` to the new next-run trigger semantics if that has not already happened earlier in the refactor.
Update tool metadata and user-facing docs for the zero-argument `compact_context()` API.

## Test Plan

- A direct-agent scope and a team scope in the same session keep independent summaries and cutoffs.
- A team member run reads and writes the owning team scope rather than a direct-agent scope keyed by the member agent id.
- Message-limited replay with one oversized visible run still preserves the newest messages rather than dropping all raw history.
- Structured raw replay passed through `additional_input` preserves roles, tool calls, and tool results for recent tool-heavy runs.
- The model-visible message order is structured raw replay messages first, followed by the final user-message block containing unseen messages, then persisted replay summary, then current user input.
- A history envelope that exceeds the threshold drops the oldest raw runs deterministically.
- A history envelope that still exceeds the threshold after raw-run dropping falls back to summary-only deterministically.
- A summary that still exceeds the threshold falls back to no-history deterministically.
- Streaming and non-streaming run paths use the same `prepare_history_for_run(...)` logic.
- Consecutive runs clear `Agent.additional_input`, and a run with no replay state cannot leak replay messages from a previous run.
- Replay messages injected for history do not create new memories.
- Replay messages injected for history are not persisted into the current run's stored session history.
- Replay messages injected for history are not included in learning extraction input.
- A cached run changes whenever structured replay messages or persisted replay summary state changes, or else cache is bypassed.
- No test depends on post-run apply because that feature no longer exists.
- `compact_context` sets `force_compact_before_next_run` for the active scope only.
- The next run for that scope consumes the flag, compacts in the normal pre-run path, and clears the flag.
- `compact_context` called from a team member run sets `force_compact_before_next_run` for the team scope only.
- The next team member run in that team scope consumes and clears the flag without changing direct-agent scope state.

## Deletions Expected In This Refactor

- `PendingCompaction`
- `pending_compaction_buffer`
- `apply_pending_compaction`
- queued manual compaction tests
- `_CompactionProgress`
- multi-pass compaction helpers
- `keep_recent_tokens`
- `src/mindroom/compaction_runtime.py` after moving any still-useful helpers into `src/mindroom/history/`

## Risks We Accept

- The fixed newest-two-runs raw suffix rule is a deliberate product regression from the older more flexible kept-suffix behavior.
- The summary prefix plus structured raw replay suffix will not be byte-for-byte identical to Agno's old native replay path.
- Some edge cases will now drop old history earlier than before.
- `compact_context` will no longer compact in the current run and instead takes effect on the next run for that scope.
- Very large old sessions may lose the benefit of best-effort multi-pass summarization.

These tradeoffs are acceptable because the current complexity is causing repeated correctness bugs and broad runtime coupling.

## Acceptance Criteria

- The history and compaction feature is isolated under `src/mindroom/history/`.
- The core runtime files call the feature through narrow pre-run hooks only.
- Direct-agent and team replay state can coexist safely in one session.
- No cached run can reuse a result across different replay state.
- Structured replay messages are cleared after each run and cannot leak across requests.
- Structured replay messages do not create new memories.
- Structured replay messages are not persisted into stored session history for the current run.
- Structured replay messages are not included in learning extraction.
- There is no delayed compaction apply path anywhere in the runtime.
- There is no multi-pass compaction code anywhere in the runtime.
- The remaining behavior is simple enough that one engineer can explain the full replay and compaction flow from memory.
