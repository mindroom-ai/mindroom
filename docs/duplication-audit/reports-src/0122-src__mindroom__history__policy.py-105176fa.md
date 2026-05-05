## Summary

One small duplication candidate exists around reserve-clamped context-window budget math.
`src/mindroom/history/policy.py` computes replay budgets with `normalize_compaction_budget_tokens(...)` and `max(0, window - reserve - prompt)`, while `src/mindroom/execution_preparation.py` computes the same window-minus-reserve fallback budget without the static prompt subtraction.
The rest of this module is mostly the canonical policy layer for compaction execution planning and decision classification; other `src` call sites consume its decisions rather than reimplementing them.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
resolve_history_execution_plan	function	lines 20-82	related-only	resolve_history_execution_plan ResolvedHistoryExecutionPlan compaction runtime settings replay budget summary input budget	src/mindroom/history/runtime.py:684; src/mindroom/history/runtime.py:1350; src/mindroom/history/manual.py:145; src/mindroom/history/compaction.py:1009
classify_compaction_decision	function	lines 85-166	related-only	classify_compaction_decision CompactionDecision forced_unavailable auto_disabled under_trigger history_exceeds_hard_budget within_hard_budget	src/mindroom/history/runtime.py:408; src/mindroom/history/types.py:93; src/mindroom/ai_run_metadata.py:170
manual_compaction_unavailable_message	function	lines 169-174	related-only	manual_compaction_unavailable_message Compaction is unavailable unavailable manual compaction	src/mindroom/history/manual.py:137; src/mindroom/custom_tools/compact_context.py:37
describe_compaction_unavailability	function	lines 177-186	related-only	describe_compaction_unavailability no context_window no usable summary input budget unavailable_reason	src/mindroom/history/runtime.py:701; src/mindroom/history/runtime.py:1418; src/mindroom/history/manual.py:153
_resolve_summary_input_budget	function	lines 189-207	related-only	compute_compaction_input_budget normalize_compaction_budget_tokens non_positive_summary_input_budget summary_input_budget_tokens	src/mindroom/token_budget.py:23; src/mindroom/history/compaction.py:1001; src/mindroom/history/runtime.py:630
_resolve_replay_threshold_tokens	function	lines 210-218	related-only	resolve_effective_compaction_threshold threshold_tokens threshold_percent 0.8	src/mindroom/history/compaction.py:983; src/mindroom/config/models.py:206
_resolve_replay_budget_tokens	function	lines 221-236	duplicate-found	normalize_compaction_budget_tokens replay_window_tokens reserve_tokens static_prompt_tokens max(0 window reserve)	src/mindroom/execution_preparation.py:520; src/mindroom/history/runtime.py:383
_resolve_replay_budget_without_compaction	function	lines 239-249	duplicate-found	context_window - normalize_compaction_budget_tokens reserve_tokens static_prompt_tokens fallback_static_token_budget	src/mindroom/execution_preparation.py:520; src/mindroom/history/runtime.py:692
```

## Findings

### 1. Reserve-clamped replay/static budget math is repeated

`src/mindroom/history/policy.py:221` computes replay budget by clamping `compaction_config.reserve_tokens` with `normalize_compaction_budget_tokens(...)`, subtracting that reserve from `replay_window_tokens`, optionally capping by `threshold_tokens`, and then subtracting `static_prompt_tokens`.
`src/mindroom/history/policy.py:239` computes the no-compaction hard replay budget as `max(0, replay_window_tokens - normalized_reserve_tokens - static_prompt_tokens)`.
`src/mindroom/execution_preparation.py:520` computes a related fallback static-token budget as `max(0, context_window - normalize_compaction_budget_tokens(reserve_tokens, context_window))`.

The behavior is duplicated at the token arithmetic level: all three paths define available prompt/history capacity as a context window minus a reserve that is clamped by the same helper.
The difference to preserve is that `execution_preparation.py` returns a total static budget and does not subtract a known current prompt cost, while `policy.py` returns replay-history capacity after subtracting `static_prompt_tokens` and may cap the ceiling at a compaction trigger threshold.

### 2. Compaction decision and unavailability wording are centralized

`src/mindroom/history/policy.py:85` is the only source implementation found for force/auto/unavailable/missing-budget/under-threshold/hard-budget compaction classification.
`src/mindroom/history/runtime.py:408` calls it and logs the returned fields, while `src/mindroom/ai_run_metadata.py:170` serializes the already-classified decision.

`src/mindroom/history/policy.py:177` is also the only source implementation found for turning `_CompactionAvailabilityReason` into user/log text.
`src/mindroom/history/manual.py:153` calls `manual_compaction_unavailable_message(...)`, and `src/mindroom/history/runtime.py:701` and `src/mindroom/history/runtime.py:1418` call `describe_compaction_unavailability(...)` for logging.
These are related call sites, not duplicated behavior.

### 3. Threshold and summary-budget policy delegates to existing helpers

`src/mindroom/history/policy.py:189` wraps `compute_compaction_input_budget(...)` from `src/mindroom/token_budget.py:23` with policy-specific availability reasons.
`src/mindroom/history/policy.py:210` delegates percent/default threshold resolution to `resolve_effective_compaction_threshold(...)` in `src/mindroom/history/compaction.py:983`.
No second source implementation of those policies was found.

## Proposed Generalization

Add one tiny shared helper only if this arithmetic is touched again:

1. Put a pure helper near the existing generic token math, likely `src/mindroom/token_budget.py`, for `available_context_budget(context_window, reserve_tokens, spent_tokens=0) -> int | None`.
2. Have `execution_preparation._fallback_static_token_budget(...)` use it with `spent_tokens=0`.
3. Have `history.policy._resolve_replay_budget_without_compaction(...)` use it with `spent_tokens=static_prompt_tokens`.
4. Keep `_resolve_replay_budget_tokens(...)` local because it has additional authored-compaction and threshold-ceiling semantics.

No immediate refactor is strongly recommended unless more call sites appear, because the duplicated arithmetic is small and the policy module already owns the higher-level compaction behavior.

## Risk/tests

The main risk in deduplicating this is changing edge-case clamping behavior when `context_window` is `None`, non-positive, or smaller than `reserve_tokens`.
Focused tests should cover fallback static budgeting and both replay-budget helpers with normal, tiny, and missing context windows.
Existing compaction policy tests should also assert that forced/manual compaction messages and `CompactionDecision` reasons remain unchanged.

## Questions or Assumptions

Assumption: only files under `./src` count as duplication candidates for this task.
