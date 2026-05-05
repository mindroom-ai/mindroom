## Summary

Top duplication candidate: `MatrixApiTools._check_rate_limit` duplicates the sliding-window per-agent/requester/room rate-limit implementation already centralized in `src/mindroom/custom_tools/matrix_helpers.py`.
Related but lower-confidence overlap exists in structured JSON tool payload helpers across custom tools, Matrix state/event wrappers shared with `matrix_room` and Matrix client helpers, and search event normalization with cache/message-preview helpers.
No production refactor was applied.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MatrixSearchError	class	lines 28-29	related-only	nio ErrorResponse custom response search errors	src/mindroom/custom_tools/matrix_api.py:28; src/mindroom/custom_tools/matrix_room.py:18
MatrixSearchResponse	class	lines 33-89	none-found	Matrix search response parser search_categories room_events from_dict	none
MatrixSearchResponse._malformed_response_error	method	lines 41-42	none-found	malformed Matrix response ErrorResponse helpers	none
MatrixSearchResponse._matrix_error_from_dict	method	lines 45-52	related-only	ErrorResponse.from_dict wrapped custom error	src/mindroom/custom_tools/matrix_api.py:45; src/mindroom/matrix/client_thread_history.py:18
MatrixSearchResponse.from_dict	method	lines 55-89	none-found	search_categories room_events count next_batch results parser	none
MatrixApiTools	class	lines 92-1539	related-only	Matrix custom tool Toolkit payload context room operations	src/mindroom/custom_tools/matrix_message.py:17; src/mindroom/custom_tools/matrix_room.py:28
MatrixApiTools.__init__	method	lines 137-141	related-only	Toolkit name tools init matrix_api	matrix_message.py:34; matrix_room.py:50
MatrixApiTools._payload	method	lines 144-147	duplicate-found	payload status tool json.dumps sort_keys custom_tools	src/mindroom/custom_tools/dynamic_tools.py:45; src/mindroom/custom_tools/matrix_room.py:57; src/mindroom/custom_tools/matrix_message.py:43; src/mindroom/custom_tools/thread_tags.py:74; src/mindroom/custom_tools/thread_summary.py:29; src/mindroom/custom_tools/subagents.py:43
MatrixApiTools._context_error	method	lines 150-154	duplicate-found	context unavailable runtime path payload error	src/mindroom/custom_tools/matrix_room.py:63; src/mindroom/custom_tools/matrix_message.py:50; src/mindroom/custom_tools/thread_tags.py:80; src/mindroom/custom_tools/thread_summary.py:35; src/mindroom/custom_tools/subagents.py:52
MatrixApiTools._error_payload	method	lines 157-175	related-only	structured error payload normalized response status_code	src/mindroom/custom_tools/matrix_room.py:112; src/mindroom/custom_tools/matrix_message.py:47
MatrixApiTools._normalize_response	method	lines 178-195	related-only	normalize nio ErrorResponse exception response status_code	src/mindroom/custom_tools/matrix_room.py:95; src/mindroom/matrix/client_room_admin.py:95
MatrixApiTools._normalize_matrix_error	method	lines 198-201	related-only	nio ErrorResponse status_code string normalization	src/mindroom/matrix/client_thread_history.py:31; src/mindroom/custom_tools/matrix_room.py:95
MatrixApiTools._supported_actions_message	method	lines 204-205	related-only	Unsupported action Use list message	src/mindroom/custom_tools/matrix_message.py:74; src/mindroom/custom_tools/matrix_room.py:171
MatrixApiTools._normalize_action	method	lines 208-209	related-only	action strip lower validation	src/mindroom/custom_tools/matrix_room.py:158; src/mindroom/custom_tools/matrix_message.py:519
MatrixApiTools._resolve_room_id	method	lines 212-226	related-only	resolve room_id current context non-empty string	src/mindroom/custom_tools/attachment_helpers.py:48; src/mindroom/custom_tools/thread_summary.py:56
MatrixApiTools._validate_bool	method	lines 229-236	related-only	bool validation rejects non-bool	src/mindroom/custom_tools/google_drive.py:71; src/mindroom/custom_tools/matrix_room.py:172
MatrixApiTools._validate_non_empty_string	method	lines 239-249	related-only	non-empty string validation strip message	src/mindroom/custom_tools/subagents.py:69; src/mindroom/custom_tools/thread_summary.py:78; src/mindroom/custom_tools/attachments.py:436
MatrixApiTools._resolve_state_key	method	lines 252-259	related-only	state_key string default empty Matrix state	src/mindroom/custom_tools/matrix_room.py:127; src/mindroom/custom_tools/matrix_room.py:380
MatrixApiTools._validate_content	method	lines 262-271	none-found	content JSON object JSON-serializable validation	none
MatrixApiTools._content_summary	method	lines 274-284	none-found	content_keys content_bytes audit summary	none
MatrixApiTools._copy_string_keyed_dict	method	lines 287-296	related-only	copy dict only string keys Mapping normalization	src/mindroom/matrix/client_visible_messages.py:205; src/mindroom/matrix/cache/event_normalization.py:17
MatrixApiTools._validate_string_list	method	lines 299-313	related-only	list non-empty strings strip validation	src/mindroom/custom_tools/attachment_helpers.py:11
MatrixApiTools._validate_search_order_by	method	lines 316-321	none-found	Matrix search order_by rank recent validation	none
MatrixApiTools._validate_search_keys	method	lines 324-331	none-found	Matrix search keys allowed content.body content.name content.topic	none
MatrixApiTools._validate_search_limit	method	lines 334-338	related-only	integer cap bool rejected limit validation	src/mindroom/custom_tools/matrix_room.py:66; src/mindroom/custom_tools/matrix_message.py:57
MatrixApiTools._validate_optional_dict	method	lines 341-352	related-only	optional dict string-keyed object validation	src/mindroom/matrix/client_visible_messages.py:205; src/mindroom/matrix/cache/event_normalization.py:17
MatrixApiTools._validate_optional_string	method	lines 355-365	related-only	optional non-empty string strip validation	src/mindroom/custom_tools/matrix_room.py:127; src/mindroom/custom_tools/attachment_helpers.py:48
MatrixApiTools._truncate_snippet	method	lines 368-374	duplicate-found	compact whitespace truncate ellipsis preview	src/mindroom/matrix/client_visible_messages.py:222; src/mindroom/custom_tools/config_manager.py:489
MatrixApiTools._search_snippet_text	method	lines 377-382	none-found	body name topic snippet selection	none
MatrixApiTools._normalize_search_event_payload	method	lines 385-404	related-only	Matrix event payload event_id room_id sender timestamp type snippet	src/mindroom/matrix/cache/event_normalization.py:17; src/mindroom/matrix/client_visible_messages.py:205
MatrixApiTools._normalize_search_context_payload	method	lines 407-440	none-found	Matrix search context events_before events_after profile_info	none
MatrixApiTools._build_search_filter	method	lines 443-469	none-found	Matrix search filter rooms limit enforcement	none
MatrixApiTools._build_search_request_body	method	lines 472-504	none-found	Matrix search request body search_categories room_events	none
MatrixApiTools._build_search_path	method	lines 507-511	none-found	nio Api build_path search next_batch	none
MatrixApiTools._check_rate_limit	method	lines 514-548	duplicate-found	sliding window rate limit deque lock stale keys	src/mindroom/custom_tools/matrix_helpers.py:11; src/mindroom/custom_tools/matrix_room.py:82; src/mindroom/custom_tools/matrix_message.py:28
MatrixApiTools._audit_write	method	lines 551-606	none-found	matrix_api_write_audit content summary normalized response	none
MatrixApiTools._state_write_policy_error	method	lines 609-645	none-found	Matrix dangerous state policy hard blocked state types	none
MatrixApiTools._send_event_policy_error	method	lines 648-679	none-found	Matrix send_event dangerous state redaction policy	none
MatrixApiTools._record_send_event_outbound_cache_write	async_method	lines 682-698	related-only	notify outbound message conversation cache after room_send	src/mindroom/custom_tools/matrix_conversation_operations.py:349; src/mindroom/matrix/cache/thread_writes.py:506
MatrixApiTools._resolve_redaction_cache_write_requirement	async_method	lines 701-733	related-only	resolve redaction thread impact fail closed cache write	src/mindroom/matrix/cache/thread_writes.py:188; src/mindroom/matrix/cache/thread_writes.py:484
MatrixApiTools._send_event	async_method	lines 735-886	related-only	room_send response payload audit cache write	src/mindroom/custom_tools/matrix_conversation_operations.py:349; src/mindroom/matrix/client_delivery.py:80
MatrixApiTools._get_state	async_method	lines 888-968	related-only	room_get_state_event found false content payload	src/mindroom/custom_tools/matrix_room.py:380; src/mindroom/matrix/client_room_admin.py:95
MatrixApiTools._put_state	async_method	lines 970-1113	related-only	room_put_state policy audit payload	src/mindroom/matrix/client_room_admin.py:122; src/mindroom/matrix/avatar.py:164
MatrixApiTools._redact	async_method	lines 1115-1244	related-only	room_redact notify outbound redaction audit payload	src/mindroom/bot.py:1760; src/mindroom/stop.py:242; src/mindroom/matrix/stale_stream_cleanup.py:1223
MatrixApiTools._get_event	async_method	lines 1246-1315	related-only	room_get_event found false event source payload	src/mindroom/matrix/conversation_cache.py:335; src/mindroom/matrix/thread_membership.py:530
MatrixApiTools._search	async_method	lines 1317-1395	none-found	Matrix search client _send MatrixSearchResponse normalize results	none
MatrixApiTools.matrix_api	async_method	lines 1397-1539	related-only	custom tool dispatch context action room access validation	src/mindroom/custom_tools/matrix_message.py:503; src/mindroom/custom_tools/matrix_room.py:461; src/mindroom/custom_tools/thread_tags.py:90
```

## Findings

### 1. Sliding-window rate limiting is duplicated

- Primary: `src/mindroom/custom_tools/matrix_api.py:514`
- Existing shared helper: `src/mindroom/custom_tools/matrix_helpers.py:11`
- Current users of shared helper: `src/mindroom/custom_tools/matrix_room.py:82`, `src/mindroom/custom_tools/matrix_message.py:28`

`MatrixApiTools._check_rate_limit` repeats the same per-`(agent_name, requester_id, room_id)` deque pruning, weighted insertion, stale-key cleanup, and lock discipline as `matrix_helpers.check_rate_limit`.
The meaningful differences are wording (`matrix_api writes` / `units`) and that `matrix_api` looks up action-specific weights internally before calling the algorithm.

### 2. Structured JSON tool payload helpers are repeated across custom tools

- Primary: `src/mindroom/custom_tools/matrix_api.py:144`, `src/mindroom/custom_tools/matrix_api.py:150`
- Similar helpers: `src/mindroom/custom_tools/dynamic_tools.py:45`, `src/mindroom/custom_tools/matrix_room.py:57`, `src/mindroom/custom_tools/matrix_message.py:43`, `src/mindroom/custom_tools/thread_tags.py:74`, `src/mindroom/custom_tools/thread_summary.py:29`, `src/mindroom/custom_tools/subagents.py:43`

These helpers all construct `{"status": status, "tool": <tool_name>, ...}` and return deterministic `json.dumps(..., sort_keys=True)`.
The duplication is real, but each tool’s method is tiny and locally readable.
Some tools also add action-specific context errors, so a broad refactor would touch many files for limited payoff.

### 3. Search snippet truncation duplicates existing preview behavior

- Primary: `src/mindroom/custom_tools/matrix_api.py:368`
- Similar helper: `src/mindroom/matrix/client_visible_messages.py:222`

Both helpers collapse internal whitespace and truncate with a trailing ellipsis.
The behavior differs only by default maximum length (`200` for Matrix API search snippets, `120` for visible message previews).
This is a small, safe dedupe candidate if another Matrix output surface starts needing the same compact preview behavior.

## Proposed Generalization

1. Refactor only `MatrixApiTools._check_rate_limit` to delegate to `mindroom.custom_tools.matrix_helpers.check_rate_limit`, passing `weight=cls._WRITE_ACTION_WEIGHTS[action]`.
2. If exact wording matters, extend `check_rate_limit` with an optional `unit_label: str = "actions"` parameter and keep current callers unchanged.
3. Leave JSON payload helpers local for now; a shared helper would be mechanically easy but would create wide churn.
4. Leave search response parsing and normalization local to `matrix_api`; no other source module currently implements Matrix `/search`.
5. Consider replacing `_truncate_snippet` with `message_preview(text, max_length=cls._SEARCH_SNIPPET_MAX_CHARS)` only if `matrix_api` already depends on visible-message helpers for another reason.

## Risk/tests

Rate-limit dedupe risk is limited to error-message text and write-weight accounting.
Tests should cover `send_event`, `put_state`, and `redact` rate-limit thresholds, including the weight-2 actions.
Payload-helper refactors would need broad custom-tool snapshot tests and are not recommended from this audit alone.
Search snippet dedupe would need tests for whitespace compaction, exact 200-character truncation, and non-string values.
