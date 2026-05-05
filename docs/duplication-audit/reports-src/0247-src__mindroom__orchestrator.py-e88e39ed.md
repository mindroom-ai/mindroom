## Summary

Top duplication candidates for `src/mindroom/orchestrator.py`:

1. Runtime retry/restart supervision in orchestrator overlaps with `src/mindroom/orchestration/runtime.py`, especially `_run_auxiliary_task_forever`, `_run_bot_start_retry`, and `_cancel_task_if_pending`.
2. Matrix account and room-invitation orchestration repeats lower-level Matrix/user flows already present in `src/mindroom/bot.py`, `src/mindroom/matrix/users.py`, `src/mindroom/api/schedules.py`, and `src/mindroom/api/matrix_operations.py`.
3. Runtime task completion supervision for the embedded API server resembles the `asyncio.wait(... FIRST_COMPLETED)` auxiliary-task handling in `src/mindroom/streaming_delivery.py`, but the shutdown semantics differ enough that only a small helper would be safe.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_EmbeddedApiServerContext	class	lines 128-136	related-only	"log_context embedded_api_server host port"	src/mindroom/message_target.py:29; src/mindroom/logging_config.py:249
_EmbeddedApiServerContext.log_context	method	lines 134-136	related-only	"log_context structured fields host port"	src/mindroom/message_target.py:29; src/mindroom/response_attempt.py:123
_signal_name	function	lines 139-144	none-found	"signal.Signals signal_name"	none
_raise_embedded_api_server_exit	function	lines 147-155	related-only	"fatal embedded api server exit RuntimeError"	src/mindroom/orchestrator.py:1860; src/mindroom/orchestrator.py:1877
_ConfigReloadDrainState	class	lines 159-211	none-found	"waiting_for_idle last_warning_at drain force_after_seconds"	none
_ConfigReloadDrainState.reset	method	lines 167-172	none-found	"drain_state reset waiting_for_idle"	none
_ConfigReloadDrainState.begin_wait	method	lines 174-179	none-found	"begin_wait request_started_at wait_started_at"	none
_ConfigReloadDrainState.should_reset_for_request	method	lines 181-183	none-found	"should_reset_for_request requested_at"	none
_ConfigReloadDrainState.wait_seconds	method	lines 185-189	none-found	"wait_seconds wait_started_at"	none
_ConfigReloadDrainState.should_warn	method	lines 191-203	related-only	"warning_after_seconds warning_interval_seconds last_warning_at"	src/mindroom/matrix/health.py:24; src/mindroom/orchestration/runtime.py:347
_ConfigReloadDrainState.mark_warning	method	lines 205-207	none-found	"mark_warning last_warning_at"	none
_ConfigReloadDrainState.should_force_reload	method	lines 209-211	related-only	"force_after_seconds timeout drain"	src/mindroom/approval_manager.py:680; src/mindroom/streaming_delivery.py:521
_SignalAwareUvicornServer	class	lines 214-235	none-found	"uvicorn.Server handle_exit shutdown_requested"	none
_SignalAwareUvicornServer.__init__	method	lines 217-219	none-found	"shutdown_requested uvicorn Server init"	none
_SignalAwareUvicornServer.handle_exit	method	lines 221-235	none-found	"handle_exit should_exit force_exit SIGINT"	none
MultiAgentOrchestrator	class	lines 239-1670	related-only	"orchestrator lifecycle runtime support Matrix rooms plugins"	src/mindroom/orchestration/runtime.py:1; src/mindroom/orchestration/plugin_watch.py:1
MultiAgentOrchestrator.__post_init__	method	lines 267-283	none-found	"build_owned_runtime_support KnowledgeRefreshScheduler ApprovalMatrixTransport"	none
MultiAgentOrchestrator.knowledge_refresh_scheduler	method	lines 286-288	not-a-behavior-symbol	"property knowledge_refresh_scheduler"	none
MultiAgentOrchestrator._stop_memory_auto_flush_worker	async_method	lines 290-300	related-only	"worker.stop gather task return_exceptions"	src/mindroom/knowledge/refresh_scheduler.py:117; src/mindroom/knowledge/watch.py:179
MultiAgentOrchestrator._sync_memory_auto_flush_worker	async_method	lines 302-323	related-only	"auto_flush_enabled MemoryAutoFlushWorker worker.run create_task"	src/mindroom/memory/auto_flush.py:567; src/mindroom/api/main.py:173
MultiAgentOrchestrator._reset_runtime_shutdown_event	method	lines 325-329	related-only	"asyncio.Event shutdown_event runtime"	src/mindroom/orchestrator.py:1951; src/mindroom/api/main.py:378
MultiAgentOrchestrator._capture_runtime_loop	method	lines 331-333	not-a-behavior-symbol	"capture_runtime_loop approval_transport"	none
MultiAgentOrchestrator.send_approval_notice	async_method	lines 335-349	related-only	"ApprovalMatrixTransport send_notice"	src/mindroom/approval_transport.py:1
MultiAgentOrchestrator._bind_runtime_support_services	method	lines 351-355	none-found	"event_cache_write_coordinator startup_thread_prewarm_registry bind bot"	none
MultiAgentOrchestrator._rebind_runtime_support_services	method	lines 357-360	none-found	"rebind runtime support services all bots"	none
MultiAgentOrchestrator._sync_event_cache_service	async_method	lines 362-373	related-only	"sync_owned_runtime_support event_cache service"	src/mindroom/runtime_support.py:1
MultiAgentOrchestrator._configure_approval_store_transport	method	lines 375-377	not-a-behavior-symbol	"bind_approval_runtime"	none
MultiAgentOrchestrator._close_runtime_support_services	async_method	lines 379-381	related-only	"close_owned_runtime_support"	src/mindroom/runtime_support.py:1
MultiAgentOrchestrator._ensure_user_account	async_method	lines 383-401	duplicate-found	"create_agent_user internal user account ensure matrix user"	src/mindroom/bot.py:860; src/mindroom/matrix/users.py:790
MultiAgentOrchestrator._require_config	method	lines 403-409	related-only	"Configuration not loaded RuntimeError config None"	src/mindroom/api/main.py:288; src/mindroom/api/config_lifecycle.py:795
MultiAgentOrchestrator._prepare_user_account	async_method	lines 411-423	related-only	"run_with_retry prepare user account permanent startup"	src/mindroom/orchestration/runtime.py:307
MultiAgentOrchestrator._cancel_config_reload_task	async_method	lines 425-430	related-only	"cancel_logged_task config_reload_task"	src/mindroom/orchestration/runtime.py:137
MultiAgentOrchestrator._cancel_bot_start_task	async_method	lines 432-435	related-only	"pop task cancel_task"	src/mindroom/orchestration/runtime.py:123; src/mindroom/orchestration/runtime.py:441
MultiAgentOrchestrator._cancel_bot_start_tasks	async_method	lines 437-440	related-only	"cancel all bot start tasks"	src/mindroom/background_tasks.py:91; src/mindroom/orchestration/runtime.py:444
MultiAgentOrchestrator._start_sync_task	method	lines 442-450	related-only	"existing task done create_task sync_forever_with_restart"	src/mindroom/orchestration/runtime.py:472; src/mindroom/orchestration/runtime.py:295
MultiAgentOrchestrator._bots_to_setup_after_background_start	method	lines 452-456	none-found	"router all running bots setup after background start"	none
MultiAgentOrchestrator._running_bots_for_entities	method	lines 458-465	related-only	"running bots for entities list filter bot.running"	src/mindroom/orchestration/config_updates.py:1
MultiAgentOrchestrator._try_start_bot_once	async_method	lines 467-476	related-only	"try_start PermanentMatrixStartupError classify start"	src/mindroom/bot.py:1175; src/mindroom/orchestration/runtime.py:120
MultiAgentOrchestrator._run_bot_start_retry	async_method	lines 478-522	duplicate-found	"startup retry loop retry_delay_seconds background start task"	src/mindroom/orchestration/runtime.py:307; src/mindroom/orchestration/runtime.py:472
MultiAgentOrchestrator._schedule_bot_start_retry	async_method	lines 524-531	related-only	"create_logged_task cancel existing retry_start"	src/mindroom/orchestration/runtime.py:295
MultiAgentOrchestrator.in_flight_response_count	method	lines 533-535	related-only	"in_flight_response_count sum bots"	src/mindroom/bot.py:609; src/mindroom/response_runner.py:409
MultiAgentOrchestrator.request_config_reload	method	lines 537-551	related-only	"debounced config reload create_logged_task"	src/mindroom/api/config_lifecycle.py:795; src/mindroom/api/main.py:298
MultiAgentOrchestrator._wait_for_reload_debounce	async_method	lines 553-562	related-only	"debounce sleep requested_at loop time"	src/mindroom/orchestration/plugin_watch.py:66; src/mindroom/file_watcher.py:1
MultiAgentOrchestrator._should_defer_reload_for_active_responses	async_method	lines 564-608	none-found	"active responses defer reload drain wait force"	none
MultiAgentOrchestrator._apply_queued_config_reload	async_method	lines 610-622	related-only	"update_config config changed retry if new change queued"	src/mindroom/api/config_lifecycle.py:795
MultiAgentOrchestrator._run_config_reload_loop	async_method	lines 624-663	related-only	"config reload loop debounce drain active responses"	src/mindroom/api/main.py:298; src/mindroom/api/config_lifecycle.py:795
MultiAgentOrchestrator._sync_runtime_support_services	async_method	lines 665-679	none-found	"knowledge watcher default workspaces event cache approval memory"	none
MultiAgentOrchestrator._stop_mcp_manager	async_method	lines 681-687	related-only	"bind_mcp_server_manager None manager shutdown"	src/mindroom/mcp/manager.py:1
MultiAgentOrchestrator._sync_mcp_manager	async_method	lines 689-699	related-only	"MCPServerManager sync_servers bind manager"	src/mindroom/mcp/manager.py:1
MultiAgentOrchestrator._entities_blocked_by_failed_mcp_servers	method	lines 701-712	none-found	"failed_server_ids get_entities_referencing_tools blocked_entities"	none
MultiAgentOrchestrator._retry_blocked_mcp_entities	async_method	lines 714-720	none-found	"retry failed MCP discovery blocked entities"	none
MultiAgentOrchestrator._configured_entity_names	method	lines 723-725	related-only	"router agents teams configured entity names"	src/mindroom/orchestration/config_updates.py:1
MultiAgentOrchestrator._create_managed_bot	method	lines 727-745	related-only	"create_temp_user create_bot_for_entity bind runtime support"	src/mindroom/orchestration/runtime.py:401; src/mindroom/bot.py:160
MultiAgentOrchestrator._build_hook_registry	method	lines 747-750	related-only	"load_plugins HookRegistry.from_plugins"	src/mindroom/agents.py:852; src/mindroom/tool_system/plugins.py:235
MultiAgentOrchestrator._activate_hook_registry	method	lines 752-757	related-only	"set_scheduling_hook_registry bot.hook_registry"	src/mindroom/scheduling.py:91; src/mindroom/agents.py:852
MultiAgentOrchestrator._sync_plugin_watch_roots	method	lines 759-766	related-only	"get_configured_plugin_roots sync_plugin_root_snapshots"	src/mindroom/orchestration/plugin_watch.py:106
MultiAgentOrchestrator._replace_plugin_watch_snapshots	method	lines 768-779	related-only	"replace_plugin_root_snapshots state revision"	src/mindroom/orchestration/plugin_watch.py:124
MultiAgentOrchestrator._refresh_plugin_watch_state	method	lines 781-791	related-only	"capture_plugin_root_snapshots replace watch state"	src/mindroom/orchestration/plugin_watch.py:119
MultiAgentOrchestrator.reload_plugins_now	async_method	lines 793-831	related-only	"reload_plugins recovery activate hook registry clear validation"	src/mindroom/tool_system/plugins.py:235; src/mindroom/orchestrator.py:1673
MultiAgentOrchestrator._apply_plugin_changes_for_config_update	async_method	lines 833-865	related-only	"prepare_plugin_reload apply_prepared_plugin_reload stop entities before mcp"	src/mindroom/tool_system/plugins.py:235
MultiAgentOrchestrator._start_entities_once	async_method	lines 867-911	related-only	"EntityStartResults gather try_start classify retryable permanent"	src/mindroom/orchestration/runtime.py:395; src/mindroom/bot.py:1175
MultiAgentOrchestrator._create_and_start_entities	async_method	lines 913-923	related-only	"create managed bot then start entities once"	src/mindroom/orchestrator.py:867
MultiAgentOrchestrator.initialize	async_method	lines 925-942	related-only	"load config build hook registry prepare user sync mcp create bots"	src/mindroom/orchestrator.py:1136; src/mindroom/api/main.py:246
MultiAgentOrchestrator.start	async_method	lines 944-952	none-found	"start runtime set_runtime_failed exception"	none
MultiAgentOrchestrator._start_router_bot	async_method	lines 954-977	related-only	"run_with_retry router try_start permanent startup"	src/mindroom/orchestration/runtime.py:307; src/mindroom/bot.py:1175
MultiAgentOrchestrator._start_router_bot.<locals>._start_router	nested_async_function	lines 965-969	related-only	"router try_start raise RuntimeError"	src/mindroom/orchestrator.py:467
MultiAgentOrchestrator.hook_message_sender	method	lines 979-984	related-only	"router backed hook sender"	src/mindroom/hooks.py:1
MultiAgentOrchestrator.hook_room_state_querier	method	lines 986-991	related-only	"router client build_hook_room_state_querier"	src/mindroom/hooks.py:1
MultiAgentOrchestrator.hook_room_state_putter	method	lines 993-998	related-only	"router client build_hook_room_state_putter"	src/mindroom/hooks.py:1
MultiAgentOrchestrator.hook_matrix_admin	method	lines 1000-1005	related-only	"router client build_hook_matrix_admin"	src/mindroom/hooks/matrix_admin.py:1
MultiAgentOrchestrator._log_degraded_startup	method	lines 1007-1018	none-found	"System starting in degraded mode failed_agents operational_agent_count"	none
MultiAgentOrchestrator._cleanup_stale_streams_after_restart	async_method	lines 1020-1055	related-only	"cleanup_stale_streaming_messages per bot non-critical warning"	src/mindroom/matrix/stale_stream_cleanup.py:124
MultiAgentOrchestrator._auto_resume_after_restart	async_method	lines 1057-1081	related-only	"auto_resume_interrupted_threads router client non-critical warning"	src/mindroom/matrix/stale_stream_cleanup.py:167
MultiAgentOrchestrator.handle_bot_ready	async_method	lines 1083-1085	not-a-behavior-symbol	"approval_transport handle_bot_ready"	none
MultiAgentOrchestrator._start_runtime	async_method	lines 1087-1134	related-only	"wait_for_matrix_homeserver start router start entities setup rooms sync loops"	src/mindroom/orchestration/runtime.py:347; src/mindroom/orchestrator.py:1308
MultiAgentOrchestrator._load_initial_config	async_method	lines 1136-1144	related-only	"initial config prepare user activate hooks sync mcp runtime support"	src/mindroom/orchestrator.py:925
MultiAgentOrchestrator._update_unchanged_bots	async_method	lines 1146-1155	none-found	"bot config enable_streaming hook_registry presence model info"	none
MultiAgentOrchestrator._plugin_change_paths	method	lines 1158-1165	related-only	"plugin entries path model_dump diff changed_paths"	src/mindroom/orchestration/plugin_watch.py:85
MultiAgentOrchestrator._emit_config_reloaded	async_method	lines 1167-1202	related-only	"ConfigReloadedContext emit EVENT_CONFIG_RELOADED"	src/mindroom/hooks.py:1
MultiAgentOrchestrator._remove_deleted_entities	async_method	lines 1204-1212	related-only	"cancel start task cancel sync cleanup pop entity"	src/mindroom/orchestration/runtime.py:444
MultiAgentOrchestrator._stop_entities_before_mcp_sync	async_method	lines 1214-1233	related-only	"get_entities_referencing_tools changed MCP stop_entities"	src/mindroom/orchestration/runtime.py:444
MultiAgentOrchestrator._restart_changed_entities	async_method	lines 1235-1261	related-only	"stop_entities recreate changed entities remove deleted"	src/mindroom/orchestration/runtime.py:444; src/mindroom/orchestrator.py:1204
MultiAgentOrchestrator._handle_mcp_catalog_change	async_method	lines 1263-1292	related-only	"mcp catalog change restart referencing entities schedule retry"	src/mindroom/orchestrator.py:1235
MultiAgentOrchestrator._reconcile_post_update_rooms	async_method	lines 1294-1306	related-only	"setup rooms memberships or ensure rooms root space"	src/mindroom/orchestrator.py:1415
MultiAgentOrchestrator.update_config	async_method	lines 1308-1402	related-only	"reload configuration update plan restart affected entities support services"	src/mindroom/orchestration/config_updates.py:1
MultiAgentOrchestrator._router_bot	method	lines 1404-1413	related-only	"router available router client available warnings"	src/mindroom/orchestrator.py:979; src/mindroom/orchestrator.py:986
MultiAgentOrchestrator._setup_rooms_and_memberships	async_method	lines 1415-1459	related-only	"ensure rooms root space resolve aliases ensure invitations ensure_rooms"	src/mindroom/bot_room_lifecycle.py:1; src/mindroom/matrix/rooms.py:395
MultiAgentOrchestrator._setup_rooms_and_memberships.<locals>._ensure_internal_user_memberships	nested_async_function	lines 1430-1438	related-only	"load_rooms ensure_user_in_rooms mindroom_user"	src/mindroom/matrix/rooms.py:540
MultiAgentOrchestrator._ensure_rooms_exist	async_method	lines 1461-1474	related-only	"router client ensure_all_rooms_exist log count"	src/mindroom/matrix/rooms.py:395
MultiAgentOrchestrator._ensure_root_space	async_method	lines 1476-1509	duplicate-found	"ensure_root_space invite root space users get_room_members invite_to_room"	src/mindroom/matrix/rooms.py:496; src/mindroom/matrix/client_room_admin.py:24
MultiAgentOrchestrator._invite_user_if_missing	async_method	lines 1511-1532	duplicate-found	"invite_to_room if user not current members success failure logging"	src/mindroom/matrix/client_room_admin.py:24; src/mindroom/matrix/client_room_admin.py:74
MultiAgentOrchestrator._invite_internal_user_to_rooms	async_method	lines 1534-1566	related-only	"internal user MatrixID from username invite rooms get members"	src/mindroom/matrix/rooms.py:540; src/mindroom/matrix/users.py:790
MultiAgentOrchestrator._invite_authorized_users_to_room	async_method	lines 1568-1585	related-only	"authorized users is_authorized_sender invite missing"	src/mindroom/authorization.py:1
MultiAgentOrchestrator._invite_configured_bots_to_room	async_method	lines 1587-1603	related-only	"configured bots MatrixID invite missing"	src/mindroom/entity_resolution.py:1; src/mindroom/matrix/users.py:790
MultiAgentOrchestrator._ensure_room_invitations	async_method	lines 1605-1645	related-only	"get_joined_rooms authorized users configured bots invite all rooms"	src/mindroom/matrix/client_room_admin.py:24; src/mindroom/matrix/rooms.py:395
MultiAgentOrchestrator.stop	async_method	lines 1647-1670	related-only	"shutdown cancel tasks stop bots close support reset"	src/mindroom/orchestration/runtime.py:123; src/mindroom/knowledge/watch.py:179
_recover_failed_plugin_reload	function	lines 1673-1690	related-only	"reload_plugins skip_broken_plugins deactivate_plugins recovery"	src/mindroom/tool_system/plugins.py:235
_handle_config_change	async_function	lines 1693-1696	related-only	"Configuration file changed queue hot reload"	src/mindroom/api/config_lifecycle.py:795
_watch_config_task	async_function	lines 1699-1705	duplicate-found	"watch_file config change callback"	src/mindroom/api/config_lifecycle.py:795; src/mindroom/api/main.py:298
_watch_config_task.<locals>.on_config_change	nested_async_function	lines 1702-1703	duplicate-found	"nested config change calls handler"	src/mindroom/api/config_lifecycle.py:801
_watch_skills_task	async_function	lines 1708-1719	related-only	"watch snapshot poll running sleep clear cache"	src/mindroom/orchestration/plugin_watch.py:40; src/mindroom/api/main.py:298
_run_api_server	async_function	lines 1722-1753	none-found	"initialize_api_app uvicorn embedded server SignalAware serve unexpected exit"	none
_run_auxiliary_task_forever	async_function	lines 1756-1792	duplicate-found	"restart task forever retry_delay_seconds crashed exited restarting"	src/mindroom/orchestration/runtime.py:307; src/mindroom/orchestration/runtime.py:472
_wait_for_runtime_completion	async_function	lines 1795-1826	duplicate-found	"asyncio.wait monitored tasks FIRST_COMPLETED consume completed graceful shutdown"	src/mindroom/streaming_delivery.py:594; src/mindroom/orchestration/runtime.py:241
_consume_completed_runtime_tasks	async_function	lines 1829-1857	duplicate-found	"consume completed tasks collect failures cancellation api task special case"	src/mindroom/streaming_delivery.py:548; src/mindroom/orchestration/runtime.py:241
_await_api_task_completion	async_function	lines 1860-1874	related-only	"await api task classify shutdown_requested unexpected exit"	src/mindroom/orchestrator.py:147; src/mindroom/orchestrator.py:1829
_await_api_task_graceful_shutdown	async_function	lines 1877-1915	duplicate-found	"bounded graceful shutdown loop asyncio.wait timeout first completed consume tasks"	src/mindroom/streaming_delivery.py:521; src/mindroom/background_tasks.py:131
_cancel_task_if_pending	async_function	lines 1918-1924	duplicate-found	"cancel task if pending suppress CancelledError"	src/mindroom/orchestration/runtime.py:123; src/mindroom/streaming_delivery.py:541
main	async_function	lines 1927-2023	related-only	"setup logging credentials create auxiliary tasks api orchestrator shutdown cleanup"	src/mindroom/api/main.py:378; src/mindroom/cli/main.py:1
```

## Findings

### 1. Retry and restart loops are duplicated around existing runtime helpers

`MultiAgentOrchestrator._run_bot_start_retry` (`src/mindroom/orchestrator.py:478`) and `_run_auxiliary_task_forever` (`src/mindroom/orchestrator.py:1756`) both implement long-running retry loops with capped exponential backoff through `retry_delay_seconds`.
`src/mindroom/orchestration/runtime.py:307` already owns `run_with_retry`, and `src/mindroom/orchestration/runtime.py:472` owns the similar "run operation, classify cancellation, log failure, back off, retry while allowed" flow for Matrix sync loops.

The duplication is behavioral, not literal.
The orchestrator-specific loops have extra semantics that must be preserved: `_run_bot_start_retry` classifies permanent startup failures and updates room memberships after recovery, while `_run_auxiliary_task_forever` restarts watchers after both clean exits and crashes and stops when `should_restart` becomes false.

### 2. Config watcher wrapping is duplicated with API config lifecycle watching

`_watch_config_task` and its nested `on_config_change` (`src/mindroom/orchestrator.py:1699`) wrap `file_watcher.watch_file` and translate filesystem changes into a reload callback.
`src/mindroom/api/config_lifecycle.py:795` has the same watch-file callback shape for API config cache reloads, and `src/mindroom/api/main.py:298` implements another config polling watcher for standalone API state.

The callbacks differ in return behavior and lifecycle ownership, but the watch-file-to-callback adapter is repeated.

### 3. Matrix invite-if-missing behavior is repeated around lower-level invite helpers

`_invite_user_if_missing` (`src/mindroom/orchestrator.py:1511`) checks membership, calls `invite_to_room`, logs success/failure, and mutates the current member set.
`src/mindroom/matrix/client_room_admin.py:24` owns the actual Matrix invite response classification and logging, and `src/mindroom/matrix/client_room_admin.py:74` repeats a power-user invite loop after room creation.
`_ensure_root_space` (`src/mindroom/orchestrator.py:1476`) repeats the same membership check plus invite loop for root-space users instead of using `_invite_user_if_missing`.

The differences to preserve are contextual log event names and whether the caller has already fetched `current_members`.

### 4. Matrix account creation is repeated across runtime and API surfaces

`_ensure_user_account` (`src/mindroom/orchestrator.py:383`) creates the internal MindRoom user through `create_agent_user`.
`AgentBot.ensure_user_account` (`src/mindroom/bot.py:860`) performs the same "skip if user_id exists, otherwise call `create_agent_user` and log" flow for agents.
API helpers also create or retrieve Matrix users before login at `src/mindroom/api/schedules.py:238`, `src/mindroom/api/matrix_operations.py:89`, and `src/mindroom/api/matrix_operations.py:195`.
`src/mindroom/matrix/users.py:790` still contains `_ensure_all_agent_users`, marked unused, with bulk account creation for router, agents, and teams.

The internal user is intentionally special: it uses `INTERNAL_USER_AGENT_NAME`, optional `config.mindroom_user`, and an explicit username.
Any generalization must preserve those fields and not force agent-only semantics.

### 5. Runtime task completion supervision resembles streaming auxiliary supervision

`_wait_for_runtime_completion`, `_consume_completed_runtime_tasks`, and `_await_api_task_graceful_shutdown` (`src/mindroom/orchestrator.py:1795`, `src/mindroom/orchestrator.py:1829`, `src/mindroom/orchestrator.py:1877`) monitor multiple tasks with `asyncio.wait(..., FIRST_COMPLETED)`, consume done tasks, distinguish cancellations from failures, and perform bounded shutdown.
`src/mindroom/streaming_delivery.py:548` and `src/mindroom/streaming_delivery.py:594` implement the same supervision skeleton for stream, progress, and delivery tasks.
`src/mindroom/background_tasks.py:131` has a simpler timeout-and-drain version.

The orchestrator version has API-server-specific fatal-exit classification and application shutdown semantics, so only the generic "consume completed tasks and collect failures" portion is a plausible shared helper.

### 6. Pending task cancellation duplicates the shared cancellation helper

`_cancel_task_if_pending` (`src/mindroom/orchestrator.py:1918`) is a small duplicate of `cancel_task` in `src/mindroom/orchestration/runtime.py:123`, except it skips already completed tasks and only suppresses `CancelledError`.
Similar pending-task cancellation appears in `src/mindroom/streaming_delivery.py:541`.

## Proposed Generalization

No broad refactor recommended.
If this file is revisited, the lowest-risk extraction would be:

1. Add a tiny restart-loop helper to `src/mindroom/orchestration/runtime.py` that accepts an operation, restart predicate, task name, and backoff constants.
2. Replace only `_run_auxiliary_task_forever` first, because it has the fewest orchestrator-specific side effects.
3. Add a Matrix helper near `src/mindroom/matrix/client_room_admin.py` for "invite if missing and update provided member set", parameterized with success/failure log messages or event names.
4. Replace `_ensure_root_space`'s local invite loop with `_invite_user_if_missing` before considering a cross-module helper.
5. Leave the API-server task supervision local unless another runtime surface needs the same fatal-exit classification.

## Risk/Tests

Primary risks are lifecycle regressions: retries could stop too early, shutdown could mask API-server exits, and Matrix invite reconciliation could emit different logs or skip member-set updates.
Focused tests should cover `_run_auxiliary_task_forever` restart/stop behavior, `_wait_for_runtime_completion` when the API task exits unexpectedly versus after shutdown, and invitation helpers when a user is already present, invite succeeds, and invite fails.
No production code was edited.
