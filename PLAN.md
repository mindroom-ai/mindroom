# PLAN — ISSUE-243: Compaction resilience — single summary-call failure must degrade, never abort-and-loop

Synthesized from two independent plans (codex + claude) and their cross-critiques. Both planners converged on the same core; this plan takes the agreed synthesis.

## Root cause

1. `SummaryRetryPolicy.should_shrink()` (`src/mindroom/history/summary_call.py`) recognizes only `TimeoutError`, `CompactionSummaryOutputLimitError`, and `ContextWindowExceededError` (ISSUE-242). A `ModelSafeguardRefusalError` (Vertex `stop_reason=refusal`, raised in `vertex_claude_compat.py`) or an empty-result failure propagates out of `_generate_compaction_summary_with_retry()` at attempt 1, `_run_scope_compaction_with_lifecycle()` catches it and continues without compaction, the session stays over budget, and the next message re-triggers the identical failing request — an abort-and-loop that bills ~800K–1.2M uncached input tokens per iteration.
2. `_resolve_summary_input_budget()` (`src/mindroom/history/policy.py`) derives the summary chunk budget from the model context window with no independent cap. With `context_window: 1000000` the budget becomes 881,616 tokens, producing refusal-prone, blowout-prone, slow, expensive 800K+ chunks. Replay window is 200K; a summary chunk should never exceed it.

## Changes

### (a) Shrink on refusal + empty result — `summary_call.py`

- Extend the `should_shrink()` isinstance union with `ModelSafeguardRefusalError` (import from `mindroom.error_handling`) and `_CompactionSummaryEmptyResultError`.
- Delete the now-redundant empty-result bypass in `retry_budget()`; empty result goes through the same shrink path.
- Halving changes both request size and which runs are included, so a "deterministic" refusal is not deterministic across attempts; production evidence shows smaller chunks succeed.
- Keep `max_attempts=2`, halving divisor, and floor unchanged. Update invariant docstrings.

### (b) Chunk cap independent of context_window — `policy.py`

- Pass the resolved `replay_window_tokens` into `_resolve_summary_input_budget()` and compute `min(compute_compaction_input_budget(...), replay_window_tokens)` when the replay window is not `None`.
- No new config knob, no magic constant, no second cap in `compaction.py`, `token_budget.py` untouched.
- Keep the existing `<= 0` availability check operating on the capped value (it drives `unavailable_reason` → `destructive_compaction_available` → unavailability messages).
- Known degradation mode (accepted): a single serialized run above 200K routes through `_build_oversized_summary_input()` → truncated excerpt — degrade, not abort. No change to that path.

### (c) Bounded transient retry — `summary_call.py` only

- Module-local `_TRANSIENT_SUMMARY_STATUS_CODES = frozenset({429, 503, 529})` with `isinstance(error, ModelProviderError)` + membership check → one same-budget retry within existing `max_attempts=2`.
- Deliberately narrower than `error_handling.TRANSIENT_PROVIDER_STATUS_CODES`: `ModelProviderError` defaults to `status_code=502`, so reusing the broad set would silently grant unclassified errors (including refusals) a full-budget retry. Add a comment stating exactly this so nobody "helpfully" unifies the lists.
- `retry_budget()` ordering: max-attempts guard → `should_shrink` (shrink) → transient (same budget) → `None`. Shrink-first removes the latent trap where a broadened transient set would stop refusals from shrinking.
- No sleep/backoff, no changes to `compaction.py`, `claude_stream_retry.py`, or `error_handling.py`.

### Plumbing

- `tach.toml`: add `mindroom.error_handling` to `mindroom.history.summary_call` dependencies.

## Tests

- `tests/test_compaction_invariants.py` (policy level): refusal shrink (attempt 1 → half, attempt 2 → None); direct `should_shrink` assertions for refusal + empty result; parameterized 429/503/529 same-budget retry; 400/401 negative guard (transient branch cannot silently broaden).
- Loop level: `test_compaction_shrinks_input_after_safeguard_refusal` modeled on the existing empty-result loop test (~line 798) — proves the typed error propagates through the task/wait wrapper into the retry loop, the second request is smaller, and a successful compaction is persisted. Keep the existing empty-result loop test green.
- `tests/test_history_replay_planning.py` (plan resolution): 1M context window + 200K replay window → `summary_input_budget_tokens == 200_000` while `compaction_context_window == 1_000_000` (cap must not leak into the provider window). Existing resolver tests verified unaffected (computed 10,800 < replay 20,000 no-op at :583; `min(0, …)` harmless at :680).
- Rely on the existing ISSUE-216 regression `test_history_compaction_rewrite.py::test_rewrite_passes_full_summary_input_budget_into_chunk_construction` for budget pass-through.

## Explicitly NOT doing

- Run serialization / duplicated tool results / `tools_schema` (issue-242-item3, other thread).
- Provider-correct token counting (RCA item 4).
- Compaction model selection, timeouts, outer-loop circuit breakers, durable fallback summaries.
- Config knobs, sleeps/backoff, helper relocation into `error_handling.py`, changes to `claude_stream_retry.py` or `vertex_claude_compat.py`.
- No fabricated fallback summary, no suppression of future compaction attempts, no discarding history.

## Verification (inside `nix-shell shell.nix`, after `uv sync --all-extras`)

Targeted pytest (`test_compaction_invariants.py`, `test_history_replay_planning.py`, `test_history_compaction_rewrite.py`, `test_compaction.py`, `test_history_prepare_lifecycle.py`) → full `pytest` → `uv run tach check --dependencies --interfaces` → `uv run pre-commit run --all-files`.
