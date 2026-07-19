# ISSUE-246 redesign implementation report

Status: 🔍 APPROVAL PENDING

Branch: `issue-246-compaction-token-estimate`, rebased onto `origin/main` (b1949a4dc), 12 commits ahead.
Binding inputs: `DESIGN-SYNTHESIS.md` (decision), `DESIGN-CLAUDE.md` §2/§3/§6 (invariants, mechanism, tests), `DESIGN-CODEX.md` borrowings B1 (multi-profile acceptance = its invariant 3) and B2 (migration sentinel = its test #8).

## What changed, by commit

1. **Rebase (A3)** — the 7 prior branch commits replayed onto `origin/main`, which gained safeguard-fallback model switching (e2a3ee6cb).
   Conflict resolutions integrate both sides: main's `_compaction_token_estimator` seam became a combined estimator+kind resolver re-evaluated at the fallback switch, and main's new direct test callers were adapted to this branch's required sizing kwargs (`ISSUE-246 A3: adapt main's fallback tests...`).
2. **Mechanism** (`ISSUE-246 redesign: persisted-summary fit invariant + provider-arbitrated condensation backstop`):
   - `token_budget.persistable_summary_limit(B) = B - max(1000, B // 4)` — the acceptance limit `L(B)`, one home for the arithmetic.
   - `_CompactionSizingContext` frozen dataclass (model, model_name, genuine_openai_endpoint, token_estimator, estimate_kind, summary_input_budget, acceptance_limit) + `_resolve_compaction_sizing_context`, called at rewrite start and at the safeguard-fallback switch seam (outer loop and in-helper), so frozen sizing labels hold per serving model.
   - **I1 acceptance** in `_generate_compaction_summary_with_retry`: after a successful call, `E(block(candidate))` is evaluated for EVERY profile that can serve the next compaction attempt (primary AND configured fallback, each under its own resolved budget — codex B1); a violation raises `CompactionSummaryOversizedOutputError`, a member of `_TYPED_SHRINKABLE_ERRORS`, so the existing policy shrinks once and exhaustion propagates with nothing persisted (I3).
   - **Backstop** `_condense_carried_summary` rewritten: marker consult first (zero calls on a match unless forced); no client-side send gate; the call routes through the retry wrapper with `SummaryRetryPolicy(shrink_allowed=False)` (transient failures keep the standard one delayed same-budget retry); fitting output persists IMMEDIATELY as a summary-only chunk via `record_compaction_chunk` with empty run ids (I5); strictly-smaller-but-unfit output persists with a marker on the NEW digest; non-shrinking output gets one strengthened retry whose condense note carries the numeric word target from `L(B)`, then a marker; a typed `ContextWindowExceededError` writes the marker, logs a distinct warning, and raises `_CarriedSummaryUnfitError` naming the remedies, which the lifecycle wrapper surfaces as the failure notice.
   - `HistoryScopeState.carried_summary_unfit: CarriedSummaryUnfitMarker | None` (frozen dataclass: summary_digest, model_identifier, summary_input_budget, failed_at, reason), round-tripped through session metadata in `storage.py`; matches only when digest, model identity, and budget all match (I4).
   - Prompt steering: soft word target appended to the summary prompt, `min(acceptance limits) // 5` words; provider `max_tokens` untouched.
   - `policy.py` resolves `compaction_fallback_summary_input_budget_tokens` from the fallback's own window (min'd with the replay window, same availability floor), plumbed through `runtime.py` → `compact_scope_history`.
   - `docs/configuration/models.md` + both generated mirrors: the shrink-retry-recovers claim replaced with the actual contract (fit-by-construction, one provider-arbitrated condensation attempt, terminal per (summary, model, budget), remedies).
   - Deletions (unanimous): the o200k send gate + its justification comment, the direct retry-bypassing `generate_compaction_summary` call, the adopt-then-discard condensation flow, the frozen-once estimator/estimate_kind block. `approximate_o200k_tokens` survives only for `vertex_claude_compat`.
3. **Test matrix** (`ISSUE-246 redesign: full invariant test matrix`) — all 12 DESIGN-CLAUDE §6 tests, codex #8, the multi-profile acceptance test, and the reworked round-4 sentinel tests (map below).
4. **ty fix** — the one ty finding introduced by this branch (dict narrowing in the marker parser) fixed with the house cast pattern; remaining ty failures are confined to `src/mindroom/desktop/*` (macOS-only AppKit/Quartz imports), byte-identical to `origin/main`, i.e. the pre-existing Linux baseline.

## Deviations from the design docs (each justified)

1. **Acceptance enforcement is scoped to planner-admissible budgets** (`summary_input_budget > 2 × COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS`, the same expression as the backstop's floor guard).
   Below ~1,334 units `L(B) ≤ 0` and nothing could ever pass; DESIGN-CLAUDE's consequence proof (§4 Q1) is itself stated for the planner's floor `B = 2,001` upward, and plans at or below the floor are unavailable in production, where the pre-existing degenerate-budget no-op contract (still pinned by `test_rewrite_keeps_noop_contract_for_degenerate_budget_with_carried_summary`) applies.
2. **I4's "(at most 2 provider calls)" parenthetical is 3 in one mixed case.**
   §3.3 mandates the wrapper-owned transient same-budget retry and §3.4 mandates one strengthened not-smaller retry; a transient blip followed by a non-compressing output therefore spends initial + transient retry + strengthened call = 3 calls, once, before the marker.
   The substance of I4 — one attempt-set per distinct (digest, model, budget), bounded and never repeated automatically — holds; the strengthened call itself runs with `max_attempts=1` so it adds no further retries.
3. **Remedy wording uses the `compact_context` tool, not `!compact`.**
   No `!compact` chat command exists in this codebase; forced compaction is requested via the `compact_context` tool (matching `docs/configuration/models.md`). The design's `!compact` is read as shorthand for "force compaction".
4. **§6 test 10's "a condensation after the switch uses the fallback" is not implemented literally** because it is unreachable by construction: a fallback-served chunk's summary passed acceptance under every profile, so the zero-run corner cannot arise later in the same rewrite (I1 consequence).
   The test instead pins what the seam actually guarantees: estimator arithmetic, logged estimate kind, and model_name all switch together on identical request bytes, in both the genuine-OpenAI→byte-bound and byte-bound→genuine-OpenAI directions, and later chunks build with the fallback's estimator (main's rebased tests).
5. **The strengthened condense note's "numeric size target derived from L(B)" is denominated in words** (`min(acceptance limits) // 5`, the design's own ~5-estimator-units-per-word steering rule) because models can act on a word count, not on estimator units. The acceptance check, not the note, remains the guarantee.
6. **Fallback profile sizing edge cases:** when the fallback's own budget is unresolvable (unknown window) the fallback profile inherits the primary's budget (matching main's unchanged-budget switch semantics); when it resolves but fails the availability floor, no fallback profile is added, because no fallback-served next attempt can exist under its own plan.
   Documented on `ResolvedHistoryExecutionPlan.compaction_fallback_summary_input_budget_tokens`; this keeps codex B1 without adopting the explicitly-rejected config-admission machinery.
7. **The marker write also clears the force flag it consumed** (preserving a concurrently-set fresh force flag).
   Without this, a forced backstop failure would leave `force_compact_before_next_run` set (the no-candidates clearing path deliberately refuses to clear when the durable row moved — and the marker write moves it), producing an unbounded forced retry loop that would violate I4.
8. **`_CarriedSummaryUnfitError` is module-private** (the repo's privacy hook requires it; precedent: `_CompactionSummaryEmptyResultError`). Its remedies message is the user-facing surface via the lifecycle failure notice.

## Invariant → test map

| Invariant | Tests |
|---|---|
| I1 persisted-summary fit | `test_acceptance_limit_guarantees_next_build_includes_a_run` (§6.1, incl. B=2,001), `test_rewrite_rejects_oversized_merge_output_and_persists_nothing` (§6.2), `test_multi_profile_acceptance_rejects_candidate_failing_the_fallback_budget` (+ no-fallback control; codex B1), `test_inherited_unfit_summary_with_fitting_runs_heals_through_a_normal_chunk` (§6.12) |
| I2 complete input (E1) | `test_rewrite_condenses_an_inherited_oversized_summary_into_durable_summary_only_progress` (§6.3 + codex #8: CRITICAL-CONTEXT-SENTINEL in the last Critical Context line reaches the request verbatim and survives into the admitted persisted result), `test_rewrite_sends_condensation_even_when_the_estimate_exceeds_the_budget` (§6.5, TAIL-FACT-MUST-SURVIVE), `test_condensation_transient_failure_retries_same_budget_then_persists` (byte-identical resend), `test_rewrite_keeps_noop_contract_for_degenerate_budget_with_carried_summary` (kept round-4 sentinel) |
| I3 all-or-nothing | `test_rewrite_rejects_oversized_merge_output_and_persists_nothing`, `test_condensation_context_rejection_is_terminal_with_marker_and_remedies` (§6.6), `test_condensation_call_failure_propagates_with_persisted_state_untouched` (§6.11a), `test_condensation_that_cannot_shrink_writes_marker_and_next_pass_is_free` (nothing persisted) |
| I4 bounded spend | `test_condensation_that_cannot_shrink_writes_marker_and_next_pass_is_free` (§6.4: marker, zero next-pass calls, forced bypass exactly one attempt-set), `test_condensation_context_rejection_is_terminal_with_marker_and_remedies` (next pass free), `test_condensation_smaller_but_unfit_output_persists_with_marker_on_new_digest` (§6.8), `test_marker_invalidates_when_any_key_dimension_changes` + `test_matching_marker_skips_condensation_without_model_calls` (§6.9) |
| I5 durable backstop progress | `test_rewrite_condenses_an_inherited_oversized_summary_into_durable_summary_only_progress` (summary-only chunk persists FIRST; lifecycle reports both), `test_persisted_condensation_survives_a_later_chunk_failure` (§6.11c), `test_condensation_transient_failure_retries_same_budget_then_persists` (§6.7) |
| A3 per-serving-model sizing | `test_fallback_switch_relabels_sizing_per_serving_model` (both directions, §6.10), rebased main tests (`test_rewrite_switches_to_fallback_and_uses_it_for_later_chunks`, `test_compaction_fallback_serves_later_chunks_state_and_outcome`), `test_resolve_history_execution_plan_resolves_fallback_summary_budget_from_its_own_window` |
| Sizing-log truthfulness | `test_condense_event_uses_truthful_sizing_fields_and_may_exceed_budget` (§6.11b), existing `test_compaction_sizing_logs.py` schema tests (all still green) |

## Gate results

- Full `uv run pytest tests/ -n auto --no-cov` under `set -o pipefail`: exit 0 (all passed; run repeated on the compaction area after the final ty-fix commit, exit 0).
- `SKIP=ty uv run pre-commit run --all-files`: exit 0, no hook failures, no hook-modified files.
  ty is skipped only for the pre-existing Linux baseline: this branch adds zero ty findings (the one it introduced, dict narrowing in the marker parser, is fixed in its own commit), and the remaining ty failures are confined to `src/mindroom/desktop/{accessibility,provider}.py` (macOS-only AppKit/Quartz imports), byte-identical to `origin/main`.
- `uv run tach check --dependencies --interfaces`: ✅ All modules validated (with `CompactionSummaryOversizedOutputError` added to the `mindroom.history.summary_call` public interface in the same PR).
- Frontend `npx vitest run`: 506 passed, 1 skipped (the branch touches no frontend code; run because the rebase carried main's frontend changes).
