# Duplication Audit: `src/mindroom/custom_tools/attachment_helpers.py`

## Summary

Top duplication candidate: `resolve_requested_room_id` overlaps with `MatrixApiTools._resolve_room_id` in `src/mindroom/custom_tools/matrix_api.py`, with the same fallback-to-context and strip/non-empty validation flow but stricter Matrix-room-ID validation and different error text in `matrix_api`.

Related-only patterns exist for thread fallback and string-list normalization, but the nearby helpers either already call this module or intentionally preserve different semantics.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
normalize_str_list	function	lines 16-28	related-only	normalize_str_list; _normalize_str_list; entries must be strings; list string validation	src/mindroom/tool_system/skills.py:531; src/mindroom/custom_tools/matrix_message.py:250; src/mindroom/custom_tools/matrix_message.py:260
room_access_allowed	function	lines 31-44	related-only	room_access_allowed; is_authorized_sender; room_alias; requester_id	src/mindroom/bot.py:1423; src/mindroom/approval_inbound.py:82; src/mindroom/bot_room_lifecycle.py:199; src/mindroom/custom_tools/attachments.py:348; src/mindroom/custom_tools/matrix_api.py:1483; src/mindroom/custom_tools/matrix_room.py:501; src/mindroom/custom_tools/thread_summary.py:67; src/mindroom/custom_tools/thread_tags.py:108
resolve_requested_room_id	function	lines 47-59	duplicate-found	resolve_requested_room_id; _resolve_room_id; room_id.strip; room_id must be omitted; room_id must be non-empty	src/mindroom/custom_tools/matrix_api.py:212; src/mindroom/custom_tools/thread_summary.py:57; src/mindroom/custom_tools/thread_tags.py:99; src/mindroom/custom_tools/thread_tags.py:190; src/mindroom/custom_tools/thread_tags.py:300
resolve_context_thread_id	function	lines 62-77	related-only	resolve_context_thread_id; resolved_thread_id fallback; room_timeline_sentinel; effective_thread_id	src/mindroom/custom_tools/attachments.py:338; src/mindroom/custom_tools/matrix_conversation_operations.py:724; src/mindroom/custom_tools/matrix_conversation_operations.py:749; src/mindroom/custom_tools/matrix_conversation_operations.py:769; src/mindroom/custom_tools/matrix_conversation_operations.py:782; src/mindroom/custom_tools/matrix_message.py:141; src/mindroom/custom_tools/thread_tags.py:217; src/mindroom/custom_tools/thread_tags.py:329
CanonicalToolThreadTarget	class	lines 81-86	related-only	CanonicalToolThreadTarget; requested_thread_id; canonical_thread_id; resolved_thread_id dataclass	src/mindroom/message_target.py:13; src/mindroom/custom_tools/thread_summary.py:99; src/mindroom/custom_tools/thread_tags.py:143; src/mindroom/custom_tools/thread_tags.py:253; src/mindroom/custom_tools/thread_tags.py:393
resolve_canonical_tool_thread_target	async_function	lines 89-130	related-only	resolve_canonical_tool_thread_target; normalize_thread_id; canonical thread root; resolve_thread_root_event_id_for_client	src/mindroom/custom_tools/thread_summary.py:82; src/mindroom/custom_tools/thread_tags.py:126; src/mindroom/custom_tools/thread_tags.py:234; src/mindroom/custom_tools/thread_tags.py:376; src/mindroom/conversation_resolver.py:214
```

## Findings

### 1. Room-id argument resolution is duplicated with stricter validation in `matrix_api`

`resolve_requested_room_id` in `src/mindroom/custom_tools/attachment_helpers.py:47` and `MatrixApiTools._resolve_room_id` in `src/mindroom/custom_tools/matrix_api.py:212` both implement the same core behavior:

- If `room_id` is omitted, use `context.room_id`.
- If present, require a string.
- Strip whitespace.
- Reject an empty result.
- Return `(resolved_room_id, error)`.

The difference to preserve is that `matrix_api` also requires a Matrix room ID in `!room:server` form at `src/mindroom/custom_tools/matrix_api.py:224`, while `resolve_requested_room_id` intentionally accepts aliases or any non-empty target string for broader custom-tool use.

### 2. Thread-context fallback is already centralized for most custom tools

`resolve_context_thread_id` is actively shared by `matrix_message`, `matrix_conversation_operations`, and `thread_tags`.

`_resolve_send_target` in `src/mindroom/custom_tools/attachments.py:338` has a related subset of the same fallback behavior: explicit `thread_id` wins, otherwise inherit `context.resolved_thread_id` only when targeting the current room.

That helper also performs room authorization and joined-room checks, so it is not a clean duplicate of `resolve_context_thread_id`.

### 3. String-list normalization is related but not behaviorally equivalent

`normalize_str_list` rejects non-string entries and strips out empty strings.

`_normalize_str_list` in `src/mindroom/tool_system/skills.py:531` accepts a single string as a list, accepts tuple/set inputs, silently ignores non-string entries, and does not strip strings.

These are similar names and adjacent concepts, but not duplicate behavior.

### 4. Authorization checks are related but context-specific

`room_access_allowed` wraps `is_authorized_sender` for tool runtime contexts and allows the current room immediately.

Other authorization call sites in `src/mindroom/bot.py:1423`, `src/mindroom/approval_inbound.py:82`, and `src/mindroom/bot_room_lifecycle.py:199` call `is_authorized_sender` directly because they have a Matrix room object with a canonical alias and no `ToolRuntimeContext`.

These are related access checks, not direct duplication.

## Proposed Generalization

Only one narrow refactor is worth considering:

1. Extend `resolve_requested_room_id` with optional parameters for `error_message` and `require_matrix_room_id`.
2. Replace `MatrixApiTools._resolve_room_id` with a call to that shared helper.
3. Preserve the current `matrix_api` error strings and `!room:server` validation through those parameters.

No refactor is recommended for `normalize_str_list`, `room_access_allowed`, `resolve_context_thread_id`, `CanonicalToolThreadTarget`, or `resolve_canonical_tool_thread_target`.

## Risk/Tests

Risk is low if only `matrix_api` room-id resolution is deduplicated, but the exact error text and alias-vs-room-ID acceptance must remain unchanged.

Focused tests should cover omitted room ID, non-string room ID, whitespace-only room ID, alias input where allowed, alias input where disallowed, and valid `!room:server` input.

No production code was edited for this audit.
