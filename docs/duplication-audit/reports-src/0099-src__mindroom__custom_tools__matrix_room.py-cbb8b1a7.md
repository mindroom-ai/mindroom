## Summary

Top duplication candidate: `MatrixRoomTools._thread_reply_count` and `MatrixRoomTools._threads` duplicate the thread-root serialization behavior already used by `MatrixMessageOperations._room_threads`.
Related but lower-payoff overlap exists in JSON tool payload/context-error helpers across custom tools, optional string/room request normalization, and Matrix state/member read wrappers.
No production refactor was applied.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_MatrixRoomRequest	class	lines 27-33	related-only	dataclass request action room_id limit event_type state_key page_token	src/mindroom/custom_tools/matrix_api.py:472; src/mindroom/custom_tools/thread_tags.py:80
MatrixRoomTools	class	lines 36-521	related-only	Matrix custom tool Toolkit room-info members threads state	src/mindroom/custom_tools/matrix_message.py:18; src/mindroom/custom_tools/matrix_api.py:92; src/mindroom/custom_tools/thread_tags.py:64
MatrixRoomTools.__init__	method	lines 50-54	related-only	Toolkit name tools init matrix_room	src/mindroom/custom_tools/matrix_message.py:36; src/mindroom/custom_tools/thread_tags.py:67; src/mindroom/custom_tools/thread_summary.py:22
MatrixRoomTools._payload	method	lines 57-60	duplicate-found	payload status tool json.dumps sort_keys custom_tools	src/mindroom/custom_tools/matrix_message.py:43; src/mindroom/custom_tools/matrix_api.py:144; src/mindroom/custom_tools/thread_tags.py:74; src/mindroom/custom_tools/thread_summary.py:29; src/mindroom/custom_tools/attachments.py:60; src/mindroom/custom_tools/subagents.py:43
MatrixRoomTools._context_error	method	lines 63-67	duplicate-found	context unavailable runtime path payload error	src/mindroom/custom_tools/matrix_message.py:56; src/mindroom/custom_tools/matrix_api.py:150; src/mindroom/custom_tools/thread_tags.py:80; src/mindroom/custom_tools/thread_summary.py:35
MatrixRoomTools._thread_limit	method	lines 70-74	related-only	clamp limit default max int read limit	src/mindroom/custom_tools/matrix_message.py:63; src/mindroom/custom_tools/matrix_api.py:334
MatrixRoomTools._thread_reply_count	method	lines 77-91	duplicate-found	unsigned m.relations m.thread count bool rejected	src/mindroom/custom_tools/matrix_conversation_operations.py:447; src/mindroom/matrix/client_thread_history.py:224; src/mindroom/matrix/client_visible_messages.py:230
MatrixRoomTools._check_rate_limit	method	lines 94-107	none-found	matrix_room check_rate_limit wrapper recent_actions 30 20	src/mindroom/custom_tools/matrix_helpers.py:15; src/mindroom/custom_tools/matrix_message.py:113
MatrixRoomTools._transport_error_message	method	lines 110-113	related-only	ClientError TimeoutError transport failure type detail message	src/mindroom/matrix/client_thread_history.py:144; src/mindroom/custom_tools/matrix_api.py:178
MatrixRoomTools._input_error	method	lines 116-117	related-only	input validation error action payload helper	src/mindroom/custom_tools/matrix_api.py:157; src/mindroom/custom_tools/thread_tags.py:99; src/mindroom/custom_tools/thread_summary.py:57
MatrixRoomTools._normalize_optional_str_fields	method	lines 120-147	related-only	optional string fields strip validation room_id event_type state_key page_token	src/mindroom/custom_tools/attachment_helpers.py:47; src/mindroom/custom_tools/matrix_api.py:355
MatrixRoomTools._normalize_request	method	lines 150-182	related-only	action strip lower limit int bool rejected request normalization	src/mindroom/custom_tools/matrix_api.py:208; src/mindroom/custom_tools/matrix_api.py:334; src/mindroom/custom_tools/matrix_message.py:166
MatrixRoomTools._dispatch_action	async_method	lines 184-207	related-only	action dispatch room-info members threads state	src/mindroom/custom_tools/matrix_message.py:166; src/mindroom/custom_tools/matrix_api.py:1397
MatrixRoomTools._room_info	async_method	lines 209-260	related-only	cached MatrixRoom room metadata power_levels creator room_get_state_event	src/mindroom/matrix/client_room_admin.py:95; src/mindroom/matrix/client_room_admin.py:423
MatrixRoomTools._members	async_method	lines 262-305	related-only	joined_members response member display avatar power level	src/mindroom/matrix/client_room_admin.py:405; src/mindroom/authorization.py:275
MatrixRoomTools._threads	async_method	lines 307-368	duplicate-found	get_room_threads_page thread roots preview reply_count next_token has_more	src/mindroom/custom_tools/matrix_conversation_operations.py:524; src/mindroom/custom_tools/matrix_conversation_operations.py:480; src/mindroom/matrix/client_thread_history.py:117
MatrixRoomTools._state	async_method	lines 370-456	related-only	room_get_state_event room_get_state state_summary content_preview m.room.member	src/mindroom/custom_tools/matrix_api.py:888; src/mindroom/matrix/client_room_admin.py:429; src/mindroom/matrix/rooms.py:688
MatrixRoomTools.matrix_room	async_method	lines 458-521	related-only	tool entry context normalize action room access rate limit dispatch	src/mindroom/custom_tools/matrix_message.py:166; src/mindroom/custom_tools/matrix_api.py:1397; src/mindroom/custom_tools/thread_tags.py:86
```

## Findings

### 1. Thread root serialization is duplicated with `matrix_message`

- Primary: `src/mindroom/custom_tools/matrix_room.py:77`, `src/mindroom/custom_tools/matrix_room.py:307`
- Duplicate behavior: `src/mindroom/custom_tools/matrix_conversation_operations.py:447`, `src/mindroom/custom_tools/matrix_conversation_operations.py:480`, `src/mindroom/custom_tools/matrix_conversation_operations.py:524`

Both paths call `get_room_threads_page`, convert each root into `thread_id`, `sender`, `timestamp`, `body_preview`, and `reply_count`, and return `count`, `threads`, `next_token`, and `has_more`.
Both use `trusted_visible_sender_ids` and `thread_root_body_preview` for the preview.
The differences to preserve are action names (`threads` vs `room-threads`), the `MatrixMessageOperationResult` wrapper, and the extra `latest_activity_ts` field emitted only by `matrix_message`.

### 2. Thread reply count extraction is repeated

- Primary: `src/mindroom/custom_tools/matrix_room.py:77`
- Duplicate behavior: `src/mindroom/custom_tools/matrix_conversation_operations.py:447`
- Related helpers: `src/mindroom/matrix/client_thread_history.py:224`, `src/mindroom/matrix/client_visible_messages.py:230`

Both concrete implementations walk `event.source["unsigned"]["m.relations"]["m.thread"]["count"]` and return the count only when it is an `int` but not `bool`.
`matrix_room` first guards `event.source` because it is typed as possibly non-dict; `matrix_conversation_operations` assumes a dict source.
The related helpers already parse adjacent Matrix relation metadata, so this belongs near Matrix thread/event projection rather than in either tool.

### 3. Structured custom-tool payload helpers are repeated

- Primary: `src/mindroom/custom_tools/matrix_room.py:57`, `src/mindroom/custom_tools/matrix_room.py:63`
- Similar helpers: `src/mindroom/custom_tools/matrix_message.py:43`, `src/mindroom/custom_tools/matrix_api.py:144`, `src/mindroom/custom_tools/thread_tags.py:74`, `src/mindroom/custom_tools/thread_summary.py:29`, `src/mindroom/custom_tools/attachments.py:60`, `src/mindroom/custom_tools/subagents.py:43`

These helpers all build a deterministic JSON payload with `status`, `tool`, and caller-supplied fields.
The duplication is real but tiny.
Context-error helpers differ only in tool-specific text and optional action fields.

## Proposed Generalization

1. Extract a small Matrix thread-root serializer, likely in `src/mindroom/matrix/client_visible_messages.py` or a focused `src/mindroom/matrix/thread_root_payload.py`, that returns the common `thread_id`, `sender`, `timestamp`, `body_preview`, and `reply_count` payload.
2. Move reply-count extraction into the same focused Matrix helper and have both `matrix_room` and `matrix_conversation_operations` call it.
3. Keep `matrix_room` and `matrix_message` responsible for their public action names and result wrappers.
4. Leave custom-tool JSON payload helpers local unless a broader custom-tool response abstraction is introduced for other reasons.
5. Leave room state/member reads local; current overlap is API-adjacent but not a clear shared behavior with matching output contracts.

## Risk/tests

Thread serialization dedupe risk is mainly output drift: field names, `latest_activity_ts`, malformed event handling, and pagination error payloads must stay unchanged.
Focused tests should cover `matrix_room` `threads`, `matrix_message` `room-threads`, malformed thread root sources, bundled preview hydration, and `bool` rejection for reply counts.
Payload-helper dedupe would create broad mechanical churn and would need custom-tool snapshot coverage, so no refactor is recommended from this audit alone.
