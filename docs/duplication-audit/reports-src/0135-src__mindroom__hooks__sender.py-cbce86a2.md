# Duplication Audit: `src/mindroom/hooks/sender.py`

## Summary

Top duplication candidate: `send_hook_message` repeats the common Matrix outbound text pipeline used in delivery, tools, scheduled workflows, subagents, and thread summaries: resolve thread fallback state, build message content, call `send_message_result`, then notify the conversation cache.
The hook-specific metadata keys and sender-domain resolution make this a narrow duplication rather than a drop-in replacement.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
send_message_result	async_function	lines 19-30	related-only	send_message_result facade/import-cycle wrapper	matrix/client_delivery.py:154, matrix/client_delivery.py:410
resolve_hook_sender_domain	function	lines 33-47	related-only	MatrixID.parse user_id startswith sender_domain matrix client user_id	matrix/users.py:36, matrix/stale_stream_cleanup.py:138, bot.py:391
send_hook_message	async_function	lines 50-94	duplicate-found	format_message_with_mentions get_latest_thread_event_id_if_needed notify_outbound_message source_kind hook_source	delivery_gateway.py:523, custom_tools/matrix_conversation_operations.py:69, custom_tools/subagents.py:249, scheduling.py:730, scheduling.py:790, thread_summary.py:395, matrix/client_delivery.py:410
build_hook_message_sender	function	lines 97-133	related-only	HookMessageSender build_hook_message_sender message_sender factory	context.py:100, context.py:314, context.py:516, context.py:622, context.py:712, scheduling.py:850, orchestrator.py:979
build_hook_message_sender.<locals>._send	nested_async_function	lines 110-131	related-only	HookMessageSender Protocol trigger_dispatch send_hook_message closure	hooks/types.py:80, hooks/context.py:100, hooks/context.py:516, hooks/context.py:622, hooks/context.py:712
```

## Findings

### 1. Repeated outbound Matrix text send pipeline

`send_hook_message` at `src/mindroom/hooks/sender.py:50` performs the same core sequence as several other source paths:

- Get the latest thread event through `conversation_cache.get_latest_thread_event_id_if_needed`.
- Build Matrix message content with `format_message_with_mentions` or `build_message_content`.
- Send through `send_message_result`.
- Notify `conversation_cache.notify_outbound_message` on successful delivery.
- Return or use the delivered event ID.

Similar active flows exist in `src/mindroom/delivery_gateway.py:523`, `src/mindroom/custom_tools/matrix_conversation_operations.py:69`, `src/mindroom/custom_tools/subagents.py:249`, `src/mindroom/scheduling.py:730`, `src/mindroom/scheduling.py:790`, `src/mindroom/thread_summary.py:395`, and `src/mindroom/matrix/client_delivery.py:410`.
The duplication is behavioral, not literal.
Differences to preserve include hook metadata (`com.mindroom.source_kind`, `com.mindroom.hook_source`), scheduled workflow metadata (`com.mindroom.source_kind: scheduled`, original sender), tool-specific mention suppression/original sender behavior, notice messages, reply-to handling, and distinct `caller_label` values for cache diagnostics.

### 2. Sender-domain extraction overlaps Matrix user helpers

`resolve_hook_sender_domain` at `src/mindroom/hooks/sender.py:33` validates `client.user_id` and extracts the domain via `MatrixID.parse`.
`src/mindroom/matrix/users.py:36` has similar parsing in `_extract_domain_from_user_id`, and other call sites parse known-valid user IDs directly, such as `src/mindroom/matrix/stale_stream_cleanup.py:138`.
This is related duplication only because the hook helper returns `None` for missing or invalid identity, while `_extract_domain_from_user_id` falls back to `"localhost"`.
That fallback difference is semantically important and should not be collapsed without auditing callers.

### 3. Hook sender factory is mostly a thin adapter

`build_hook_message_sender` and its nested `_send` closure bind a Matrix client, config, runtime paths, sender domain, and conversation cache into the `HookMessageSender` protocol.
The repeated send methods in `src/mindroom/hooks/context.py:314`, `src/mindroom/hooks/context.py:516`, `src/mindroom/hooks/context.py:622`, and `src/mindroom/hooks/context.py:712` are upstream callers of the protocol, not duplicate factories.
They share the same bound-message adapter behavior through `_send_bound_message` at `src/mindroom/hooks/context.py:88`, so no additional factory generalization is recommended from this file alone.

## Proposed Generalization

A small helper could be added near Matrix delivery, for example `src/mindroom/matrix/client_delivery.py`, to send already-built content and notify an optional conversation cache:

`send_and_track_message(client, room_id, content, *, config, conversation_cache) -> str | None`

That helper would centralize the `send_message_result` plus `notify_outbound_message` tail while leaving content construction, metadata, sender-domain selection, and caller labels at existing call sites.
This is the safest generalization because the duplicated prefix logic has source-specific behavior.
No refactor is recommended for `resolve_hook_sender_domain` or `build_hook_message_sender` based on current evidence.

## Risk/Tests

Risks are mainly around thread fallback bookkeeping and cache consistency.
A refactor should preserve the exact content sent to Matrix, the exact event ID returned, and the fact that cache notification uses `delivered.content_sent`.
Tests should cover hook sends with and without `trigger_dispatch`, sends with a thread ID, failed sends returning `None`, and at least one non-hook caller to confirm the shared send-and-track tail does not change metadata.
