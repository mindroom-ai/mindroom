Summary: Top duplication candidates are Matrix text send formatting/delivery, model-facing tool JSON payload/context-error boilerplate, bounded pagination helpers, and manual thread summary/tag side effects used during subagent spawn.
The subagent session registry itself appears purpose-specific and has no active duplicate registry elsewhere in `src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_now_iso	function	lines 35-36	related-only	datetime.now UTC isoformat generated_at created_at	src/mindroom/thread_summary.py:419; src/mindroom/matrix/state.py:31; src/mindroom/llm_request_logging.py:290
_now_epoch	function	lines 39-40	related-only	datetime.now UTC timestamp now_epoch created_at_epoch	src/mindroom/memory/auto_flush.py:87; src/mindroom/teams.py:1126; src/mindroom/history/runtime.py:1064
_payload	function	lines 43-49	duplicate-found	json.dumps sort_keys status tool payload custom_tools	src/mindroom/custom_tools/matrix_message.py:45; src/mindroom/custom_tools/matrix_room.py:60; src/mindroom/custom_tools/thread_summary.py:29
_context_error	function	lines 52-57	duplicate-found	get_tool_runtime_context unavailable runtime path context_error	src/mindroom/custom_tools/matrix_message.py:56; src/mindroom/custom_tools/matrix_room.py:63; src/mindroom/custom_tools/thread_summary.py:35; src/mindroom/custom_tools/attachments.py:405
_get_context	function	lines 60-64	related-only	get_tool_runtime_context storage_path is None	src/mindroom/custom_tools/attachments.py:75; src/mindroom/custom_tools/attachments.py:114; src/mindroom/tool_system/runtime_context.py:395
_normalize_spawn_summary	function	lines 67-76	duplicate-found	normalize_thread_summary_text summary non-empty max length	src/mindroom/thread_summary.py:442; src/mindroom/thread_summary.py:456; src/mindroom/thread_summary.py:460
_validate_spawn_metadata	function	lines 79-82	related-only	normalize_thread_summary_text normalize_tag_name ThreadTagsError	src/mindroom/custom_tools/thread_summary.py:75; src/mindroom/custom_tools/thread_tags.py:95; src/mindroom/thread_tags.py:828
_registry_path	function	lines 85-87	none-found	subagents session_registry storage_path registry_path	none
_normalize_registry	function	lines 90-93	related-only	json.loads raw payload isinstance dict registry	src/mindroom/attachments.py:590; src/mindroom/interactive.py:176; src/mindroom/knowledge/refresh_runner.py:839
_load_registry	function	lines 96-106	related-only	read_text json.loads empty file registry	src/mindroom/attachments.py:590; src/mindroom/interactive.py:176; src/mindroom/memory/auto_flush.py:213
_maybe_reuse_spawned_session	async_function	lines 109-152	none-found	reuse spawned session label require_thread spawn_followup_warnings	none
_save_registry	function	lines 155-160	related-only	mkdir write_text json.dumps sort_keys tmp replace	src/mindroom/attachments.py:442; src/mindroom/memory/auto_flush.py:234; src/mindroom/codex_model.py:144
_coerce_epoch	function	lines 163-186	related-only	fromisoformat Z timestamp parse datetime string epoch	src/mindroom/approval_manager.py:102; src/mindroom/approval_events.py:150; src/mindroom/thread_tags.py:107; src/mindroom/scheduling.py:183
_entry_recency	function	lines 189-195	none-found	updated_at_epoch created_at_epoch entry recency	none
_bounded_limit	function	lines 198-201	duplicate-found	max min limit default maximum custom_tools	src/mindroom/custom_tools/matrix_message.py:63; src/mindroom/custom_tools/matrix_room.py:70; src/mindroom/api/sandbox_worker_prep.py:83
_bounded_offset	function	lines 204-207	related-only	bounded offset max zero pagination offset	none
_session_key_to_room_thread	function	lines 210-215	duplicate-found	session_id room_id thread_id build_session_id MessageTarget	src/mindroom/message_target.py:34; src/mindroom/message_target.py:63; src/mindroom/message_target.py:97
_agent_thread_mode	function	lines 218-224	related-only	get_entity_thread_mode room thread resolve_agent_thread_mode	src/mindroom/entity_resolution.py:68; src/mindroom/config/main.py:1609; src/mindroom/delivery_gateway.py:581
_threaded_dispatch_error	function	lines 227-246	related-only	thread_mode room threaded dispatch unsupported get_entity_thread_mode	src/mindroom/delivery_gateway.py:581; src/mindroom/message_target.py:23
_send_matrix_text	async_function	lines 249-280	duplicate-found	format_message_with_mentions get_latest_thread_event_id_if_needed send_message_result notify_outbound_message	src/mindroom/custom_tools/matrix_conversation_operations.py:69; src/mindroom/delivery_gateway.py:523; src/mindroom/thread_summary.py:368
_spawn_room_mode_error	function	lines 283-292	related-only	spawn room mode unsupported thread_mode room	src/mindroom/delivery_gateway.py:581; src/mindroom/message_target.py:23
_spawn_followup_warnings	async_function	lines 295-335	duplicate-found	send_thread_summary_event update_last_summary_count set_thread_tag warnings	src/mindroom/thread_summary.py:442; src/mindroom/thread_tags.py:778; src/mindroom/custom_tools/thread_summary.py:42; src/mindroom/custom_tools/thread_tags.py:86
_spawn_session_payload	async_function	lines 338-386	related-only	spawn message create_session_id record session followup warnings	src/mindroom/turn_controller.py:1112; src/mindroom/message_target.py:34
_record_session	function	lines 389-432	none-found	session_registry label target_agent requester created_at updated_at	none
_in_scope	function	lines 435-441	none-found	agent_name room_id requester_id entry scope	none
_entry_thread_id	function	lines 444-448	none-found	entry thread_id isinstance str non-empty	none
_resolve_by_label	function	lines 451-478	none-found	resolve label registry candidates recency require_thread	none
_lookup_target_agent	function	lines 481-489	none-found	lookup target_agent session_key registry scope	none
SubAgentsTools	class	lines 492-669	not-a-behavior-symbol	Toolkit subagents class registration	none
SubAgentsTools.__init__	method	lines 495-504	related-only	Toolkit name tools list custom_tools init	src/mindroom/custom_tools/thread_summary.py:22; src/mindroom/custom_tools/matrix_message.py:20
SubAgentsTools.agents_list	async_method	lines 506-517	none-found	agents_list sorted config agents current_agent	none
SubAgentsTools.sessions_send	async_method	lines 519-589	related-only	sessions_send MessageTarget send_matrix_text record_session	src/mindroom/custom_tools/matrix_conversation_operations.py:69; src/mindroom/turn_controller.py:1112
SubAgentsTools.sessions_spawn	async_method	lines 591-633	related-only	sessions_spawn validate summary tag spawn message	src/mindroom/custom_tools/thread_summary.py:42; src/mindroom/custom_tools/thread_tags.py:86
SubAgentsTools.list_sessions	async_method	lines 635-669	related-only	list sessions bounded limit offset sort recency	src/mindroom/custom_tools/matrix_message.py:63; src/mindroom/custom_tools/matrix_room.py:70
```

## Findings

1. Matrix text delivery is duplicated between subagents and other Matrix-facing send paths.
`src/mindroom/custom_tools/subagents.py:249` builds formatted Matrix content with `format_message_with_mentions`, optionally resolves the latest thread event, sends with `send_message_result`, notifies `conversation_cache`, and returns the delivered event ID.
The same behavior appears in `src/mindroom/custom_tools/matrix_conversation_operations.py:69` with extra interactive and mention-suppression options, and in the central `DeliveryGateway.send_text` path at `src/mindroom/delivery_gateway.py:523`.
The core behavior is the same: construct text content for a room/thread, preserve thread relation freshness, send it, and notify outbound cache.
Differences to preserve are `ORIGINAL_SENDER_KEY` handling in subagents, `ignore_mentions` and interactive formatting in Matrix message tools, reply-to/tool-trace support in `DeliveryGateway`, and caller labels used for cache diagnostics.

2. JSON payload and context-error boilerplate repeats across model-facing tools.
`src/mindroom/custom_tools/subagents.py:43` and `src/mindroom/custom_tools/subagents.py:52` mirror the sorted JSON status payload helpers in `src/mindroom/custom_tools/matrix_message.py:45`, `src/mindroom/custom_tools/matrix_room.py:60`, and `src/mindroom/custom_tools/thread_summary.py:29`.
Their context-error methods also repeatedly encode nearly identical `"context is unavailable in this runtime path"` errors after `get_tool_runtime_context()` checks, with tool-specific names and messages.
The behavior is functionally the same: tools serialize a stable JSON object with a status and optional tool/action fields, then short-circuit when runtime context is unavailable.
Differences to preserve are whether the payload includes `"tool"`, an `"action"`, or a tool-specific context error message.

3. Bounded limit clamping is duplicated in several custom tools.
`src/mindroom/custom_tools/subagents.py:198` clamps optional limits to a default and maximum.
`src/mindroom/custom_tools/matrix_message.py:63` and `src/mindroom/custom_tools/matrix_room.py:70` perform the same operation with different defaults and caps.
The shared behavior is a reusable optional integer clamp of `None -> default` and otherwise `max(1, min(limit, maximum))`.
The limits themselves must remain call-site parameters because subagent sessions cap at 200 while Matrix read/thread lists cap at 50.

4. Spawn follow-up summary/tag work duplicates manual thread summary and tag orchestration at a smaller scale.
`src/mindroom/custom_tools/subagents.py:295` sends a manual thread summary, updates the summary count, then writes a thread tag while collecting warnings.
The same primitives are exposed independently by `src/mindroom/thread_summary.py:442` and `src/mindroom/thread_tags.py:778`, through tool adapters in `src/mindroom/custom_tools/thread_summary.py:42` and `src/mindroom/custom_tools/thread_tags.py:86`.
This is related active duplication because subagents manually compose both side effects instead of reusing a small "set summary and tag" helper.
Differences to preserve are subagents' warning-return behavior and fixed summary message counts for new versus reused spawned sessions.

5. Session-key parsing duplicates the canonical session-id format only partially.
`src/mindroom/custom_tools/subagents.py:210` parses persisted session keys by splitting on `":$"` and restoring the Matrix event `$` prefix.
`src/mindroom/message_target.py:34` builds session IDs as `room_id` or `f"{room_id}:{resolved_thread_id}"`, and `src/mindroom/message_target.py:63` uses that value from runtime context.
The functional overlap is the same persisted room/thread session identifier format.
Subagents need the reverse parse operation, while `MessageTarget` currently only owns construction.

## Proposed Generalization

1. Add a tiny shared Matrix text-send helper near the existing delivery code, or expose a `ToolRuntimeContext` adapter that can build a `SendTextRequest` for `DeliveryGateway` when one is available.
Keep caller labels, `ORIGINAL_SENDER_KEY`, and skip-mention behavior explicit parameters.

2. Add a small custom-tool response helper, for example `mindroom.custom_tools.tool_payloads`, with `tool_payload(tool_name, status, **fields)` and `context_error_payload(tool_name, message, **fields)`.
Adopt only in touched tools to avoid broad churn.

3. Add a generic `bounded_int(value, *, default, minimum=1, maximum)` helper in a low-level utility module only if another custom-tool edit is already touching these call sites.
This is low-risk but low-impact.

4. Add `MessageTarget.parse_session_id(session_id: str) -> tuple[str, str | None]` beside `_build_session_id`.
Subagents can then rely on the same source of truth for the session-id format.

5. Consider a focused helper for "write manual thread metadata" that accepts summary, tag, message counts, requester, and warning/error mode.
Do this only if another feature needs both summary and tag writes; the current duplication is moderate and not worth a broad refactor alone.

## Risk/Tests

Matrix text-send deduplication has the highest behavioral risk because thread relations, mention formatting, original sender metadata, and cache notification affect dispatch behavior.
Tests should cover room-level send, threaded send, original-sender preservation, mention suppression where applicable, and `conversation_cache.notify_outbound_message`.

Payload helper changes are low risk but need snapshot-style assertions for exact JSON keys and sorting, because model-facing tool outputs may be consumed by prompts or tests.

Limit clamping is low risk and can be covered with direct unit tests for `None`, below-minimum, normal, and above-maximum inputs.

Session-id parse centralization should include Matrix event IDs containing `$` and room IDs containing colons, because the existing parser relies on `rsplit(":$", 1)`.

Assumption: this audit is report-only, so no production refactor is recommended in this task.
