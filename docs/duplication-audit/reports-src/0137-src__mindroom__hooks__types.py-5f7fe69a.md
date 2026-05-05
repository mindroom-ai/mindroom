## Summary

Top duplication candidates:

- `HookMatrixAdmin` duplicates the public method surface of `_BoundHookMatrixAdmin`, but this is intentional protocol-to-implementation mirroring rather than duplicated behavior.
- `EnrichmentItem` construction is repeated in `MessageEnrichContext.add_metadata` and `SystemEnrichContext.add_instruction`; the behavior is the same append operation with different hook-facing names.
- `HookMessageSender`, `HookCallback`, `RegisteredHook`, and event-name timeout/validation helpers are central source-of-truth types or small helpers with no meaningful duplicated behavior found elsewhere in `src`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
HookMessageSender	class	lines 80-93	related-only	HookMessageSender source_hook trigger_dispatch send_response	src/mindroom/hooks/sender.py:57; src/mindroom/hooks/sender.py:104; src/mindroom/bot.py:1641; src/mindroom/turn_controller.py:872
HookMessageSender.__call__	method	lines 83-93	related-only	hook message sender callable room_id body thread_id source_hook	src/mindroom/hooks/sender.py:57; src/mindroom/hooks/sender.py:114; src/mindroom/hooks/context.py:88; src/mindroom/hooks/context.py:299
HookMatrixAdmin	class	lines 96-119	related-only	HookMatrixAdmin resolve_alias create_room invite_user get_room_members add_room_to_space	src/mindroom/hooks/matrix_admin.py:20; src/mindroom/runtime_protocols.py:56; src/mindroom/commands/handler.py:92; src/mindroom/scheduling.py:165
HookMatrixAdmin.resolve_alias	async_method	lines 99-100	related-only	resolve_alias room_resolve_alias room alias	src/mindroom/hooks/matrix_admin.py:26; src/mindroom/matrix/rooms.py:277; src/mindroom/matrix/rooms.py:466
HookMatrixAdmin.create_room	async_method	lines 102-110	related-only	create_room alias_localpart topic power_user_ids	src/mindroom/hooks/matrix_admin.py:33; src/mindroom/matrix/client_room_admin.py:38; src/mindroom/matrix/rooms.py:516
HookMatrixAdmin.invite_user	async_method	lines 112-113	related-only	invite_user invite_to_room room_id user_id	src/mindroom/hooks/matrix_admin.py:50; src/mindroom/matrix/client_room_admin.py:24; src/mindroom/orchestrator.py:1505; src/mindroom/orchestrator.py:1527
HookMatrixAdmin.get_room_members	async_method	lines 115-116	related-only	get_room_members joined members set[str]	src/mindroom/hooks/matrix_admin.py:54; src/mindroom/matrix/client_room_admin.py:405; src/mindroom/orchestrator.py:1501; src/mindroom/matrix/room_cleanup.py:104
HookMatrixAdmin.add_room_to_space	async_method	lines 118-119	related-only	add_room_to_space space_room_id server_name	src/mindroom/hooks/matrix_admin.py:58; src/mindroom/matrix/client_room_admin.py:351; src/mindroom/matrix/rooms.py:516
HookCallback	class	lines 132-136	related-only	HookCallback discovered_hooks callback coroutinefunction	src/mindroom/hooks/decorators.py:39; src/mindroom/hooks/registry.py:28; src/mindroom/tool_system/plugins.py:65
HookCallback.__call__	method	lines 135-136	related-only	callback ctx Awaitable object None hook.callback	src/mindroom/hooks/execution.py:210; src/mindroom/hooks/decorators.py:46
EnrichmentItem	class	lines 140-145	duplicate-found	EnrichmentItem key text cache_policy add_metadata add_instruction	src/mindroom/hooks/context.py:395; src/mindroom/hooks/context.py:405; src/mindroom/hooks/context.py:415; src/mindroom/hooks/context.py:425; src/mindroom/hooks/enrichment.py:14
RegisteredHook	class	lines 149-162	related-only	RegisteredHook hook registry priority plugin_order source_lineno timeout_ms	src/mindroom/hooks/registry.py:75; src/mindroom/hooks/execution.py:93; src/mindroom/hooks/execution.py:201; src/mindroom/hooks/execution.py:235
is_custom_event_name	function	lines 165-167	none-found	is_custom_event_name BUILTIN_EVENT_NAMES custom event name	none
default_timeout_ms_for_event	function	lines 170-172	none-found	default_timeout_ms_for_event DEFAULT_EVENT_TIMEOUT_MS DEFAULT_CUSTOM_EVENT_TIMEOUT_MS timeout_ms	src/mindroom/hooks/execution.py:201; src/mindroom/hooks/registry.py:80
validate_event_name	function	lines 175-188	related-only	validate_event_name EVENT_NAME_PATTERN reserved namespace hook event	src/mindroom/hooks/decorators.py:41; src/mindroom/tool_system/runtime_context.py:558; src/mindroom/config/plugin.py:26; src/mindroom/tool_system/plugin_identity.py:13; src/mindroom/mcp/config.py:22
```

## Findings

### 1. Enrichment append helpers duplicate the same construction behavior

- `src/mindroom/hooks/context.py:397` `MessageEnrichContext.add_metadata` appends `EnrichmentItem(key=key, text=text, cache_policy=cache_policy)` to `_items`.
- `src/mindroom/hooks/context.py:417` `SystemEnrichContext.add_instruction` appends the same `EnrichmentItem` shape to `_items`.
- Both methods use the same default cache policy and the same storage field.

Differences to preserve:

- The public method names are domain-specific and should remain distinct because hook authors call `add_metadata` for message enrichment and `add_instruction` for system enrichment.
- The docstrings and contexts communicate different semantics even though the append operation is the same.

### 2. HookMatrixAdmin protocol mirrors its bound implementation

- `src/mindroom/hooks/types.py:96` declares the hook-facing Matrix admin protocol.
- `src/mindroom/hooks/matrix_admin.py:20` implements the same five methods on `_BoundHookMatrixAdmin`.

This is structural typing, not harmful duplication.
The protocol is the public contract while `_BoundHookMatrixAdmin` adapts `nio.AsyncClient` and existing `matrix.client_room_admin` helpers.
No refactor is recommended unless another concrete implementation appears.

### 3. Event name validation has similar shape to other identifier validators, but not duplicated behavior

- `src/mindroom/hooks/types.py:175` trims, accepts built-in hook event names, validates a colon-delimited pattern, and rejects reserved hook namespaces.
- `src/mindroom/tool_system/plugin_identity.py:13`, `src/mindroom/mcp/config.py:22`, and `src/mindroom/matrix_identifiers.py:13` also normalize strings with regex checks, but each uses different grammar, normalization rules, and error messages.

This is related validation structure, not reusable duplicated behavior.
Keeping hook event validation local preserves the built-in/reserved namespace rules.

## Proposed Generalization

No production refactor recommended for this audit.

If the enrichment append duplication grows, a minimal future cleanup would be:

1. Add a private helper or mixin in `src/mindroom/hooks/context.py` that appends an `EnrichmentItem` to `_items`.
2. Keep `add_metadata` and `add_instruction` as the public hook-author methods.
3. Route both methods through the private helper.
4. Add focused tests for both context methods preserving method names and default cache policy.

## Risk/Tests

- Refactoring `HookMatrixAdmin` is not worthwhile now because the protocol and implementation intentionally mirror each other.
- Refactoring `validate_event_name` into a generic identifier validator would risk weakening hook-specific built-in and reserved namespace behavior.
- If the enrichment append helper is introduced later, tests should cover `MessageEnrichContext.add_metadata`, `SystemEnrichContext.add_instruction`, returned collector items from `emit_collect`, and rendering order in `render_system_enrichment_block`.
