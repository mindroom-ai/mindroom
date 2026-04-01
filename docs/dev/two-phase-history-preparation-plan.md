## Two-Phase History Preparation

This plan replaces the old single-action history preparation model with a two-phase pipeline.
The goal is to separate durable history rewriting from per-run replay safety.

## Goals

The first goal is a hard runtime invariant.
If the active runtime model has a known `context_window`, the replay actually sent to the model must fit before the model call.

The second goal is to keep destructive compaction conservative.
Compaction may improve durable session state, but it must never silently discard previously compacted context.

The third goal is to make the code easier to reason about.
There should be one place that decides whether durable compaction may run, and one place that decides what replay is safe for the current call.

## Problems With The Previous Design

The previous design treated `compact`, `implicit_guard`, and `none` as mutually exclusive actions.
That coupling made compaction responsible for both durable rewriting and run-safety.

This created several recurring bugs.
Authored compaction could disable the old overflow guard even when destructive compaction was unavailable.
Compaction could complete successfully even though the next model call was still over budget.
The oversized-summary path could drop `<previous_summary>` and overwrite durable state with a summary generated from only the newest compacted run.

These were not isolated bugs.
They came from one design problem.
The code did not enforce a final replay-fit invariant after durable compaction.

## New Design

History preparation now has two phases.

Phase 1 is optional durable compaction.
This phase may rewrite the stored Agno session.
It uses the compaction model only for summary generation.
If `previous_summary` cannot be preserved within the compaction input budget, compaction skips instead of degrading it.

Phase 2 is mandatory replay-fit planning.
This phase decides what persisted history is safe to replay for the current model call.
It uses only the active runtime model window.
If the active runtime model has no known `context_window`, replay-fit planning is unavailable and the run proceeds with configured replay behavior.

## Phase 1: Durable Compaction

Durable compaction may run in two cases.
It runs when `compact_context` schedules a forced compaction for the next run.
It also runs automatically when authored auto-compaction is enabled and the current replay exceeds the configured replay budget.
Automatic compaction only runs when that replay budget can actually be computed for the current call.
If no replay budget can be computed, only forced or manual compaction is eligible.

Durable compaction is not responsible for guaranteeing that the next run fits.
It is only responsible for rewriting old session history into a preserved handoff summary.

Durable compaction uses `compaction.model` only for the summary-generation pass.
If `compaction.model` is explicitly configured, that model must define its own `context_window`.
That window is used only for compaction input budgeting.

If durable compaction fails, the run still continues.
The next phase will still plan safe replay when the active runtime model window is known.

## Phase 2: Replay-Fit Planning

Replay-fit planning runs after phase 1.
It runs even when compaction succeeded.
It also runs when compaction was skipped, unavailable, or failed.

Replay-fit planning uses the active runtime model window plus the resolved replay budget for the current call.
`compaction.model` never affects this phase.

Replay-fit planning has a strict degradation ladder.
It first keeps configured raw history plus summary when that already fits.
If that does not fit, it reduces raw replay to the largest fitting run or message limit.
If raw replay cannot fit at all, it falls back to summary-only replay.
If the wrapped Agno summary still does not fit, it disables persisted replay entirely for that run.

The result is a concrete replay plan for the current live `Agent` or `Team`.
`ReplayPlan` is ephemeral.
It must not mutate persisted session state.
It only mutates the live `Agent` or `Team` replay knobs for the current run.
Concretely, it decides whether raw replay is enabled, the final run or message limit, whether summary replay is enabled, and whether persisted replay is fully disabled.
That replay plan is applied after any durable session rewrite has finished.

## Scoped State

Compaction state is persisted per logical history scope.
Agent scope and team scope must not overwrite each other in shared session metadata.
The metadata format therefore stores a map of scope key to scoped state instead of a single global state blob.

## Why This Is Better

The new design is easier to reason about because the responsibilities are explicit.
Compaction mutates durable history.
Replay-fit planning decides what is safe to send now.

This removes the main failure class from the previous design.
Successful compaction no longer implies that the next run is safe.
The run is safe only after replay-fit planning has produced a fitting replay plan.

It also simplifies the operator contract.
Replay safety depends on the active runtime model window.
`compaction.model` only controls whether MindRoom can generate a durable summary.

## Implementation Shape

The intended implementation shape is:

`maybe_compact_session(...) -> session`

`plan_replay_that_fits(...) -> ReplayPlan`

`apply_replay_plan(...)`

The runtime orchestrator should call those in order.
The compaction module should focus on durable rewriting.
The replay planner should focus on the final model-call budget.
