Summary: The strongest duplication candidate is the instance-level Agno model hook pattern shared with LLM request logging and queued-message notice hooks.
No second implementation of Vertex Claude prompt-cache breakpoint selection was found under `src`.
The cache-control payload helper is related to Codex prompt-cache helpers, but the provider behavior differs enough that no shared prompt-cache abstraction is recommended.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_vertex_claude_cache_control	function	lines 20-24	related-only	cache_control extended_cache_time ttl ephemeral prompt_cache	src/mindroom/codex_model.py:221; src/mindroom/codex_model.py:237; src/mindroom/model_loading.py:90; src/mindroom/matrix/message_content.py:25
copy_messages_with_vertex_prompt_cache_breakpoint	function	lines 27-73	none-found	copy_messages_with vertex prompt cache breakpoint cacheable_block_types model_copy deep message content cache_control	src/mindroom/ai_runtime.py:58; src/mindroom/execution_preparation.py:405; src/mindroom/vertex_claude_compat.py:10; src/mindroom/history/compaction.py:1340
install_vertex_claude_prompt_cache_hook	function	lines 76-174	duplicate-found	install hook vars(model) hook attr invoke ainvoke invoke_stream ainvoke_stream model_dict assignment	src/mindroom/llm_request_logging.py:334; src/mindroom/ai_runtime.py:270; src/mindroom/model_loading.py:169; src/mindroom/model_loading.py:175
install_vertex_claude_prompt_cache_hook.<locals>._prepare_messages	nested_function	lines 89-92	none-found	prepare messages cache_system_prompt copy_messages_with_vertex_prompt_cache_breakpoint	src/mindroom/ai_runtime.py:58; src/mindroom/ai_runtime.py:65; src/mindroom/ai_runtime.py:84; src/mindroom/execution_preparation.py:405
install_vertex_claude_prompt_cache_hook.<locals>._invoke_with_prompt_cache	nested_function	lines 94-111	related-only	invoke wrapper original_invoke messages assistant_message response_format tools tool_choice run_response compress_tool_results	src/mindroom/codex_model.py:341; src/mindroom/llm_request_logging.py:351; src/mindroom/ai_runtime.py:281
install_vertex_claude_prompt_cache_hook.<locals>._ainvoke_with_prompt_cache	nested_async_function	lines 113-130	duplicate-found	ainvoke wrapper original_ainvoke messages assistant_message response_format tools tool_choice run_response compress_tool_results	src/mindroom/llm_request_logging.py:351; src/mindroom/codex_model.py:369; src/mindroom/model_loading.py:169
install_vertex_claude_prompt_cache_hook.<locals>._invoke_stream_with_prompt_cache	nested_function	lines 132-149	related-only	invoke_stream wrapper original_invoke_stream yield from messages assistant_message response_format tools tool_choice run_response compress_tool_results	src/mindroom/codex_model.py:355; src/mindroom/llm_request_logging.py:367
install_vertex_claude_prompt_cache_hook.<locals>._ainvoke_stream_with_prompt_cache	nested_async_function	lines 151-169	duplicate-found	ainvoke_stream wrapper original_ainvoke_stream async for chunk yield messages assistant_message response_format tools tool_choice run_response compress_tool_results	src/mindroom/llm_request_logging.py:367; src/mindroom/codex_model.py:383; src/mindroom/model_loading.py:169
```

## Findings

1. Instance-level model hook installation is repeated.
`src/mindroom/vertex_claude_prompt_cache.py:76` installs a one-time hook by reading `vars(model)`, checking an installed marker, capturing original model methods, and replacing methods on the model instance.
`src/mindroom/llm_request_logging.py:334` does the same for `ainvoke` and `ainvoke_stream`, including a marker in `vars(model)` and instance method replacement.
`src/mindroom/ai_runtime.py:270` uses the same lifecycle pattern for queued-message notice hooks, with marker installation and replacement of model call helpers.
The behavior is functionally duplicated at the hook-management level: all three need idempotent per-instance wrapping while preserving the original callable.
Differences to preserve: the Vertex hook is provider-gated and wraps four invoke variants; logging is debug-gated and wraps only async request paths; queued-message notice wraps formatting/media helpers and tolerates missing private methods.

2. Async invoke and async stream wrappers duplicate the logging wrapper shape.
`src/mindroom/vertex_claude_prompt_cache.py:113` and `src/mindroom/vertex_claude_prompt_cache.py:151` wrap `original_ainvoke` and `original_ainvoke_stream` after transforming `messages`.
`src/mindroom/llm_request_logging.py:351` and `src/mindroom/llm_request_logging.py:367` wrap the same async model entry points before delegating to originals.
The shared behavior is not prompt-cache-specific; it is "run pre-processing, then delegate to the captured original async model method."
Differences to preserve: logging snapshots context at wrapper-call time and writes an async side effect before delegation, while Vertex prompt cache only rewrites the `messages` argument and must keep the original Agno signature.

3. Prompt-cache configuration helpers are related, but not duplicates worth merging.
`src/mindroom/vertex_claude_prompt_cache.py:20` creates Anthropic-on-Vertex `cache_control` blocks based on `extended_cache_time`.
`src/mindroom/codex_model.py:221` and `src/mindroom/codex_model.py:237` create Codex prompt-cache request headers and extra body fields.
These are both provider prompt-cache adapters, but their wire formats and insertion points differ: Vertex mutates message content blocks, while Codex augments request params.
No shared helper would reduce meaningful duplication without hiding provider-specific rules.

No meaningful duplication was found for `copy_messages_with_vertex_prompt_cache_breakpoint`.
Other message-copying helpers such as `src/mindroom/ai_runtime.py:58`, `src/mindroom/execution_preparation.py:405`, and `src/mindroom/history/compaction.py:1340` only share safe-copy mechanics, not the same last-cacheable-user-block selection behavior.
`src/mindroom/vertex_claude_compat.py:10` is related because it returns sanitized Vertex-compatible payloads without mutating caller input, but it handles tool definitions rather than messages.

## Proposed Generalization

A small helper could live near model runtime plumbing, for example `src/mindroom/model_hooks.py`, to centralize idempotent per-instance hook installation:

1. Add a typed helper that accepts `model`, `installed_attr`, and a mapping of replacement callables.
2. Have the helper check `vars(model).get(installed_attr) is True`, set the marker, and assign replacements into the model instance dictionary.
3. Keep provider-specific wrapper functions in their current modules.
4. Migrate `install_llm_request_logging`, `install_queued_message_notice_hook`, and `install_vertex_claude_prompt_cache_hook` only if the helper makes each installer shorter without weakening signatures.
5. Add focused tests for idempotency and wrapper ordering.

No refactor is recommended for the Vertex prompt-cache breakpoint logic or provider prompt-cache payload helpers.

## Risk/tests

The main risk in generalizing hook installation is wrapper ordering.
`src/mindroom/model_loading.py:169` installs LLM request logging before `src/mindroom/model_loading.py:175` installs the Vertex prompt-cache hook, so the Vertex hook currently captures already-logged async methods.
A shared helper must preserve that order and must not overwrite markers before all originals are captured.

Tests needing attention:
`tests/test_extra_kwargs.py` for Vertex prompt-cache behavior and idempotent wrapping.
`tests/test_llm_request_logging.py` and `tests/test_issue_154_logging_integration.py` for async logging wrappers.
`tests/test_queued_message_notify.py` for queued-message hook idempotency and missing-method behavior.

Assumption: this audit is report-only, so no production code or tests were edited.
