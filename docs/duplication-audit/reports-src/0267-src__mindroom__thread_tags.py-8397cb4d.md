## Summary

Top duplication candidates:

1. `thread_tags.py` has its own ISO datetime parser that overlaps with approval-card datetime parsing in `src/mindroom/approval_events.py`.
2. Thread-tag JSON-compatible payload validation overlaps with Matrix state content validation and tool-call JSON sanitization, but the behavior is stricter and domain-specific.
3. Matrix room-state and member-state wrappers overlap operationally with Matrix room/API tools, but thread tags add tag-specific parsing, merge semantics, verification retries, and authorization checks.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ThreadTagsError	class	lines 39-40	not-a-behavior-symbol	"ThreadTagsError RuntimeError domain error"	none
ThreadTagRecord	class	lines 43-88	related-only	"BaseModel set_by set_at note data model_dump exclude_none"	src/mindroom/custom_tools/thread_tags.py:32; src/mindroom/thread_tags.py:439
ThreadTagRecord._validate_set_by	method	lines 55-60	related-only	"strip non-empty string validation set_by"	src/mindroom/approval_events.py:118; src/mindroom/custom_tools/matrix_api.py:239; src/mindroom/thread_tags.py:134
ThreadTagRecord._normalize_set_at	method	lines 64-67	duplicate-found	"datetime tzinfo replace UTC fromisoformat"	src/mindroom/approval_events.py:147
ThreadTagRecord._normalize_note	method	lines 71-81	related-only	"optional string strip empty none note"	src/mindroom/custom_tools/matrix_api.py:355; src/mindroom/approval_inbound.py:55
ThreadTagRecord._normalize_data	method	lines 85-88	related-only	"data object mapping JSON-compatible validation"	src/mindroom/custom_tools/matrix_api.py:262; src/mindroom/custom_tools/matrix_api.py:287
ThreadTagsState	class	lines 91-98	not-a-behavior-symbol	"ThreadTagsState room_id thread_root_id tags model"	none
_parse_timestamp	function	lines 101-113	duplicate-found	"fromisoformat tzinfo UTC parse datetime"	src/mindroom/approval_events.py:147
_parse_power_level	function	lines 116-120	related-only	"int not bool Matrix power level"	src/mindroom/custom_tools/matrix_room.py:224; src/mindroom/custom_tools/matrix_room.py:295
_normalize_non_empty_string	function	lines 123-131	duplicate-found	"normalize non-empty string strip helper"	src/mindroom/custom_tools/matrix_api.py:239; src/mindroom/approval_events.py:126; src/mindroom/mcp/config.py:18
_require_non_empty_string	function	lines 134-140	related-only	"require non-empty string raise domain error"	src/mindroom/custom_tools/matrix_api.py:239; src/mindroom/approval_events.py:118
normalize_tag_name	function	lines 143-154	none-found	"thread tag regex lowercase normalize_tag_name tag must be 1-50"	none
_normalize_blocked_by	function	lines 157-170	related-only	"list of non-empty strings validate blocked_by"	src/mindroom/custom_tools/matrix_api.py:299; src/mindroom/mcp/toolkit.py:35
_normalize_object_mapping	function	lines 173-189	duplicate-found	"string-keyed object mapping validate JSON compatible"	src/mindroom/custom_tools/matrix_api.py:287; src/mindroom/custom_tools/matrix_api.py:262
_normalize_json_compatible_value	function	lines 192-211	related-only	"recursive JSON-compatible value finite float"	src/mindroom/tool_system/tool_calls.py:303; src/mindroom/custom_tools/matrix_api.py:262
_normalize_blocked_tag_data	function	lines 214-218	none-found	"blocked tag data blocked_by normalize"	none
_normalize_waiting_tag_data	function	lines 221-230	none-found	"waiting tag data waiting_on normalize"	none
_normalize_priority_tag_data	function	lines 233-246	none-found	"priority tag data level high medium low normalize"	none
_normalize_due_tag_data	function	lines 249-258	related-only	"due tag deadline ISO timestamp normalize"	src/mindroom/approval_events.py:147
_normalize_tag_data	function	lines 269-283	none-found	"predefined thread tag data normalizers blocked waiting priority due"	none
_parse_thread_tag_record	function	lines 286-302	none-found	"parse ThreadTagRecord model_validate drop malformed tag payload"	none
_parse_thread_tags_state	function	lines 305-331	none-found	"parse thread tags state content tags mapping"	none
_thread_tag_state_key	function	lines 334-336	none-found	"json dumps array state key thread_root_id tag separators"	none
_parse_thread_tag_state_key	function	lines 339-360	none-found	"json loads state key array thread_root_id tag"	none
_thread_tags_state_from_tags	function	lines 363-375	related-only	"build state from tags sorted non-empty"	src/mindroom/custom_tools/thread_tags.py:32
_collect_thread_tag_state_entry	function	lines 378-409	none-found	"legacy tags per-tag tombstones state entry parse merge inputs"	none
_merge_thread_tag_room_state	function	lines 412-436	none-found	"merge legacy per-tag tombstones thread tag room state"	none
_thread_tag_record_content	function	lines 439-441	duplicate-found	"ThreadTagRecord model_dump mode json exclude_none"	src/mindroom/custom_tools/thread_tags.py:32
_thread_tag_records_match	function	lines 444-451	related-only	"compare serialized expected actual tag record"	src/mindroom/approval_events.py:111
_verified_state_contains_expected_tag	function	lines 454-463	none-found	"verification state contains expected exact tag record"	none
_verified_remove_state_matches	function	lines 466-474	none-found	"verification state removed tag absent"	none
_empty_thread_tags_state	function	lines 477-483	none-found	"empty ThreadTagsState room thread tags empty"	none
_put_thread_tag_state	async_function	lines 486-506	related-only	"room_put_state RoomPutStateResponse state_key content Matrix state write"	src/mindroom/custom_tools/matrix_api.py:970; src/mindroom/matrix/avatar.py:170; src/mindroom/topic_generator.py:166
_required_state_event_power_level	function	lines 509-525	related-only	"power levels events state_default event type required"	src/mindroom/custom_tools/matrix_room.py:224
_user_power_level	function	lines 528-544	related-only	"power levels users users_default user level"	src/mindroom/custom_tools/matrix_room.py:295
_raise_insufficient_power_level	function	lines 547-560	none-found	"insufficient Matrix power level error message thread tags"	none
_assert_requester_joined_room	async_function	lines 563-580	related-only	"joined_members JoinedMembersResponse member user_id verify joined"	src/mindroom/custom_tools/matrix_room.py:262; src/mindroom/authorization.py:276
_assert_user_can_write_thread_tags	function	lines 583-607	none-found	"assert user can write thread tags power level"	none
_get_room_thread_tags_states	async_function	lines 610-640	related-only	"room_get_state RoomGetStateResponse iterate state events filter type"	src/mindroom/custom_tools/matrix_room.py:412; src/mindroom/custom_tools/matrix_api.py:933
list_tagged_threads_from_state_map	function	lines 643-674	none-found	"prefetched state map list tagged threads filter tag"	none
_assert_thread_tags_write_allowed	async_function	lines 677-723	related-only	"room_get_state_event power levels requester joined write authorization"	src/mindroom/custom_tools/matrix_room.py:237; src/mindroom/custom_tools/matrix_api.py:933
get_thread_tags	async_function	lines 726-740	none-found	"get thread tags normalized thread root from room states"	none
set_thread_tag	async_function	lines 743-812	none-found	"set thread tag verified write retry expected record"	none
remove_thread_tag	async_function	lines 815-881	duplicate-found	"remove thread tag retry verify tombstone no state not set"	src/mindroom/thread_tags.py:903
_get_thread_tags_via_room_state	async_function	lines 884-900	related-only	"abstract query_room_state thread tag state map"	src/mindroom/thread_tags.py:610; src/mindroom/custom_tools/matrix_room.py:370
remove_thread_tag_via_room_state	async_function	lines 903-971	duplicate-found	"remove thread tag retry verify tombstone no state not set"	src/mindroom/thread_tags.py:815
list_tagged_threads	async_function	lines 974-987	related-only	"list tagged threads optional tag filter"	src/mindroom/thread_tags.py:643; src/mindroom/custom_tools/thread_tags.py:335
```

## Findings

### 1. ISO datetime parsing is repeated

`src/mindroom/thread_tags.py:101` parses an ISO-8601 string with `datetime.fromisoformat`, treats malformed input as absent, and assigns UTC to naive datetimes.
`src/mindroom/approval_events.py:147` implements the same timezone normalization for approval timestamps, although it expects `str | None` and lets invalid ISO values raise.
`ThreadTagRecord._normalize_set_at` at `src/mindroom/thread_tags.py:64` repeats the same "naive means UTC" normalization for already-parsed datetimes.

Differences to preserve:
`thread_tags._parse_timestamp` returns `None` for non-string, empty, or invalid values.
`approval_events._parse_datetime` accepts only `str | None` and currently raises on invalid strings.

### 2. JSON/object payload validation is partially duplicated

`src/mindroom/thread_tags.py:173` and `src/mindroom/thread_tags.py:192` recursively validate a string-keyed JSON-like object and reject non-finite floats.
`src/mindroom/custom_tools/matrix_api.py:262` validates Matrix state content by checking `json.dumps(content, sort_keys=True)`, and `src/mindroom/custom_tools/matrix_api.py:287` separately copies only string-keyed dicts.
`src/mindroom/tool_system/tool_calls.py:303` recursively walks arbitrary values and explicitly handles non-finite floats, but it sanitizes instead of rejecting.

Differences to preserve:
Thread tags must reject invalid nested values and must keep only JSON-compatible primitives.
Matrix API state writes currently allow any dict that `json.dumps` accepts.
Tool-call persistence sanitizes and bounds values rather than validating user input.

### 3. Thread-tag record serialization is duplicated between core and tool output

`src/mindroom/thread_tags.py:439` serializes a single `ThreadTagRecord` with `model_dump(mode="json", exclude_none=True)`.
`src/mindroom/custom_tools/thread_tags.py:32` serializes every tag record with the same `model_dump(mode="json", exclude_none=True)` call for tool payloads.

Differences to preserve:
The core helper returns `dict[str, object]` for one record and is used for write verification.
The tool helper returns a mapping keyed by tag name for user-facing JSON payloads.

### 4. Remove-tag retry loops are near-duplicates

`src/mindroom/thread_tags.py:815` removes a tag via a `nio.AsyncClient`, checks write permissions, writes a per-tag tombstone with `_put_thread_tag_state`, then verifies with `get_thread_tags`.
`src/mindroom/thread_tags.py:903` performs the same existence checks, tombstone write, verification, empty-state returns, and retry failure message through abstract `query_room_state` and `put_room_state` callbacks.

Differences to preserve:
The client-backed path must run `_assert_thread_tags_write_allowed`.
The callback-backed path supports `expected_record` for stale wake protection and returns the existing state without writing if the record no longer matches.
The callback-backed path receives a boolean write result instead of a `nio.RoomPutStateResponse`.

### 5. Matrix state and membership operations are related but not clean duplicates

`src/mindroom/thread_tags.py:563`, `src/mindroom/thread_tags.py:610`, and `src/mindroom/thread_tags.py:677` wrap `joined_members`, `room_get_state`, and `room_get_state_event` with domain-specific `ThreadTagsError` messages and thread-tag parsing.
`src/mindroom/custom_tools/matrix_room.py:262`, `src/mindroom/custom_tools/matrix_room.py:370`, and `src/mindroom/custom_tools/matrix_room.py:412` expose the same Matrix API surfaces for general room inspection.
`src/mindroom/custom_tools/matrix_api.py:933` and `src/mindroom/custom_tools/matrix_api.py:970` expose generic state get/put behavior.

This is related infrastructure reuse potential rather than duplicated thread-tag behavior.
Thread tags have stricter authorization and merge semantics that should not move into generic Matrix tools.

## Proposed Generalization

1. Extract a tiny datetime helper such as `normalize_aware_utc_datetime(value: datetime) -> datetime` and, if useful, `parse_iso_datetime_or_none(value: object) -> datetime | None` into a small shared module used by `thread_tags.py` and `approval_events.py`.
2. Expose a public `thread_tag_record_content(record: ThreadTagRecord) -> dict[str, object]` or reuse `_thread_tag_record_content` from `custom_tools/thread_tags.py` if private-helper imports are acceptable after renaming.
3. Consider a local private helper inside `thread_tags.py` for the common remove-tag retry body, parameterized by load/write callbacks and optional `expected_record`.
4. Do not generalize Matrix state access yet; current overlaps are API-shape related and domain behavior differs.
5. Do not generalize tag-specific schema normalizers; no active duplicates were found.

## Risk/tests

Datetime extraction risk is low but needs tests for naive datetimes, aware datetimes, invalid strings, and `None` across thread tags and approval events.
Thread-tag serialization extraction risk is low and should be covered by existing thread-tag tool payload and write-verification tests.
Remove-loop deduplication is higher risk because it controls concurrent write verification, tombstones, callback write failures, and stale expected-record behavior.
Tests should cover successful remove, missing state before any write, missing tag before any write, verified empty state after tombstone, retry exhaustion, and expected-record mismatch for the callback-backed path.
