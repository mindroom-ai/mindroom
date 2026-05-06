## Summary

`src/mindroom/matrix/event_info.py` is already the central relation-analysis module and most active callers use `EventInfo.from_event`.
The only meaningful duplication found is a narrower reply-target parser in `src/mindroom/matrix/client_visible_messages.py` that repeats part of `_analyze_event_relations`.
Timestamp checks are related but intentionally differ between optional raw-event reads and required cache validation.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
origin_server_ts_from_event_source	function	lines 14-21	related-only	origin_server_ts timestamp bool int float event.source	src/mindroom/bot.py:1346; src/mindroom/matrix/cache/sqlite_event_cache_events.py:44; src/mindroom/approval_events.py:131; src/mindroom/matrix/conversation_cache.py:271; src/mindroom/matrix/cache/event_normalization.py:15
EventInfo	class	lines 25-94	related-only	EventInfo relation dataclass thread edit reply reaction	src/mindroom/matrix/thread_membership.py:121; src/mindroom/conversation_resolver.py:195; src/mindroom/turn_controller.py:1493; src/mindroom/matrix/cache/thread_writes.py:327; src/mindroom/approval_inbound.py:45
EventInfo.from_event	method	lines 76-78	related-only	EventInfo.from_event analyze event relations callers	src/mindroom/turn_controller.py:1493; src/mindroom/conversation_resolver.py:240; src/mindroom/approval_events.py:155; src/mindroom/matrix/cache/sqlite_event_cache_events.py:9; tests/test_threading_error.py:4042
EventInfo.next_related_event_id	method	lines 80-94	related-only	next_related_event_id related target original reaction reference reply	src/mindroom/matrix/thread_membership.py:121; src/mindroom/matrix/thread_membership.py:176; src/mindroom/matrix/thread_membership.py:225; tests/test_threading_error.py:4183
_analyze_event_relations	function	lines 97-195	duplicate-found	m.relates_to rel_type m.thread m.replace m.annotation m.in_reply_to event_id	src/mindroom/matrix/client_visible_messages.py:335; src/mindroom/approval_inbound.py:45; src/mindroom/thread_summary.py:240; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:48
_extract_thread_id_from_new_content	function	lines 198-212	related-only	m.new_content m.relates_to rel_type m.thread event_id	src/mindroom/matrix/message_content.py:271; src/mindroom/conversation_resolver.py:52; src/mindroom/matrix/cache/thread_writes.py:100; tests/test_threading_error.py:2338
```

## Findings

### 1. Narrow duplicate reply-target extraction

`_analyze_event_relations` extracts `content["m.relates_to"]["m.in_reply_to"]["event_id"]` into `EventInfo.reply_to_event_id` at `src/mindroom/matrix/event_info.py:161`.
`_reply_to_event_id_from_content` repeats the same nested Matrix relation traversal at `src/mindroom/matrix/client_visible_messages.py:335`.

The behavior is functionally the same for normal visible content dictionaries: validate `m.relates_to` as a mapping, validate `m.in_reply_to` as a mapping, and return the nested string `event_id`.
The difference to preserve is input shape.
`EventInfo.from_event` expects an event source containing `content`, while `_reply_to_event_id_from_content` accepts a content payload directly and also accepts any `Mapping`.

### 2. Optional vs required timestamp validation is related, not duplicate

`origin_server_ts_from_event_source` at `src/mindroom/matrix/event_info.py:14` returns an optional `int | float` timestamp from any raw event source mapping.
Several cache and approval paths repeat similar type checks, including `event_timestamp_for_cache` at `src/mindroom/matrix/cache/sqlite_event_cache_events.py:44`, `_created_at_ms` at `src/mindroom/approval_events.py:131`, `_latest_visible_event_source` at `src/mindroom/matrix/conversation_cache.py:271`, and `normalize_event_source_for_cache` at `src/mindroom/matrix/cache/event_normalization.py:15`.

These are not direct duplicates because their contracts differ.
Cache serialization requires an integer and raises when absent.
Approval fallback returns `0`.
Conversation-cache edit projection copies only integer timestamps.
The audited helper permits float timestamps for logging receive lag.

### 3. Relation analysis centralization is mostly complete

`EventInfo.from_event` is used by major relation consumers such as `src/mindroom/conversation_resolver.py:240`, `src/mindroom/turn_controller.py:1493`, `src/mindroom/matrix/cache/thread_writes.py:327`, `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:48`, and `src/mindroom/approval_inbound.py:45`.
`EventInfo.next_related_event_id` is also already delegated from `src/mindroom/matrix/thread_membership.py:121`.

Manual relation handling that remains in `src/mindroom/thread_summary.py:240` is a scoped metadata filter for summary events, not a full duplicate of relation classification.
Manual `m.new_content` handling in `src/mindroom/matrix/message_content.py:271` and `src/mindroom/matrix/cache/thread_writes.py:100` extracts visible edit bodies or stream status, not thread-root relation analysis.

## Proposed Generalization

A minimal refactor could add a small content-level helper in `src/mindroom/matrix/event_info.py`, for example `reply_to_event_id_from_content(content: Mapping[str, object] | None) -> str | None`, and have both `_analyze_event_relations` and `src/mindroom/matrix/client_visible_messages.py` call it.
That would remove the one active duplicate without changing the broader `EventInfo.from_event` event-source contract.

No refactor is recommended for timestamp extraction unless callers first agree on one contract for optional vs required timestamps.
No refactor is recommended for `m.new_content` handling because those sites inspect different visible-content and stream-status fields.

## Risk/tests

The reply-target helper is low risk if it preserves Mapping input and non-string rejection.
Tests to run would include reply preview/context coverage in `tests/test_threading_error.py`, `tests/test_turn_controller.py`, and any direct client-visible-message tests that cover replies.

Timestamp helper consolidation would be higher risk because cache writes depend on strict integer validation and explicit exceptions.
If attempted later, tests should cover event cache serialization, approval card creation, and conversation-cache latest-edit projection.

## Questions or Assumptions

Assumption: this task requested report-only auditing, so no production code was edited.
