## Summary

Top duplication candidate: `approval_events._parse_datetime` is a literal duplicate of `approval_manager._parse_datetime`.
Related but not duplicate behavior exists around approval-card content construction in `approval_manager.py`, approval response parsing in `approval_inbound.py`, generic Matrix edit detection in `matrix.event_info`, and visible replacement-content extraction in `matrix.visible_body`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
PendingApproval	class	lines 16-98	related-only	PendingApproval approval_id tool_call_id tool_name approver_user_id io.mindroom.tool_approval	/src/mindroom/approval_manager.py:18 /src/mindroom/approval_manager.py:650 /src/mindroom/approval_manager.py:1090 /src/mindroom/approval_manager.py:1107 /src/mindroom/approval_manager.py:1143 /src/mindroom/approval_inbound.py:29
PendingApproval.from_card_event	method	lines 36-86	related-only	from_card_event approval card parsing approval_id tool_call_id arguments_truncated requested_at expires_at	/src/mindroom/approval_manager.py:650 /src/mindroom/approval_manager.py:867 /src/mindroom/approval_manager.py:1090 /src/mindroom/approval_manager.py:1107 /src/mindroom/approval_manager.py:1143 /src/mindroom/approval_inbound.py:39
PendingApproval.latest_status	method	lines 88-98	related-only	latest_status visible_content_from_content status pending approved denied expired m.new_content	/src/mindroom/matrix/visible_body.py:41 /src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:33 /src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:32 /src/mindroom/approval_manager.py:857
is_original_approval_card	function	lines 101-108	related-only	is_original_approval_card io.mindroom.tool_approval m.replace is_edit	/src/mindroom/approval_manager.py:867 /src/mindroom/approval_manager.py:876 /src/mindroom/matrix/event_info.py:97 /src/mindroom/matrix/event_info.py:152 /src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:40 /src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:39
terminal_edit_matches_card_sender	function	lines 111-115	none-found	terminal_edit_matches_card_sender latest edit sender card_sender_id get_latest_edit	/src/mindroom/approval_manager.py:850 /src/mindroom/approval_manager.py:857
_required_str	function	lines 118-123	related-only	required string event_id sender missing value isinstance str	/src/mindroom/approval_inbound.py:46 /src/mindroom/approval_transport.py:248 /src/mindroom/matrix/event_info.py:167
_content_str	function	lines 126-128	related-only	content string nonempty approval_id reason agent_name thread_id	/src/mindroom/approval_inbound.py:46 /src/mindroom/approval_inbound.py:54 /src/mindroom/approval_transport.py:65 /src/mindroom/approval_transport.py:285
_created_at_ms	function	lines 131-136	related-only	created_at_ms origin_server_ts requested_at timestamp bool origin_server_ts_from_event_source	/src/mindroom/matrix/event_info.py:14 /src/mindroom/bot.py:1346 /src/mindroom/approval_manager.py:1090
_timeout_seconds	function	lines 139-144	related-only	timeout_seconds requested_at expires_at total_seconds max zero	/src/mindroom/approval_manager.py:287 /src/mindroom/approval_manager.py:673 /src/mindroom/approval_manager.py:1151
_parse_datetime	function	lines 147-151	duplicate-found	def _parse_datetime datetime.fromisoformat tzinfo UTC replace	/src/mindroom/approval_manager.py:99 /src/mindroom/scheduling.py:176 /src/mindroom/thread_tags.py:107 /src/mindroom/commands/config_confirmation.py:60
_is_replace_content	function	lines 154-155	related-only	is_replace_content EventInfo.from_event is_edit rel_type m.replace	/src/mindroom/matrix/event_info.py:97 /src/mindroom/matrix/event_info.py:152 /src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:48 /src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:47
```

## Findings

### Duplicate: timezone-aware ISO datetime parsing

`src/mindroom/approval_events.py:147` and `src/mindroom/approval_manager.py:99` contain the same helper:
both accept `str | None`, return `None` for missing input, parse with `datetime.fromisoformat`, and attach `UTC` when the parsed datetime is naive.
This is functional duplication in the approval subsystem, and both sides are active in the same approval-card lifecycle.

Differences to preserve: none observed between these two helpers.
Other datetime parsing call sites are only related.
For example, `src/mindroom/scheduling.py:176` normalizes trailing `Z` and catches parse errors, while `src/mindroom/thread_tags.py:107` parses tag timestamps inline and catches `ValueError`.

### Related: approval card construction and approval card parsing

`PendingApproval.from_card_event` in `src/mindroom/approval_events.py:36` is the read-side projection for approval cards.
`ApprovalManager._pending_event_content` and `_resolved_event_content` in `src/mindroom/approval_manager.py:1107` and `src/mindroom/approval_manager.py:1143` are the write-side schema for the same event content fields.
The field lists intentionally mirror one another (`approval_id`, `tool_call_id`, `tool_name`, `arguments`, `arguments_truncated`, `approver_user_id`, `requested_at`, `expires_at`, `thread_id`, `agent_name`, `requester_id`, and `status`), but they are not duplicate behavior because one constructs outbound Matrix content and the other validates/parses cached inbound Matrix events.

### Related: generic Matrix edit and visible-content helpers

`_is_replace_content` in `src/mindroom/approval_events.py:154` delegates to the shared `EventInfo` relation parser in `src/mindroom/matrix/event_info.py:97`.
`PendingApproval.latest_status` uses `visible_content_from_content` from `src/mindroom/matrix/visible_body.py:41`, which is also used by cache snapshot readers in `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:33` and `src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:32`.
These are related reuse points, not local duplication in `approval_events.py`.

### Related: optional string extraction

`_content_str` and `_required_str` centralize approval-card-specific string validation locally.
There are small inline string checks elsewhere, such as `src/mindroom/approval_inbound.py:46`, `src/mindroom/approval_inbound.py:54`, and `src/mindroom/approval_transport.py:65`, but their payloads and error behavior differ.
No cross-module helper is recommended for these small checks.

## Proposed Generalization

Minimal refactor: extract the duplicated approval datetime parser into a small shared helper, for example `mindroom.approval_datetime.parse_approval_datetime(value: str | None) -> datetime | None`, and use it from both `approval_events.py` and `approval_manager.py`.

No broader refactor is recommended for approval card parsing or Matrix edit detection.
The existing code already delegates generic Matrix relation and visible-content behavior to shared helpers.

## Risk/tests

Risk is low for extracting the datetime parser because the duplicate implementations are identical.
Tests should cover naive ISO values gaining UTC, aware ISO values preserving their offset, and `None` returning `None`.
Approval behavior tests that parse cards and emit resolved approval content should still pass, especially paths using `PendingApproval.from_card_event` and `ApprovalManager._resolved_event_content`.
