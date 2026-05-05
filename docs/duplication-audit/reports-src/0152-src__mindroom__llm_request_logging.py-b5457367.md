## Summary

Top duplication candidates in `src/mindroom/llm_request_logging.py`:

1. Async iterator context rebinding and explicit `aclose()` handling is near-duplicated in `src/mindroom/tool_system/worker_routing.py` and `src/mindroom/tool_system/runtime_context.py`.
2. `_normalized_string_list` is literally duplicated in `src/mindroom/ai.py` and used for the same Matrix metadata de-duplication behavior.
3. `_json_safe` overlaps with JSON-normalization logic in `src/mindroom/tool_system/output_files.py`, but the error and fallback semantics differ enough that it should not be extracted blindly.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_AsyncClosableIterator	class	lines 33-37	duplicate-found	AsyncClosableIterator aclose Protocol stream_with_*	src/mindroom/tool_system/runtime_context.py:51, src/mindroom/tool_system/worker_routing.py:44
_AsyncClosableIterator.aclose	async_method	lines 36-37	duplicate-found	aclose Protocol async iterator close	src/mindroom/tool_system/runtime_context.py:55, src/mindroom/tool_system/worker_routing.py:48
_daily_log_path	function	lines 74-76	related-only	jsonl log path daily log path tool_call_log_path	src/mindroom/tool_system/tool_calls.py:419
_system_prompt	function	lines 79-83	none-found	system_prompt messages role system get_content_string	none
_model_params	function	lines 86-101	related-only	model_params_payload model params dataclass fields json.dumps	src/mindroom/ai.py:1014, src/mindroom/teams.py:1504, src/mindroom/api/openai_compat.py:1494
_write_jsonl_line	function	lines 104-108	related-only	jsonl mkdir parents write json dumps append	src/mindroom/tool_system/tool_calls.py:419, src/mindroom/tool_system/tool_calls.py:445
_json_safe	function	lines 111-131	related-only	json_safe normalize_json_value BaseModel asdict Path bytes base64	src/mindroom/tool_system/output_files.py:341, src/mindroom/tool_system/sandbox_proxy.py:663
_request_message_payloads	function	lines 134-143	none-found	model_dump mode python exclude non api message fields	none
_request_messages	function	lines 146-149	none-found	isinstance list Message kwargs messages	none
_request_tools	function	lines 152-155	none-found	isinstance list dict kwargs tools	none
_normalized_string_list	function	lines 158-165	duplicate-found	normalized_string_list list str unique source event ids	src/mindroom/ai.py:364
_snapshot_request_log_context	function	lines 168-170	related-only	ContextVar get json safe snapshot current context	src/mindroom/tool_system/runtime_context.py:395, src/mindroom/tool_system/worker_routing.py:101
current_llm_request_log_context	function	lines 173-175	related-only	current request log context getter ContextVar	src/mindroom/tool_system/runtime_context.py:395, src/mindroom/tool_system/worker_routing.py:101
model_params_payload	function	lines 178-180	related-only	model_params_payload model_params metadata logging	src/mindroom/ai.py:1022, src/mindroom/teams.py:1512, src/mindroom/api/openai_compat.py:1502
build_llm_request_log_context	function	lines 183-237	related-only	build request log context source event ids source event prompts metadata	src/mindroom/ai.py:614, src/mindroom/teams.py:111, src/mindroom/api/openai_compat.py:259, src/mindroom/turn_store.py:171, src/mindroom/history/interrupted_replay.py:220
bind_llm_request_log_context	function	lines 241-253	related-only	ContextVar set reset contextmanager bind context	src/mindroom/tool_system/runtime_context.py:590, src/mindroom/tool_system/worker_routing.py:115, src/mindroom/ai_runtime.py:112
stream_with_llm_request_log_context	async_function	lines 256-276	duplicate-found	stream_with context identity async iterator anext StopAsyncIteration aclose	src/mindroom/tool_system/worker_routing.py:135, src/mindroom/tool_system/runtime_context.py:357
write_llm_request_log	async_function	lines 279-307	related-only	write jsonl request record timestamp model messages tools model_params	src/mindroom/tool_system/tool_calls.py:400, src/mindroom/tool_system/tool_calls.py:445
_write_llm_request_log_if_present	async_function	lines 310-331	none-found	write if kwargs messages request tools	none
install_llm_request_logging	function	lines 334-386	related-only	install hook model ainvoke ainvoke_stream installed attr vars model	src/mindroom/vertex_claude_prompt_cache.py:76
install_llm_request_logging.<locals>._logged_ainvoke	nested_function	lines 351-365	related-only	wrap ainvoke nested function snapshot context original_ainvoke	src/mindroom/vertex_claude_prompt_cache.py:113
install_llm_request_logging.<locals>._invoke	nested_async_function	lines 354-363	related-only	nested async invoke write before original_ainvoke	src/mindroom/vertex_claude_prompt_cache.py:113
install_llm_request_logging.<locals>._logged_ainvoke_stream	nested_function	lines 367-382	related-only	wrap ainvoke_stream nested function async iterator	src/mindroom/vertex_claude_prompt_cache.py:151
install_llm_request_logging.<locals>._stream	nested_async_function	lines 370-380	related-only	nested async stream write before original_ainvoke_stream async for	src/mindroom/vertex_claude_prompt_cache.py:151
```

## Findings

### 1. Async stream context rebinding is duplicated across three context systems

`stream_with_llm_request_log_context()` in `src/mindroom/llm_request_logging.py:256` performs a specific async-iterator wrapping pattern:

- Bind a context before creating or entering the iterator.
- Re-bind the context around each `__anext__()` pull.
- Yield outside the bound context so ContextVar tokens do not span consumer code.
- Explicitly close async generators or objects matching `_AsyncClosableIterator`.

The same behavior appears in `stream_with_tool_execution_identity()` at `src/mindroom/tool_system/worker_routing.py:135` and `ToolRuntimeSupport.stream_in_context()` at `src/mindroom/tool_system/runtime_context.py:357`.
Both files also duplicate the `_AsyncClosableIterator` protocol at `src/mindroom/tool_system/worker_routing.py:44` and `src/mindroom/tool_system/runtime_context.py:51`.

Differences to preserve:

- LLM request logging accepts an already-created `AsyncIterator` and an explicit request-context dict.
- Worker routing accepts a `stream_factory`.
- Runtime context wraps a `stream_factory` inside an instance method and uses `_tool_runtime_context_scope()`.
- The three wrappers bind different context managers.

This is real functional duplication, not just similar syntax.
A tiny shared helper could accept a context-manager factory and a stream factory/iterator factory.

### 2. String-list normalization is duplicated between request logging and AI metadata

`_normalized_string_list()` in `src/mindroom/llm_request_logging.py:158` is a literal duplicate of `_normalized_string_list()` in `src/mindroom/ai.py:364`.
Both accept an object, return `[]` unless it is a list, keep only non-empty strings, and preserve first-seen order while removing duplicates.

Both functions are used for Matrix metadata event-id handling:

- LLM request logs merge `reply_to_event_id` and `MATRIX_SOURCE_EVENT_IDS_METADATA_KEY` in `src/mindroom/llm_request_logging.py:218`.
- Matrix run metadata merges reply/source/seen/unseen event IDs in `src/mindroom/ai.py:406`.

Differences to preserve:

- None in the normalization behavior.
- The consumers have different metadata keys and output shapes, so only the normalizer should be shared.

### 3. JSON-safe normalization overlaps with tool output JSON normalization, but semantics differ

`_json_safe()` in `src/mindroom/llm_request_logging.py:111` recursively converts Pydantic models, dataclasses, dicts, iterables, bytes, `Path`, primitives, and unknown objects into a JSON-safe value.
`_normalize_json_value()` in `src/mindroom/tool_system/output_files.py:341` covers the same broad type family: primitives, `Path`, Pydantic models, dataclasses, mappings, tuples/lists, and sets.

This is related behavior, but not an immediate duplicate extraction candidate because the differences are semantically important:

- `_json_safe()` accepts bytes and records them as base64 metadata.
- `_json_safe()` falls back to `repr()` for unknown values so request logging remains best-effort.
- `_normalize_json_value()` rejects unknown values with `TypeError`, normalizes enums, and sorts sets/frozensets for stable output files.
- `_json_safe()` uses Pydantic `mode="python"` and excludes `None`; `_normalize_json_value()` uses `mode="json"`.

The overlap is worth tracking if more durable JSON normalization variants appear, but a shared helper would need policy parameters and would likely be more complex than the current local functions.

### 4. Model method monkey-patching has related hook-install behavior

`install_llm_request_logging()` in `src/mindroom/llm_request_logging.py:334` and `install_vertex_claude_prompt_cache_hook()` in `src/mindroom/vertex_claude_prompt_cache.py:76` both:

- Guard with a model-local installed marker stored in `vars(model)`.
- Capture original model methods.
- Assign wrapper callables back onto the model instance.

The actual behaviors differ: request logging wraps `ainvoke` and `ainvoke_stream` to emit JSONL records, while prompt-cache wrapping adjusts Vertex Claude messages for sync and async invoke/stream variants.
This is related hook-install scaffolding rather than a clear dedupe target because a generic monkey-patch installer would add indirection around small, provider-specific wrappers.

## Proposed Generalization

Recommended minimal refactor candidates:

1. Extract a tiny async iterator context wrapper helper, for example `mindroom.context_streaming.stream_with_context_scope(stream_factory, scope_factory)`.
   It should own the repeated `anext()` loop, `StopAsyncIteration`, and explicit `aclose()` protocol.
2. Move `_AsyncClosableIterator` to that helper module if the stream helper is extracted.
3. Extract the duplicate string-list normalizer to a small shared Matrix metadata utility, for example `mindroom.matrix.metadata.normalize_string_list()`, and use it from `ai.py` and `llm_request_logging.py`.

No refactor recommended for `_json_safe()` or model hook installation at this time.

## Risk/tests

For the stream helper:

- Risk is ContextVar lifetime regression if tokens span yields or if stream cleanup runs outside the intended context.
- Add focused async tests that assert the bound context is visible during stream creation, each item pull, and `aclose()`, but not leaked after iteration.
- Cover normal completion, early consumer break, and stream creation returning an async generator with `aclose()`.

For the string-list normalizer:

- Risk is low because the two implementations are identical.
- Add or update tests around duplicate event IDs, empty strings, non-list values, and order preservation in both Matrix run metadata and request-log context.

For JSON normalization:

- No extraction recommended, so no migration tests are needed.
- If unified later, tests must pin bytes handling, unknown-object fallback versus rejection, enum normalization, set ordering, and Pydantic dump modes.
