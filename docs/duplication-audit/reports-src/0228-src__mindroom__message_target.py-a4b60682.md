Summary: One meaningful duplication candidate was found.
`MessageTarget._build_session_id()` duplicates `thread_utils.create_session_id()` for canonical room/thread session IDs.
`thread_summary.thread_summary_cache_key()` uses the same string shape, but its scope is an in-memory summary cache rather than persisted conversation/session identity.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MessageTarget	class	lines 14-98	duplicate-found	MessageTarget dataclass room_id source_thread_id resolved_thread_id reply_to_event_id session_id; direct constructors; target metadata	src/mindroom/handled_turns.py:1040; src/mindroom/api/openai_compat.py:944; src/mindroom/api/credentials.py:304; src/mindroom/tool_system/runtime_context.py:103
MessageTarget.is_room_mode	method	lines 24-26	related-only	is_room_mode room_mode resolved_thread_id is None effective_thread_id None	src/mindroom/streaming.py:333; src/mindroom/delivery_gateway.py:1080; src/mindroom/conversation_resolver.py:552
MessageTarget.log_context	method	lines 29-31	related-only	log_context bound_log_context room_id thread_id resolved_thread_id	logger contexts checked at src/mindroom/inbound_turn_normalizer.py:174; src/mindroom/turn_controller.py:1046; src/mindroom/interactive.py:487
MessageTarget._build_session_id	method	lines 34-36	duplicate-found	create_session_id session_id f"{room_id}:{thread_id}" room_id thread_id	src/mindroom/thread_utils.py:83; src/mindroom/thread_summary.py:124; src/mindroom/turn_store.py:347; src/mindroom/custom_tools/subagents.py:362
MessageTarget.for_scheduled_task	method	lines 39-53	related-only	for_scheduled_task ScheduledWorkflow workflow.room_id new_thread thread_id MessageTarget.resolve	src/mindroom/scheduling.py:739; src/mindroom/scheduling.py:799; src/mindroom/scheduling.py:894; src/mindroom/custom_tools/scheduler.py:56
MessageTarget.from_runtime_context	method	lines 56-64	related-only	from_runtime_context ToolRuntimeContext target session_id resolved_thread_id runtime_context	src/mindroom/tool_system/runtime_context.py:103; src/mindroom/tool_system/runtime_context.py:200; src/mindroom/tool_system/runtime_context.py:418
MessageTarget.with_thread_root	method	lines 66-76	related-only	with_thread_root resolved_thread_id replace target thread root session_id	src/mindroom/response_runner.py:924; src/mindroom/response_runner.py:950; src/mindroom/handled_turns.py:1040
MessageTarget.resolve	method	lines 79-98	related-only	MessageTarget.resolve build_message_target thread_start_root_event_id room_mode effective_thread_id	src/mindroom/conversation_resolver.py:177; src/mindroom/inbound_turn_normalizer.py:168; src/mindroom/commands/handler.py:210; src/mindroom/turn_controller.py:1787
```

Findings:

1. Canonical room/thread session ID construction is duplicated.
   `MessageTarget._build_session_id()` returns `room_id` when the resolved thread is `None`, otherwise `f"{room_id}:{resolved_thread_id}"`.
   `thread_utils.create_session_id()` at `src/mindroom/thread_utils.py:83` implements the same behavior for `thread_id`.
   `turn_store.py` uses `create_session_id()` at `src/mindroom/turn_store.py:347` and `src/mindroom/turn_store.py:399`, while `handled_turns.py` uses `MessageTarget._build_session_id()` at `src/mindroom/handled_turns.py:1048`.
   These are functionally the same room/thread conversation key operation and should not drift.

2. Thread summary cache keys are shape-related but not the same domain behavior.
   `thread_summary_cache_key()` at `src/mindroom/thread_summary.py:124` also returns `f"{room_id}:{thread_id}"`.
   It is only for an in-memory summary cache and requires a non-optional thread ID, so this is related string formatting rather than a strong refactor candidate.

3. Target resolution and room-mode handling are already centralized.
   `conversation_resolver.build_message_target()` at `src/mindroom/conversation_resolver.py:177` delegates to `MessageTarget.resolve()`.
   Downstream voice, command, response, delivery, and streaming paths generally consume `MessageTarget` fields or properties rather than reimplementing the full target resolution.

Proposed generalization:

Move the canonical session-key helper to one public source of truth, preferably `mindroom.thread_utils.create_session_id()` or a small neutral helper if `thread_utils` is too broad.
Then have `MessageTarget._build_session_id()` delegate to it, or replace `_build_session_id()` call sites with the public helper.
Do not fold `thread_summary_cache_key()` into this unless the team explicitly wants cache keys and persisted session IDs to share a named contract.

Minimal refactor plan:

1. Keep `MessageTarget` as the canonical target metadata type.
2. Pick one public helper for room/thread session IDs.
3. Update `MessageTarget._build_session_id()` and direct persisted-session call sites to use that helper.
4. Leave thread summary cache keys unchanged unless a separate cache-key cleanup is desired.
5. Add/adjust focused tests for room-level and thread-level session IDs.

Risk/tests:

The main risk is accidentally changing the persisted session key format, which would affect existing conversation/session lookups.
Tests should cover `MessageTarget.resolve()`, `MessageTarget.from_runtime_context()`, `thread_utils.create_session_id()`, and any turn-store lookup paths that depend on the exact `room_id[:thread_id]` format.
No production code was edited.
