Summary: top duplication candidates are the repeated JSON tool payload/context-error helpers across custom Matrix tools, the repeated bounded limit helper shape, and the repeated Matrix tool preflight flow of runtime-context lookup, room resolution, authorization, rate limiting, and dispatch.
The message-operation behavior itself is already mostly factored into `matrix_conversation_operations.py`; no broad refactor is recommended from this file alone.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MatrixMessageTools	class	lines 18-312	duplicate-found	MatrixMessageTools MatrixRoomTools MatrixApiTools Toolkit payload context rate_limit room_access_allowed	src/mindroom/custom_tools/matrix_room.py:36; src/mindroom/custom_tools/matrix_api.py:92; src/mindroom/custom_tools/thread_summary.py:19; src/mindroom/custom_tools/thread_tags.py:64
MatrixMessageTools.__init__	method	lines 36-40	related-only	Toolkit __init__ name tools matrix_message matrix_room thread_summary	src/mindroom/custom_tools/matrix_room.py:50; src/mindroom/custom_tools/thread_summary.py:22; src/mindroom/custom_tools/thread_tags.py:67
MatrixMessageTools._payload	method	lines 43-46	duplicate-found	def _payload json.dumps sort_keys status tool custom_tools	src/mindroom/custom_tools/matrix_room.py:57; src/mindroom/custom_tools/thread_summary.py:29; src/mindroom/custom_tools/thread_tags.py:74; src/mindroom/custom_tools/matrix_api.py:144; src/mindroom/custom_tools/subagents.py:43; src/mindroom/custom_tools/attachments.py:67
MatrixMessageTools._operation_result_payload	method	lines 49-53	related-only	MatrixMessageOperationResult status fields result payload dispatch_action	src/mindroom/custom_tools/matrix_conversation_operations.py:49; src/mindroom/custom_tools/matrix_conversation_operations.py:65; src/mindroom/custom_tools/matrix_room.py:184
MatrixMessageTools._context_error	method	lines 56-60	duplicate-found	tool context is unavailable runtime path _context_error custom_tools	src/mindroom/custom_tools/matrix_room.py:62; src/mindroom/custom_tools/thread_summary.py:34; src/mindroom/custom_tools/thread_tags.py:79; src/mindroom/custom_tools/matrix_api.py:149; src/mindroom/custom_tools/subagents.py:52; src/mindroom/custom_tools/attachments.py:409
MatrixMessageTools._read_limit	method	lines 63-66	duplicate-found	bounded limit default max max(1 min(limit custom_tools	src/mindroom/custom_tools/matrix_room.py:70; src/mindroom/custom_tools/subagents.py:198; src/mindroom/custom_tools/matrix_api.py:334
MatrixMessageTools._action_supports_attachments	method	lines 69-70	none-found	action supports attachments send reply thread-reply attachment_ids attachment_file_paths	src/mindroom/custom_tools/matrix_conversation_operations.py:140; src/mindroom/custom_tools/attachments.py:647
MatrixMessageTools._validate_matrix_message_request	method	lines 72-111	duplicate-found	VALID_ACTIONS attachment_count room_access_allowed unsupported action Not authorized target room	src/mindroom/custom_tools/matrix_room.py:493; src/mindroom/custom_tools/matrix_room.py:500; src/mindroom/custom_tools/matrix_api.py:1483; src/mindroom/custom_tools/thread_summary.py:57; src/mindroom/custom_tools/thread_tags.py:108
MatrixMessageTools._check_rate_limit	method	lines 114-130	duplicate-found	check_rate_limit rate_limit_lock recent_actions RATE_LIMIT_WINDOW_SECONDS max_actions tool_name	src/mindroom/custom_tools/matrix_room.py:94; src/mindroom/custom_tools/matrix_api.py:514; src/mindroom/custom_tools/matrix_helpers.py:15
MatrixMessageTools._message_context	method	lines 132-164	related-only	context action room_id thread_id reply_to_event_id requester_id agent_name resolve_context_thread_id	src/mindroom/custom_tools/attachment_helpers.py:62; src/mindroom/tool_system/runtime_context.py:67; src/mindroom/response_runner.py:178
MatrixMessageTools.matrix_message	async_method	lines 166-312	duplicate-found	get_tool_runtime_context normalize request validate room access rate limit dispatch action Matrix tool	src/mindroom/custom_tools/matrix_room.py:458; src/mindroom/custom_tools/matrix_api.py:1433; src/mindroom/custom_tools/thread_summary.py:42; src/mindroom/custom_tools/thread_tags.py:95; src/mindroom/custom_tools/matrix_conversation_operations.py:57
```

Findings:

1. JSON payload and context-error helpers are duplicated across custom tools.
`MatrixMessageTools._payload` and `_context_error` at `src/mindroom/custom_tools/matrix_message.py:43` and `src/mindroom/custom_tools/matrix_message.py:56` repeat the same sorted JSON object shape used by `MatrixRoomTools._payload`/`_context_error` at `src/mindroom/custom_tools/matrix_room.py:57` and `src/mindroom/custom_tools/matrix_room.py:62`, `ThreadSummaryTools._payload`/`_context_error` at `src/mindroom/custom_tools/thread_summary.py:29` and `src/mindroom/custom_tools/thread_summary.py:34`, and `MatrixApiTools._payload`/`_context_error` at `src/mindroom/custom_tools/matrix_api.py:144` and `src/mindroom/custom_tools/matrix_api.py:149`.
The behavior is functionally the same: build `{"status": ..., "tool": ...}`, merge extra fields, and serialize with `sort_keys=True`; context errors differ only in tool name and message text, with some tools adding an `action`.
This duplication is small but active across many custom tools.

2. Bounded limit clamping is repeated.
`MatrixMessageTools._read_limit` at `src/mindroom/custom_tools/matrix_message.py:63` duplicates the shape of `MatrixRoomTools._thread_limit` at `src/mindroom/custom_tools/matrix_room.py:70` and `_bounded_limit` in `src/mindroom/custom_tools/subagents.py:198`.
Each defaults `None` and clamps to a minimum of `1` and a tool-specific maximum.
The behavior differences to preserve are default values and maximum caps; `matrix_api` search validation at `src/mindroom/custom_tools/matrix_api.py:334` is related but stricter because it rejects non-integer and out-of-range input instead of clamping.

3. Matrix tool preflight flow is repeated between `matrix_message`, `matrix_room`, and related room-scoped tools.
`MatrixMessageTools.matrix_message` at `src/mindroom/custom_tools/matrix_message.py:245` gets runtime context, normalizes input, resolves `room_id`, checks allowed actions, checks `room_access_allowed`, applies a per-room rate limit, and dispatches.
`MatrixRoomTools.matrix_room` follows the same high-level flow at `src/mindroom/custom_tools/matrix_room.py:479`, `src/mindroom/custom_tools/matrix_room.py:493`, `src/mindroom/custom_tools/matrix_room.py:500`, and `src/mindroom/custom_tools/matrix_room.py:509`.
`MatrixApiTools.matrix_api` also repeats the runtime context and room authorization part at `src/mindroom/custom_tools/matrix_api.py:1433` and `src/mindroom/custom_tools/matrix_api.py:1483`.
Differences to preserve include `matrix_message` allowing `context` without room authorization, attachment validation and weighted rate limiting, `matrix_room` read-only action names and normalizer dataclass, and `matrix_api` write-specific policy and audit behavior.

4. Rate-limit implementation is mostly centralized, but one related duplicate remains outside the primary file.
`MatrixMessageTools._check_rate_limit` at `src/mindroom/custom_tools/matrix_message.py:114` and `MatrixRoomTools._check_rate_limit` at `src/mindroom/custom_tools/matrix_room.py:94` correctly delegate to `src/mindroom/custom_tools/matrix_helpers.py:15`.
`MatrixApiTools._check_rate_limit` at `src/mindroom/custom_tools/matrix_api.py:514` still implements the same sliding-window logic inline with action weights and a different message.
This is a real duplicate of the helper algorithm, but it is not production code in the primary file because `matrix_message` already uses the helper.

Related-only notes:

`MatrixMessageTools._operation_result_payload` is a thin adapter from `MatrixMessageOperationResult` at `src/mindroom/custom_tools/matrix_conversation_operations.py:49` to the tool JSON payload.
No independent duplication was found for that method.

`MatrixMessageTools._message_context` is related to `resolve_context_thread_id` at `src/mindroom/custom_tools/attachment_helpers.py:62` and runtime context construction at `src/mindroom/tool_system/runtime_context.py:67`, but it has a tool-specific response shape.
No refactor is recommended unless more tools begin exposing the same model-facing `context` action.

`MatrixMessageTools._action_supports_attachments` has no meaningful duplicate.
The underlying send/reply attachment behavior is centralized below the adapter in `src/mindroom/custom_tools/matrix_conversation_operations.py:140` and attachment-specific helpers.

Proposed generalization:

1. Add a small helper in `src/mindroom/custom_tools/tool_payloads.py`, or extend an existing custom-tools helper module, with `tool_payload(tool_name, status, **fields)` and optionally `tool_context_error(tool_name, message, **fields)`.
2. Add a tiny bounded integer helper such as `bounded_limit(limit, default, maximum)` in a shared custom-tools helper module.
3. Consider a narrow Matrix room preflight helper only if another Matrix tool is added or if existing tools are already being touched; it should return `(context, resolved_room_id, error_payload)` and accept flags for context actions and rate-limit weights.
4. If `matrix_api.py` is touched, replace its inline sliding-window rate-limit algorithm with `matrix_helpers.check_rate_limit` while preserving write weights and the existing error text.

Risk/tests:

Payload helper extraction has low behavior risk but must preserve sorted JSON keys, tool names, and existing error fields exactly.
Limit helper extraction has low risk if tests cover `None`, below-minimum, normal, and above-maximum values for `matrix_message` and `matrix_room`.
Preflight extraction has moderate risk because each tool has different validation order and error payload fields; test unsupported actions, unauthorized rooms, `context` action authorization bypass, attachment limit errors, and rate-limit errors before and after any refactor.
No production code was edited for this audit.
