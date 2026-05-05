## Summary

Top duplication candidate: metric payload conversion and integer metric extraction are implemented in both `src/mindroom/ai_run_metadata.py` and `src/mindroom/ai.py`.
The remaining symbols are mostly metadata shape builders with related serializers elsewhere, but no exact behavior duplication that warrants a refactor.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
empty_request_metric_totals	function	lines 22-31	related-only	empty_request_metric_totals request_metric_totals input_tokens output_tokens cache_read_tokens cache_write_tokens	src/mindroom/ai.py:233; src/mindroom/ai.py:529
_get_model_config	function	lines 34-49	related-only	resolve_runtime_model config.models.get ROUTER_AGENT_NAME entity model config	src/mindroom/teams.py:1398; src/mindroom/ai.py:753; src/mindroom/execution_preparation.py:773; src/mindroom/history/runtime.py:1345
_serialize_metrics	function	lines 52-71	duplicate-found	Metrics to_dict metrics dict usage payload serialize metrics comparison	src/mindroom/ai.py:572; src/mindroom/ai.py:581
_serialize_metrics.<locals>._sanitize_metrics_payload	nested_function	lines 53-60	related-only	sanitize metrics payload float format .12g isinstance str int bool none	src/mindroom/ai.py:572; src/mindroom/metadata_merge.py:9; src/mindroom/llm_request_logging.py:179
build_model_request_metrics_fallback	function	lines 74-94	related-only	build_model_request_metrics_fallback time_to_first_token observed_request_metric_fields total_tokens	src/mindroom/ai.py:529; src/mindroom/ai.py:1628; src/mindroom/ai.py:1671
_build_context_payload	function	lines 97-122	none-found	cache_read_input_tokens uncached_input_tokens window_tokens context payload	none
_provider_reports_cache_tokens_outside_input	function	lines 125-139	none-found	anthropic bedrock vertexai_claude cache tokens outside input provider reports cache	none
_context_input_tokens_from_counts	function	lines 142-160	none-found	context_input_tokens_from_counts cache_read_tokens cache_write_tokens input_tokens provider cache outside input	none
_int_usage_value	function	lines 163-167	duplicate-found	usage payload int value metric int helper	src/mindroom/ai.py:581; src/mindroom/ai.py:596
_build_compaction_metadata_payload	function	lines 170-193	related-only	compaction metadata decision outcome replay_plan prepared_history to_notice_metadata	src/mindroom/history/types.py:145; src/mindroom/history/types.py:259; src/mindroom/delivery_gateway.py:910; src/mindroom/delivery_gateway.py:993
build_prepared_history_metadata_content	function	lines 196-210	related-only	build_prepared_history_metadata_content prepared_context compaction AI_RUN_METADATA_KEY	src/mindroom/ai.py:1013; src/mindroom/teams.py:1503; src/mindroom/api/openai_compat.py:1493
ai_run_extra_content_from_metadata	function	lines 213-220	none-found	ai_run_extra_content_from_metadata AI_RUN_METADATA_KEY run_metadata subset	src/mindroom/response_runner.py:1118; src/mindroom/response_runner.py:1228; src/mindroom/response_runner.py:1318; src/mindroom/response_runner.py:1352
build_ai_run_metadata_content	function	lines 223-339	related-only	build_ai_run_metadata_content Matrix run metadata usage context compaction prepared_context tools	src/mindroom/ai.py:1116; src/mindroom/ai.py:1633; src/mindroom/ai.py:1683; tests/test_compaction.py:355
```

## Findings

### 1. Metrics conversion and integer extraction are duplicated

`src/mindroom/ai_run_metadata.py:52` converts `Metrics | dict | None` into a dict-like usage payload before Matrix metadata serialization.
`src/mindroom/ai.py:572` implements the same core conversion for metrics comparison: `None` stays absent, `Metrics` is converted through `to_dict()`, non-dict `to_dict()` results are discarded, and plain dict metrics pass through.

`src/mindroom/ai_run_metadata.py:163` then extracts integer usage values from a dict payload.
`src/mindroom/ai.py:581` repeats the same integer extraction behavior after first converting metrics via `_metrics_comparison_payload`.

Differences to preserve:

- `ai_run_metadata._serialize_metrics` additionally sanitizes Matrix-visible payload values, preserving strings, integers, booleans, and `None`, and stringifying floats with `.12g`.
- `ai._metrics_comparison_payload` intentionally keeps the raw dict for internal completeness comparison, so it should not apply Matrix payload sanitization.

## Proposed Generalization

Add a small shared metrics helper only if this code is touched for metric work:

1. Move the common `Metrics | dict | None` to dict conversion into a private helper near the metadata code, for example `mindroom.ai_run_metadata.metrics_payload_dict`.
2. Reuse that helper from `ai.py` for `_metrics_comparison_payload`.
3. Keep Matrix-specific sanitization inside `_serialize_metrics`.
4. Optionally expose one `usage_metric_int(metrics_or_payload, key)` helper that accepts either `Metrics` or `dict` and returns an `int | None`.
5. Cover with the existing tests around fallback metrics and streaming usage selection.

No refactor is recommended for compaction metadata right now.
`src/mindroom/history/types.py:145`, `src/mindroom/history/types.py:259`, and `src/mindroom/delivery_gateway.py:910` are related Matrix-visible compaction serializers, but they serialize lifecycle notice events, not the AI run diagnostic payload in `src/mindroom/ai_run_metadata.py:170`.
Unifying them would require a broader schema decision and is not justified by active duplication in this primary file.

## Risk/Tests

Risk is low for a metrics-helper extraction if the Matrix sanitization boundary remains local to `ai_run_metadata.py`.
The main regression risk is accidentally changing which fallback usage fields backfill provider-reported metrics.

Tests to run for a future refactor:

- `uv run pytest tests/test_compaction.py -x -n 0 --no-cov -v`
- `uv run pytest tests/test_ai_user_id.py -x -n 0 --no-cov -v`
- Focused tests around `test_ai_run_metadata_fallback_usage_only_backfills_missing_fields` and streaming context-count tests.

Assumption: report-only audit requested; no production code was edited.
