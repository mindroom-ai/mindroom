## Summary

Top duplication candidates for `src/mindroom/bot.py`:

1. `ResponseRequest` assembly is repeated in `AgentBot._generate_response`, `AgentBot._generate_team_response_helper`, `TeamBot._generate_response`, and `TurnController` response dispatch paths.
2. Single-background-task start/cancel bookkeeping is repeated for startup thread prewarm and deferred overdue task draining.
3. `TeamBot._generate_response` repeats a subset of team response preparation already centralized in `ResponseRunner.generate_team_response_helper_locked`, but with bot-specific fallback and memory behavior that should be preserved.

Most remaining symbols are lifecycle wiring, property delegates, or compatibility pass-throughs to already extracted collaborators.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_create_task_wrapper	function	lines 130-157	related-only	create_background_task add_event_callback error_handler	src/mindroom/background_tasks.py:21; src/mindroom/matrix/cache/write_coordinator.py:566
_create_task_wrapper.<locals>.wrapper	nested_async_function	lines 142-155	related-only	add_event_callback wrapper background task	src/mindroom/bot.py:1121; src/mindroom/background_tasks.py:21
_create_task_wrapper.<locals>.error_handler	nested_async_function	lines 144-152	related-only	CancelledError logger.exception background task	src/mindroom/background_tasks.py:21; src/mindroom/post_response_effects.py:151
create_bot_for_entity	function	lines 160-233	related-only	create_bot_for_entity TeamBot AgentBot MatrixID.from_agent resolve_room_aliases	src/mindroom/orchestrator.py:355; src/mindroom/matrix/rooms.py:459
AgentBot	class	lines 239-1775	related-only	AgentBot lifecycle shell BotRoomLifecycle ResponseRunner TurnController	src/mindroom/bot_room_lifecycle.py:51; src/mindroom/response_runner.py:376; src/mindroom/turn_controller.py:79
AgentBot.__init__	method	lines 281-333	related-only	BotRuntimeState BotRoomLifecycle deps init	src/mindroom/bot_room_lifecycle.py:37; src/mindroom/runtime_support.py:143
AgentBot._init_runtime_components	method	lines 335-495	related-only	Deps runtime_view support collaborators	src/mindroom/runtime_support.py:143; src/mindroom/turn_controller.py:79
AgentBot.client	method	lines 498-500; lines 503-505	not-a-behavior-symbol	property delegate runtime_view client; property setter runtime_view client	none
AgentBot.config	method	lines 508-510; lines 513-515	not-a-behavior-symbol	property delegate runtime_view config; property setter runtime_view config	none
AgentBot.enable_streaming	method	lines 518-520; lines 523-525	not-a-behavior-symbol	property delegate runtime_view enable_streaming; property setter runtime_view enable_streaming	none
AgentBot.orchestrator	method	lines 528-530; lines 533-535	not-a-behavior-symbol	property delegate runtime_view orchestrator; property setter runtime_view orchestrator	none
AgentBot.event_cache	method	lines 538-544; lines 547-549	related-only; not-a-behavior-symbol	property guarded runtime_view event_cache; property setter runtime_view event_cache	src/mindroom/bot_runtime_view.py:64; none
AgentBot.event_cache_write_coordinator	method	lines 552-558; lines 561-563	related-only; not-a-behavior-symbol	property guarded runtime_view event_cache_write_coordinator; property setter runtime_view coordinator	src/mindroom/bot_runtime_view.py:66; none
AgentBot.startup_thread_prewarm_registry	method	lines 566-572; lines 575-577	related-only; not-a-behavior-symbol	property guarded startup_thread_prewarm_registry; property setter startup_thread_prewarm_registry	src/mindroom/bot_runtime_view.py:67; src/mindroom/runtime_support.py:143; none
AgentBot.runtime_started_at	method	lines 580-582	not-a-behavior-symbol	property delegate runtime_started_at	none
AgentBot.latest_thread_event_id_if_needed	async_method	lines 584-596	related-only	get_latest_thread_event_id_if_needed caller_label	src/mindroom/matrix/conversation_cache.py:936; src/mindroom/conversation_resolver.py:249
AgentBot.hook_registry	method	lines 599-601; lines 604-606	not-a-behavior-symbol	property delegate HookRegistryState; property setter HookRegistryState	none
AgentBot.in_flight_response_count	method	lines 609-611; lines 614-616	not-a-behavior-symbol	property delegate ResponseRunner count; property setter ResponseRunner count	none
AgentBot.agent_name	method	lines 619-621	not-a-behavior-symbol	property delegate AgentMatrixUser agent_name	none
AgentBot.matrix_id	method	lines 624-626	not-a-behavior-symbol	cached_property AgentMatrixUser matrix_id	none
AgentBot._entity_type	method	lines 628-634	related-only	router team agent entity type lifecycle	src/mindroom/hooks/context.py:467; src/mindroom/matrix/invited_rooms_store.py:82
AgentBot._startup_thread_prewarm_enabled	method	lines 636-642	related-only	startup_thread_prewarm router teams agents	src/mindroom/config/agent.py:223; src/mindroom/config/agent.py:400; src/mindroom/config/models.py:507
AgentBot._maybe_start_startup_thread_prewarm	method	lines 644-657	duplicate-found	single task start create_background_task done shutdown	src/mindroom/bot.py:1256; src/mindroom/background_tasks.py:21
AgentBot._get_startup_thread_prewarm_joined_rooms	async_method	lines 659-673	related-only	get_joined_rooms fail open warning	src/mindroom/matrix/stale_stream_cleanup.py:134; src/mindroom/api/matrix_operations.py:100
AgentBot._prewarm_claimed_startup_thread_room	async_method	lines 675-686	none-found	prewarm_recent_room_threads release try_claim	cancel logic unique to bot.py
AgentBot._run_startup_thread_prewarm	async_method	lines 688-701	related-only	try_claim release prewarm room current_task cleanup	src/mindroom/matrix/conversation_cache.py:717
AgentBot.has_active_response_for_target	method	lines 703-705	not-a-behavior-symbol	delegate ResponseRunner active response target	none
AgentBot._emit_reaction_received_hooks	async_method	lines 707-749	related-only	ReactionReceivedContext emit hook resolve thread	src/mindroom/hooks/context.py:532; src/mindroom/hooks/execution.py:80
AgentBot._emit_agent_lifecycle_event	async_method	lines 751-777	related-only	AgentLifecycleContext EVENT_BOT_READY emit	src/mindroom/hooks/context.py:467; src/mindroom/hooks/execution.py:67
AgentBot.show_tool_calls	method	lines 780-782	not-a-behavior-symbol	delegate show_tool_calls_for_agent	none
AgentBot.agent	method	lines 785-803	related-only	create_agent knowledge execution_identity hook_registry	src/mindroom/agents.py:1; src/mindroom/tool_system/worker_routing.py:1
AgentBot._should_accept_invite	method	lines 805-807	not-a-behavior-symbol	delegate BotRoomLifecycle	none
AgentBot._should_persist_invited_rooms	method	lines 809-811	not-a-behavior-symbol	delegate BotRoomLifecycle invited rooms	none
AgentBot._invited_rooms_path	method	lines 813-815	not-a-behavior-symbol	delegate BotRoomLifecycle invited path	none
AgentBot._load_invited_rooms	method	lines 817-819	not-a-behavior-symbol	delegate BotRoomLifecycle load invited	none
AgentBot._save_invited_rooms	method	lines 821-823	not-a-behavior-symbol	delegate BotRoomLifecycle save invited	none
AgentBot.join_configured_rooms	async_method	lines 825-827	not-a-behavior-symbol	delegate BotRoomLifecycle join	none
AgentBot._post_join_room_setup	async_method	lines 829-858	related-only	restore_scheduled_tasks restore_pending_changes welcome deferred drain	src/mindroom/scheduling.py:1609; src/mindroom/commands/config_confirmation.py:1
AgentBot.leave_unconfigured_rooms	async_method	lines 860-865	related-only	rooms_to_actually_leave leave_unconfigured_rooms	src/mindroom/bot_room_lifecycle.py:121; src/mindroom/bot_room_lifecycle.py:147
AgentBot.ensure_user_account	async_method	lines 867-883	related-only	create_agent_user login_agent_user user_id	src/mindroom/matrix/users.py:1; src/mindroom/orchestrator.py:355
AgentBot._set_avatar_if_available	async_method	lines 885-901	related-only	resolve_avatar_path check_and_set_avatar agent team	src/mindroom/matrix/rooms.py:63; src/mindroom/matrix/avatar.py:114
AgentBot._set_presence_with_model_info	async_method	lines 903-909	related-only	build_agent_status_message set_presence_status	src/mindroom/matrix/presence.py:18
AgentBot.mark_sync_loop_started	method	lines 911-919	related-only	mark_matrix_sync_loop_started sync health	src/mindroom/matrix/health.py:69; src/mindroom/orchestration/runtime.py:219
AgentBot.reset_watchdog_clock	method	lines 921-923	not-a-behavior-symbol	reset monotonic watchdog field	none
AgentBot._loaded_sync_token_for_certification	method	lines 925-938	related-only	load_sync_token_record certified legacy token	src/mindroom/matrix/sync_tokens.py:105; src/mindroom/matrix/sync_certification.py:70
AgentBot._restore_saved_sync_token	method	lines 940-949	related-only	start_from_loaded_token client.next_batch legacy token	src/mindroom/matrix/sync_certification.py:70; src/mindroom/matrix/sync_tokens.py:99
AgentBot._save_sync_checkpoint	method	lines 951-962	related-only	save_sync_token checkpoint OSError ValueError	src/mindroom/matrix/sync_tokens.py:72; src/mindroom/matrix/cache/write_coordinator.py:878
AgentBot._clear_saved_sync_token	method	lines 964-969	related-only	clear_sync_token OSError warning	src/mindroom/matrix/sync_tokens.py:89
AgentBot._apply_sync_certification_decision	method	lines 971-993	related-only	SyncCertificationDecision reset_client_token clear_saved_token diagnostics	src/mindroom/matrix/sync_certification.py:1
AgentBot._sync_cache_result_for_certification	async_method	lines 995-997	not-a-behavior-symbol	delegate cache_sync_timeline_for_certification	none
AgentBot._sync_certification_decision	method	lines 999-1012	not-a-behavior-symbol	delegate certify_sync_response	none
AgentBot.seconds_since_last_sync_activity	method	lines 1014-1018	related-only	time.monotonic watchdog last activity	src/mindroom/orchestration/runtime.py:219
AgentBot._on_sync_response	async_method	lines 1020-1057	related-only	SyncResponse mark success certify first_sync bot_ready deferred	src/mindroom/orchestration/runtime.py:219; src/mindroom/matrix/cache/write_coordinator.py:878
AgentBot._on_sync_error	async_method	lines 1059-1070	related-only	SyncError M_UNKNOWN_POS handle_unknown_pos	src/mindroom/matrix/sync_certification.py:1
AgentBot.ensure_rooms	async_method	lines 1072-1080	related-only	join configured leave unconfigured lifecycle	src/mindroom/bot_room_lifecycle.py:98; src/mindroom/bot_room_lifecycle.py:121
AgentBot._runtime_support_injection_error	method	lines 1083-1088	not-a-behavior-symbol	constant error text	none
AgentBot._validate_runtime_support_injection_contract_for_startup	method	lines 1090-1099	related-only	runtime support injected startup prewarm registry	src/mindroom/runtime_support.py:143; src/mindroom/orchestrator.py:355
AgentBot.start	async_method	lines 1101-1170	related-only	login add callbacks startup lifecycle cleanup orphaned	src/mindroom/orchestration/runtime.py:219; src/mindroom/matrix/client_session.py:1
AgentBot.try_start	async_method	lines 1172-1199	related-only	tenacity retry PermanentMatrixStartupError start	src/mindroom/orchestrator.py:471; src/mindroom/orchestration/runtime.py:120
AgentBot.try_start.<locals>._start_with_retry	nested_async_function	lines 1188-1189	related-only	retry wrapper start	none
AgentBot.cleanup	async_method	lines 1201-1216	related-only	get_joined_rooms leave_non_dm_rooms stop	src/mindroom/bot_room_lifecycle.py:121; src/mindroom/matrix/rooms.py:697
AgentBot.stop	async_method	lines 1218-1247	related-only	wait background tasks clear deferred cancel scheduled close client	src/mindroom/matrix/cache/write_coordinator.py:878; src/mindroom/scheduling.py:403
AgentBot._send_welcome_message_if_empty	async_method	lines 1249-1254	not-a-behavior-symbol	delegate BotRoomLifecycle welcome	none
AgentBot._maybe_start_deferred_overdue_task_drain	method	lines 1256-1268	duplicate-found	single task start asyncio.create_task done shutdown	src/mindroom/bot.py:644; src/mindroom/bot.py:1301
AgentBot._drain_deferred_overdue_task_queue	async_method	lines 1270-1287	related-only	drain_deferred_overdue_tasks exception log	src/mindroom/scheduling.py:362
AgentBot._cancel_deferred_overdue_task_drain	async_method	lines 1289-1299	duplicate-found	cancel task field gather return_exceptions	src/mindroom/bot.py:1301
AgentBot._cancel_startup_thread_prewarm	async_method	lines 1301-1311	duplicate-found	cancel task field gather return_exceptions	src/mindroom/bot.py:1289
AgentBot.prepare_for_sync_shutdown	async_method	lines 1313-1323	related-only	cancel prewarm drain coalescing save checkpoint	src/mindroom/orchestration/runtime.py:247
AgentBot.sync_forever	async_method	lines 1325-1328	not-a-behavior-symbol	delegate client.sync_forever timeout	none
AgentBot._on_invite	async_method	lines 1330-1331	not-a-behavior-symbol	delegate BotRoomLifecycle invite	none
AgentBot._dispatch_coalesced_batch	async_method	lines 1333-1335	not-a-behavior-symbol	delegate TurnController coalesced batch	none
AgentBot._log_matrix_event_callback_started	method	lines 1337-1357	related-only	Matrix ingress timing origin_server_ts log	src/mindroom/matrix/event_info.py:1; src/mindroom/turn_controller.py:1
AgentBot._on_message	async_method	lines 1359-1371	related-only	tool approval reply then text event	src/mindroom/approval_inbound.py:112; src/mindroom/turn_controller.py:1
AgentBot._on_redaction	async_method	lines 1373-1375	not-a-behavior-symbol	delegate conversation_cache redaction	none
AgentBot._on_reaction	async_method	lines 1377-1380	related-only	turn_thread_cache_scope reaction inner	src/mindroom/conversation_resolver.py:1
AgentBot._on_unknown_event	async_method	lines 1382-1404	related-only	custom tool approval response parse handle	src/mindroom/approval_inbound.py:66
AgentBot._handle_reaction_inner	async_method	lines 1406-1483	related-only	approval authorization stop config interactive hooks	src/mindroom/stop.py:242; src/mindroom/interactive.py:1; src/mindroom/commands/config_confirmation.py:1
AgentBot._on_media_message	async_method	lines 1485-1492	related-only	log Matrix event then media turn	src/mindroom/turn_controller.py:1
AgentBot._should_queue_follow_up_in_active_response_thread	method	lines 1494-1510	related-only	active response follow-up automation agent sender checks	src/mindroom/turn_controller.py:1281; src/mindroom/dispatch_source.py:1
AgentBot._agent_has_matrix_messaging_tool	method	lines 1512-1520	related-only	get_agent_tools matrix_message validation	src/mindroom/config/main.py:1; src/mindroom/custom_tools/matrix_api.py:1
AgentBot._generate_team_response_helper	async_method	lines 1522-1567	duplicate-found	ResponseRequest construction team response helper	src/mindroom/turn_controller.py:1357; src/mindroom/response_runner.py:830
AgentBot._generate_response	async_method	lines 1569-1639	duplicate-found	ResponseRequest construction generate_response	src/mindroom/turn_controller.py:978; src/mindroom/turn_controller.py:1378; src/mindroom/response_runner.py:2081
AgentBot._send_response	async_method	lines 1641-1670	related-only	build MessageTarget SendTextRequest send_text	src/mindroom/turn_controller.py:741; src/mindroom/delivery_gateway.py:523
AgentBot._hook_send_message	async_method	lines 1672-1704	related-only	send_hook_message client guard logging	src/mindroom/hooks/sender.py:50; src/mindroom/hooks/sender.py:119
AgentBot._hook_agent_message_snapshot	async_method	lines 1706-1729	related-only	event_cache guard get_latest_agent_message_snapshot	src/mindroom/hooks/context.py:1
AgentBot._edit_message	async_method	lines 1731-1758	related-only	build MessageTarget EditTextRequest edit_text	src/mindroom/delivery_gateway.py:576; src/mindroom/delivery_gateway.py:772
AgentBot._redact_message_event	async_method	lines 1760-1775	related-only	room_redact error notify_outbound_redaction	src/mindroom/stop.py:258; src/mindroom/custom_tools/matrix_api.py:1183; src/mindroom/matrix/stale_stream_cleanup.py:1223
TeamBot	class	lines 1778-1973	related-only	TeamBot team response resolve_configured_team ResponseRunner	src/mindroom/teams.py:1087; src/mindroom/response_runner.py:830
TeamBot.__init__	method	lines 1786-1812	related-only	super init team fields defaults	src/mindroom/config/agent.py:1
TeamBot.agent	method	lines 1815-1817	not-a-behavior-symbol	team agent None property	none
TeamBot._generate_response	async_method	lines 1819-1973	duplicate-found	team response preparation ResponseRequest memory team resolution	src/mindroom/response_runner.py:869; src/mindroom/turn_controller.py:1357; src/mindroom/api/openai_compat.py:1408
```

## Findings

### 1. Repeated `ResponseRequest` assembly

`AgentBot._generate_response` creates a `ResponseRequest` from the legacy bot method signature before delegating to `ResponseRunner.generate_response` at `src/mindroom/bot.py:1619`.
`AgentBot._generate_team_response_helper` performs the same request assembly shape before delegating to `ResponseRunner.generate_team_response_helper` at `src/mindroom/bot.py:1544`.
`TeamBot._generate_response` repeats the same request construction for empty prompts at `src/mindroom/bot.py:1841` and again constructs the equivalent payload for team generation at `src/mindroom/bot.py:1938`.
`TurnController` also constructs `ResponseRequest` directly in its normal, coalesced, and team handoff paths at `src/mindroom/turn_controller.py:978`, `src/mindroom/turn_controller.py:1358`, and `src/mindroom/turn_controller.py:1379`.

The duplicated behavior is the mapping from Matrix turn parameters into the canonical `ResponseRequest`, including repeated conversion of optional attachment lists to tuples, response envelope propagation, target propagation, matrix run metadata propagation, and lifecycle callback propagation.

Differences to preserve:

- `AgentBot._generate_response` accepts a direct `prompt` while `_generate_team_response_helper` receives a `DispatchPayload`.
- `TeamBot._generate_response` performs empty-prompt handling before memory/model preparation.
- `TurnController` sometimes supplies `prepare_after_lock`, and bot wrappers currently do not.

### 2. Repeated one-shot background task lifecycle

`AgentBot._maybe_start_startup_thread_prewarm` checks runtime guards, avoids duplicate running tasks, and stores a created task at `src/mindroom/bot.py:644`.
`AgentBot._maybe_start_deferred_overdue_task_drain` repeats the same "if no live task, start and store one task" pattern at `src/mindroom/bot.py:1256`.
`AgentBot._cancel_deferred_overdue_task_drain` and `AgentBot._cancel_startup_thread_prewarm` repeat identical cancellation and `asyncio.gather(..., return_exceptions=True)` cleanup at `src/mindroom/bot.py:1289` and `src/mindroom/bot.py:1301`.

The duplicated behavior is small but exact: a nullable task field is nulled, cancelled if pending, and awaited with exceptions captured.

Differences to preserve:

- Startup prewarm uses `create_background_task(..., owner=self._runtime_view)` and clears its field in `_run_startup_thread_prewarm`.
- Deferred overdue drain currently uses raw `asyncio.create_task`.
- Deferred drain is router-only, while startup prewarm is entity-config controlled.

### 3. Team response preparation overlaps with `ResponseRunner`

`TeamBot._generate_response` prepares memory/model prompt variants, resolves team membership, handles non-team resolution by editing or sending a visible message, builds an execution identity, schedules memory storage, creates a fallback `MessageEnvelope`, and delegates to `_generate_team_response_helper` at `src/mindroom/bot.py:1819`.
`ResponseRunner.generate_team_response_helper_locked` also prepares memory/model context for team response execution at `src/mindroom/response_runner.py:869`, and `TurnController` has direct team response request assembly at `src/mindroom/turn_controller.py:1357`.
`api/openai_compat.py` resolves configured teams independently for OpenAI-compatible requests at `src/mindroom/api/openai_compat.py:1408`.

The overlap is real around team resolution and response request setup, but the bot-specific path still has unique Matrix-facing behavior for empty prompts, visible fallback edits/sends, execution identity, and background memory persistence.

Differences to preserve:

- Matrix bot team responses must preserve existing visible-event editing behavior for invalid team resolution.
- OpenAI-compatible team handling is not Matrix delivery and should not share UI message fallback.
- `ResponseRunner` owns response lifecycle locking and delivery, so extracting too much from `TeamBot` risks mixing ingress-specific and generation-specific responsibilities.

## Proposed Generalization

1. Add a small `ResponseRequest.from_turn_parts(...)` classmethod or local helper in `src/mindroom/response_runner.py` that accepts the common fields and centralizes `attachment_ids` tuple conversion and metadata propagation.
2. Use it in `AgentBot._generate_response`, `AgentBot._generate_team_response_helper`, `TeamBot._generate_response`, and the direct `TurnController` call sites.
3. Consider a tiny private helper in `AgentBot` for task cancellation, for example `_cancel_task(task: asyncio.Task[None] | None)`, only if future edits touch these methods.
4. Do not extract `TeamBot._generate_response` wholesale yet; the duplicated portions are mixed with bot-specific Matrix delivery behavior.

## Risk/Tests

The `ResponseRequest` generalization would need tests around attachment ID normalization, empty-prompt team responses, coalesced response regeneration, and team handoff dispatch.
The task lifecycle helper would need shutdown tests that verify pending tasks are cancelled, finished tasks are awaited harmlessly, and startup prewarm still releases room claims on cancellation.
No production code was changed for this audit.
