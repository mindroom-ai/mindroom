## History Runtime Boundary Cleanup

This plan is a focused follow-up to [two-phase-history-preparation-plan.md](/Users/basnijholt/Code/dev/mindroom/docs/dev/two-phase-history-preparation-plan.md).
The two-phase design fixed the main policy problem, but some boundary issues still remain.

## Goals

The first goal is to make history preparation pure.
`prepare_history_for_run()` should decide what durable rewrite and replay behavior is needed for one run, but it should not mutate the live `Agent` or `Team`.

The second goal is to separate durable session facts from per-run replay facts.
The code should stop using one boolean to mean both "this session has persisted history" and "this call will replay persisted history".

The third goal is to keep config validation static.
If a warning depends on room overrides, active runtime model resolution, or merged runtime compaction behavior, it should not be reconstructed inside `Config`.

## Problems To Fix

The current runtime still lets history preparation mutate the live execution object.
That happens through `apply_replay_plan()` inside `prepare_history_for_run()`.
This is the wrong boundary.
History preparation should produce a decision.
The caller should apply that decision at the execution edge.

The current compaction warning path in `Config` also reconstructs runtime behavior from raw config pieces.
That duplicates runtime model resolution and can diverge from the actual planning code.
It is especially fragile around inherited `compaction.model`, explicit `null` clears, and room-specific runtime model overrides.

## Design

The clean contract is:

`prepare_history_for_run()` decides.

The caller applies.

The live runtime object is mutated only at the execution edge.

To support that contract, `PreparedHistoryState` should carry the resolved replay decision explicitly.

The intended shape is:

```python
@dataclass(frozen=True)
class PreparedHistoryState:
    compaction_outcomes: list[CompactionOutcome] = field(default_factory=list)
    replay_plan: ResolvedReplayPlan | None = None
    replays_persisted_history: bool = False
```

`replays_persisted_history` answers a per-run question.
It is used for prompt fallback behavior.

## Runtime Contract

`prepare_history_for_run()` should keep phase-1 durable compaction.
It should keep replay planning.
It should stop taking `replay_target`.
It should stop calling `apply_replay_plan()` itself.

Instead, it should always return a replay directive for the current run.

If replay budgeting is available, it should return the result of `plan_replay_that_fits(...)`.
If replay budgeting is unavailable, it should still return the configured replay directive for the current scope.
It must not implicitly leave whatever replay flags happen to be on the current object.

`replays_persisted_history` should be derived from the returned replay plan plus actual scoped session contents.
It should no longer inspect replay flags that were mutated on the live `Agent` or `Team`.
That derivation needs to work even when replay budgeting is unavailable and the returned plan is simply the configured replay behavior.

## Call Sites

The replay plan must be applied explicitly by the request-scoped caller.
This includes:

- the single-agent path in `src/mindroom/ai.py`
- the Matrix team path in `src/mindroom/teams.py`
- the OpenAI-compatible team path in `src/mindroom/api/openai_compat.py`

This works well with the current code because these main execution paths already create fresh runtime objects per request.
That means we do not need save-and-restore wrappers, cloning layers, or context managers.
We only need to stop hiding the mutation inside history preparation.

## Config Validation And Warnings

`src/mindroom/config/main.py` should stay limited to static validation.
It should validate only facts that are true without runtime context.

Examples of valid static checks are:

- unknown `compaction.model` references
- invalid threshold combinations
- an explicitly configured `compaction.model` that points to a model with no `context_window`

Runtime-dependent warning logic should not live in `Config`.
If a warning depends on merged defaults, explicit `null` clears, active runtime model selection, room overrides, or runtime fallback semantics, it belongs in history planning or diagnostics instead.

The current `_compaction_enabled_model_names()` heuristic should therefore be removed or sharply reduced to static explicit-model checks only.

## Non-Goals

This cleanup does not require save-and-restore wrappers around `Agent` or `Team`.
It does not require a larger redesign of the two-phase architecture.
It does not assume any backward-compatibility migration for old scoped-state metadata.

The `compaction.model: null` round-trip question is related but separate.
It should be verified independently instead of being mixed into this boundary cleanup unless it is reproduced again.

## Tests

This cleanup should add regressions for the new contract.

The first regression should prove that when replay budgeting is unavailable, history preparation still returns the configured replay plan instead of leaving implicit live-object state behind.

The second regression should prove that the OpenAI-compatible team path explicitly applies the returned replay plan, rather than depending on hidden mutation inside history preparation.

The third regression should prove that `replays_persisted_history` is derived from `replay_plan` plus actual scoped session contents, rather than from mutated live-object flags.

## Why This Is Better

This design gives one clear contract.
History preparation decides.
The caller applies.

It also removes a recurring source of bugs.
Per-run replay behavior stops being hidden inside long-lived object mutation.
Config validation stops trying to predict runtime behavior from partial static information.

This is a smaller change than another full architecture rewrite.
But it fixes the remaining boundary problems in the cleanest way.
