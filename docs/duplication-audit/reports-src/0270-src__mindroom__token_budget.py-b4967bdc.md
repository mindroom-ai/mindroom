Summary: No meaningful duplication found that warrants production changes.
The only reusable overlap is deterministic JSON serialization, where several modules use local `json.dumps(..., sort_keys=True)` variants for domain-specific output, validation, hashing, or previews.
`token_budget.py` already centralizes the generic token estimate and compaction input budget used by active compaction paths.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
estimate_text_tokens	function	lines 12-20	related-only	estimate_text_tokens, chars // 4, len(...) // 4, max_tokens * 4	src/mindroom/history/compaction.py:695, src/mindroom/history/compaction.py:710, src/mindroom/history/compaction.py:780, src/mindroom/history/compaction.py:1286, src/mindroom/history/compaction.py:1535, src/mindroom/history/compaction.py:1695, src/mindroom/history/compaction.py:1774
compute_compaction_input_budget	function	lines 23-37	none-found	compute_compaction_input_budget, reserve_tokens, prompt_overhead_tokens, safety_margin_ratio, context_window - reserve	src/mindroom/history/policy.py:189, src/mindroom/execution_preparation.py:520, src/mindroom/history/compaction.py:994, src/mindroom/history/compaction.py:1001
stable_serialize	function	lines 40-50	related-only	stable_serialize, json.dumps sort_keys, separators=(,, :), ensure_ascii, default=str	src/mindroom/approval_manager.py:106, src/mindroom/approval_manager.py:115, src/mindroom/constants.py:446, src/mindroom/workers/backends/kubernetes_resources.py:262, src/mindroom/custom_tools/matrix_api.py:143, src/mindroom/custom_tools/matrix_api.py:262, src/mindroom/mcp/results.py:49
```

## Findings

No active duplicate of `estimate_text_tokens` was found outside this module.
Several compaction helpers still perform direct character-to-token arithmetic, but they either call `estimate_text_tokens` for text inputs or intentionally count already-rendered message/tool/media character totals before a single `// 4` conversion.
Examples include `estimate_static_tokens` and `compute_prompt_token_breakdown` in `src/mindroom/history/compaction.py:695` and `src/mindroom/history/compaction.py:1747`.
Those are related token-estimation behavior, not independent copies of `estimate_text_tokens`, because they aggregate domain-specific character sources before converting to tokens.

No duplicate of `compute_compaction_input_budget` was found.
The active summary-input path in `src/mindroom/history/policy.py:189` already calls this helper after normalizing reserve tokens.
Nearby budgeting helpers in `src/mindroom/history/compaction.py:994`, `src/mindroom/history/compaction.py:1001`, and `src/mindroom/execution_preparation.py:520` solve different budget problems: clamping a knob, applying a per-call summary cap, and computing fallback static prompt budget.
Those differences should remain explicit.

The closest duplicated behavior is stable JSON serialization.
`stable_serialize` uses `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)` and returns strings unchanged.
Related local serializers exist in `src/mindroom/approval_manager.py:106`, `src/mindroom/constants.py:446`, `src/mindroom/workers/backends/kubernetes_resources.py:262`, `src/mindroom/custom_tools/matrix_api.py:143`, and `src/mindroom/mcp/results.py:49`.
They are not direct duplicates because their output contracts differ: approval previews fall back to `str()` on `TypeError`, startup manifests and Kubernetes hashes require deterministic compact JSON without `default=str`, Matrix tool payloads use default JSON spacing as user-visible tool output, and MCP result compaction forces ASCII.

## Proposed Generalization

No refactor recommended for this primary file.
If deterministic JSON serialization variants continue to multiply, a future narrow helper such as `mindroom.json_utils.dumps_stable(value, *, ensure_ascii=False, compact=True, default=str)` could be considered.
That helper should not be introduced from this audit alone because the current call sites have meaningful format and failure-mode differences.

## Risk/tests

Changing `estimate_text_tokens` would affect compaction replay sizing, tool definition estimates, summary input construction, and prompt metadata.
Tests around `src/mindroom/history/compaction.py` token estimates and compaction chunking would need attention.

Changing `compute_compaction_input_budget` would affect compaction availability decisions in `src/mindroom/history/policy.py`.
Tests for authored reserve tokens, missing context windows, and non-positive summary budgets would need attention.

Changing `stable_serialize` or replacing local JSON serializers would risk snapshot/hash churn and user-visible output formatting.
Tests for approval argument previews, startup manifest serialization, Kubernetes pod-template hashing, Matrix API tool payloads, and MCP result rendering would need attention.
