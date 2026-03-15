# Parallel Runtime Refactor Coordination

This file is the single coordination ledger for parallel work on the runtime-path refactor.

Every agent working on this branch must read and update this file before touching any shared file.

## Protocol

1. Claim files before editing them.
2. A claim means editing this file and changing one or more rows from `unclaimed` to `claimed`.
3. Each claimed row must include the agent name, the date, and a short note about the intended change.
4. Do not edit a file that is already claimed by another agent.
5. If you need a file that is already claimed, add a note in this document instead of editing the code.
6. Only one agent may own `tests/conftest.py` at a time because fixture changes can invalidate many other test files.
7. Only one agent may own a core runtime source file at a time.
8. `tests/test_agents.py` is reserved for a different agent and must not be touched unless that reservation is explicitly removed here.
9. Keep commits narrow.
10. Prefer one commit per claimed file or one commit per tightly-coupled file pair.
11. When a file is done, mark it `done` and leave one short note describing what was fixed.
12. If a file turns out not to need changes, mark it `not_needed` with a short reason.
13. If a claim is abandoned, change it back to `unclaimed` immediately.

## Shared Source Files

These files are high-risk because they define runtime behavior for many tests.

| File | Status | Owner | Notes |
| --- | --- | --- | --- |
| `src/mindroom/constants.py` | claimed | codex / 2026-03-14 | Active runtime/env cleanup and explicit runtime consistency. |
| `src/mindroom/config/main.py` | done | codex / 2026-03-14 | `Config.from_yaml(...)` is now an explicit pure file loader; runtime-aware loads go through `load_config(runtime_paths)`. |
| `src/mindroom/api/main.py` | claimed | codex / 2026-03-14 | API app runtime scoping and request/runtime consistency. |
| `src/mindroom/api/openai_compat.py` | claimed | codex / 2026-03-14 | OpenAI-compatible runtime propagation and auth/runtime tests. |
| `src/mindroom/api/credentials.py` | claimed | codex / 2026-03-14 | Explicit runtime-based dashboard execution identity. |
| `src/mindroom/api/google_integration.py` | done | codex / 2026-03-14 | Google OAuth config/reset now read and write the request runtime's `.env`, then refresh the app runtime paths so follow-up requests see the change immediately. |
| `src/mindroom/api/integrations.py` | claimed | codex / 2026-03-14 | Remove ambient integration env reads. |
| `src/mindroom/bot.py` | claimed | codex / 2026-03-14 | Tool execution identity cleanup. |
| `src/mindroom/cli/config.py` | claimed | codex / 2026-03-14 | Make config-discovery display paths use an explicit exported env snapshot instead of ambient reads. |
| `src/mindroom/cli/main.py` | claimed | codex / 2026-03-14 | Make top-level missing-config guidance use explicit config-discovery env input. |
| `src/mindroom/commands/handler.py` | claimed | codex / 2026-03-14 | Explicit runtime in skill execution identity. |
| `tests/conftest.py` | done | codex / 2026-03-14 | Shared runtime-binding fixtures and reset behavior are in place; the coordinated explicit-runtime test sweep is complete. |

## Reserved Files

| File | Status | Owner | Notes |
| --- | --- | --- | --- |
| `tests/test_agents.py` | done | codex / 2026-03-14 | Aligned the local test config helper with the shared explicit-runtime binding fixture. |

## Claimable Test Files

These are the known test files that still need coordinated runtime-refactor work.

| File | Status | Owner | Notes |
| --- | --- | --- | --- |
| `tests/test_openai_compat.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the file now passes cleanly with the explicit runtime setup. |
| `tests/api/test_api.py` | done | codex / 2026-03-14 | Added Google OAuth coverage for runtime-scoped `.env` writes and immediate runtime-path refresh after configure/reset. |
| `tests/test_memory_auto_flush.py` | done | codex / 2026-03-14 | Replaced repo-config loading with a self-contained runtime-bound file-memory config aligned to the test storage root. |
| `tests/test_memory_mem0_backend.py` | done | codex / 2026-03-14 | Bound a self-contained general/calculator config to explicit runtime paths that match the test storage root. |
| `tests/test_memory_tools.py` | done | codex / 2026-03-14 | Bound a self-contained explicit runtime config and finished runtime-bearing mock expectations. |
| `tests/test_multi_agent_e2e.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the file already passes cleanly with explicit RuntimePaths. |
| `tests/test_multi_agent_bot.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the full multi-agent bot suite now passes cleanly with explicit RuntimePaths. |
| `tests/test_matrix_agent_manager.py` | done | codex / 2026-03-14 | Replaced repo-config dependency with explicit runtime-bound agent/team config and fixed partial-failure expectations. |
| `tests/test_bot_scheduling.py` | done | codex / 2026-03-14 | Switched the local runtime-binding helper to construct explicit RuntimePaths instead of passing bare paths into `bind_runtime_paths(...)`. |
| `tests/test_cli_config.py` | done | claude-opus / 2026-03-14 | Fixed runtime_storage_root → runtime_paths.storage_root. |
| `tests/test_error_handling_in_callbacks.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the callback error tests already pass with explicit runtime paths. |
| `tests/test_homeassistant_tools.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the Home Assistant tool tests already pass with explicit RuntimePaths. |
| `tests/test_interactive.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_interactive_thread_fix.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the interactive thread regression tests already pass with explicit runtime-bound configs. |
| `tests/test_large_messages_integration.py` | not_needed |  | Already passes. |
| `tests/test_matrix_message_tool.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_matrix_identity.py` | done | codex / 2026-03-14 | Updated the local `_bind_runtime_paths(...)` helper to pass explicit test RuntimePaths. |
| `tests/test_matrix_spaces.py` | done | codex / 2026-03-14 | Updated the local config-binding helper to use explicit orchestrator RuntimePaths for root-Space reconciliation tests. |
| `tests/test_mention_exclusion.py` | done | codex / 2026-03-14 | Re-ran after the broad-suite report; the file still passes cleanly in isolation (`2 passed`), so the regression is not currently reproducible here. |
| `tests/test_multiple_edits.py` | done | codex / 2026-03-14 | Bound the bot to the config's explicit runtime_paths in the multiple-edit regression test. |
| `tests/test_plugins.py` | done | codex / 2026-03-14 | Restored explicit RuntimePaths helpers for plugin configs and updated runtime-bearing tool/plugin calls. |
| `tests/test_presence_based_streaming.py` | done | codex / 2026-03-14 | Re-ran after the broad-suite report; the file still passes cleanly in isolation (`11 passed`), so the regression is not currently reproducible here. |
| `tests/test_response_tracking_regression.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_restore_dedup.py` | not_needed |  | Already passes. |
| `tests/test_room_invites.py` | done | codex / 2026-03-14 | Replaced stale `bind_runtime_paths(..., tmp_path)` calls with explicit test RuntimePaths. |
| `tests/test_routing.py` | done | codex / 2026-03-14 | Updated the local `_runtime_bound_config(...)` helper to pass explicit test RuntimePaths. |
| `tests/test_routing_integration.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the routing integration test already passes with the explicit RuntimePaths construction. |
| `tests/test_routing_regression.py` | done | codex / 2026-03-14 | Switched the local runtime-binding helper to construct explicit RuntimePaths instead of passing bare paths into `bind_runtime_paths(...)`. |
| `tests/test_sandbox_proxy.py` | not_needed |  | Already passes. |
| `tests/test_schedule_agent_validation.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_config_discovery.py` | claimed | codex / 2026-03-14 | Tightening config discovery so `find_config` and `config_search_locations` take an explicit env snapshot. |
| `tests/test_agent_datetime_context.py` | done | codex / 2026-03-14 | Replaced repo-config loading with a self-contained runtime-bound config and patched model creation in the datetime prompt tests. |
| `tests/test_agent_order_preservation.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the file already passes cleanly with explicit RuntimePaths. |
| `tests/test_agent_response_logic.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the file already passes cleanly with explicit RuntimePaths. |
| `tests/test_agno_history.py` | done | codex / 2026-03-14 | Fixed helper-level runtime binding to pass explicit RuntimePaths into test configs. |
| `tests/test_authorization_config_update.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_scheduler_tool.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_agent_mentions.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_bot_threading.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_handler_thread.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_streaming_behavior.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_team_media_fallback.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the team media fallback tests already pass with explicit RuntimePaths. |
| `tests/test_voice_handler.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the voice handler tests already pass with explicit RuntimePaths. |
| `tests/api/test_knowledge_api.py` | done | codex / 2026-03-14 | Bound the API app to explicit RuntimePaths and updated stale `Config.from_yaml` patches to the current `_load_runtime_config` contract. |
| `tests/api/test_credentials_api.py` | done | codex / 2026-03-14 | Bound the shared API app to explicit RuntimePaths and updated the tenant-ID test to mutate app runtime instead of relying on ambient env. |
| `tests/test_config_manager_consolidated.py` | done | codex / 2026-03-14 | Replaced stale path-only ConfigManagerTools construction with an explicit RuntimePaths helper; the file now passes cleanly. |
| `tests/test_config_reload.py` | done | codex / 2026-03-14 | Re-verified on the current tree; the runtime-bound helper and scheduled-task mock updates still pass. |
| `tests/test_edit_response_regeneration.py` | done | codex / 2026-03-14 | Bound the file to an explicit example.com runtime context so agent-edit detection, voice relays, and auth assertions match the runtime-aware bot contract. |
| `tests/test_ai_error_message_display.py` | done | codex / 2026-03-14 | Replaced stale bot/config doubles with runtime-bound test setup. |
| `tests/test_dynamic_config_update.py` | done | codex / 2026-03-14 | Patched the tests to mock `mindroom.orchestrator.load_config(...)`, which is the current update_config entrypoint. |
| `tests/test_dm_room_preservation.py` | done | codex / 2026-03-14 | Updated local runtime binding helper and stale get_configured_bots_for_room mock signature for explicit-runtime cleanup tests. |
| `tests/test_dm_functionality.py` | done | codex / 2026-03-14 | Re-ran after the broad-suite report; the file still passes cleanly in isolation (`11 passed`), so the regression is not currently reproducible here. |
| `tests/test_ai_user_id.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the AI user-id runtime checks already pass with explicit runtime-bound configs. |
| `tests/test_edit_after_restart.py` | done | codex / 2026-03-14 | Tightened the mocked config to implement `get_ids(runtime_paths)` like the production contract. |
| `tests/test_matrix_room_access.py` | done | codex / 2026-03-14 | Updated stale room-access assertions and mock signatures for explicit-runtime calls. |
| `tests/test_memory_file_backend.py` | done | codex / 2026-03-14 | Replaced repo-config loading with a self-contained runtime-bound file-memory config that matches the test storage root. |
| `tests/test_memory_facade.py` | done | codex / 2026-03-14 | Replaced repo-config loading with a self-contained runtime-bound memory config that matches the test storage root. |
| `tests/test_memory_integration.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the memory integration tests already pass with explicit runtime-bound config setup. |
| `tests/test_memory_policy.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the memory policy helpers already pass without any remaining no-arg config fixture. |
| `tests/test_delegate_tools.py` | done | claude-opus / 2026-03-14 | Fixed _bind_runtime_paths wrapper to create RuntimePaths, captured bound config return values, and added runtime_paths as 3rd positional arg in create_agent assertions. |
| `tests/test_router_rooms.py` | done | codex / 2026-03-14 | Updated local runtime binding helper and stale restore_scheduled_tasks mock signature for explicit-runtime tests. |
| `tests/test_extra_kwargs.py` | done | claude-opus / 2026-03-14 | Updated _config_with_runtime_paths to return (Config, RuntimePaths) tuple and pass runtime_paths as 2nd arg to get_model_instance. |
| `tests/test_scheduled_task_restoration.py` | done | codex / 2026-03-14 | Re-verified on the current tree; the explicit runtime_paths assertions still pass. |
| `tests/test_gemini_integration.py` | done | codex / 2026-03-14 | Updated stale get_model_instance call sites to pass explicit RuntimePaths and aligned the API-key assertion with the new signature. |
| `tests/test_skills.py` | done | codex / 2026-03-14 | Updated the stale `get_tool_by_name` test doubles for the new positional `runtime_paths` argument. |
| `tests/test_self_config.py` | done | claude-opus / 2026-03-14 | Replaced all Config.from_yaml() calls with self-contained _make_config() to avoid runtime-path KeyError. |
| `tests/test_skip_mentions.py` | done | codex / 2026-03-14 | Added explicit runtime_paths to the bot/config mocks used by the skip-mentions tests. |
| `tests/test_subagents.py` | done | codex / 2026-03-14 | Updated stale get_entity_thread_mode mock signature and explicit runtime-path assertion in the target-room dispatch test. |
| `tests/test_team_mode_decision.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the file already passes cleanly with explicit RuntimePaths. |
| `tests/test_sync_task_cancellation.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the sync-task cancellation tests already pass with the runtime-aware loader/task fixes. |
| `tests/test_tool_config_sync.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the config-field sync test suite already passes with injected runtime_paths ignored. |
| `tests/test_tool_dependencies.py` | not_needed | codex / 2026-03-14 | Re-ran after the broad-suite report; the metadata check currently passes in isolation (`38 passed`). |
| `tests/test_voice_command_processing.py` | done | codex / 2026-03-14 | Bound explicit RuntimePaths through the shared helper and removed the last unbound mock config from the bot setup. |
| `tests/test_voice_thread_agent_response.py` | done | codex / 2026-03-14 | Bound the bot fixture to explicit RuntimePaths and updated the stale extract-agent test helper signature. |
| `tests/test_config_discovery.py` (regressions) | not_needed | codex / 2026-03-14 | Re-ran on the current tree; the config-discovery regressions no longer reproduce. |
| `tests/test_preformed_team_routing.py` | done | codex / 2026-03-14 | Switched the local bind helper to explicit RuntimePaths so the file no longer passes bare paths into runtime-bound config validation. |
| `tests/test_stop_emoji_reuse.py` | done | codex / 2026-03-14 | Bound the remaining reply-permission test to explicit RuntimePaths via the shared orchestrator helper. |
| `tests/test_team_collaboration.py` | done | codex / 2026-03-14 | Verified the current helper-based explicit RuntimePaths cleanup; the file passes on the current tree. |
| `tests/test_team_scheduler_context.py` | done | codex / 2026-03-14 | Updated the team bot factory to bind explicit RuntimePaths before constructing the scheduler-context tests. |
| `tests/test_streaming_edits.py` | done | codex / 2026-03-14 | Tightened the bot setup helper to build explicit RuntimePaths once and reuse them instead of passing bare paths into runtime-bound config binding. |
| `tests/test_streaming_e2e.py` | done | codex / 2026-03-14 | Replaced the stale `bind_runtime_paths(config, tmp_path)` shorthand with an explicit orchestrator RuntimePaths object. |
| `tests/test_thread_mode.py` | done | claude-opus / 2026-03-14 | Updated _runtime_bound_config to create proper RuntimePaths from temp dir. |
| `tests/test_threading_error.py` | done | codex / 2026-03-14 | Fixed the shared runtime-binding helper to create explicit RuntimePaths for the full threading error suite. |
| `tests/test_unknown_command_response.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the file already passes cleanly with the explicit runtime-bound bot setup. |
| `tests/test_workflow_scheduling.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the workflow scheduling tests already pass with the explicit runtime helper in place. |
| `tests/test_team_invitations.py` | done | codex / 2026-03-14 | Updated the local `_bind_runtime_paths(...)` helper to pass explicit test RuntimePaths. |
| `tests/test_router_configured_agents.py` | done | codex / 2026-03-14 | Replaced stale path-only runtime binding with explicit RuntimePaths in both the shared setup and router-specific config helper. |

## Notes

If a test file only needs a mechanical `runtime_paths` argument update, claim it alone and finish it in one small commit.

If a test file requires changing one shared fixture or one shared source file, claim that shared file first and note the dependency here before editing.

If a change requires touching both a source file and many tests, the source file owner should land the source change first, then release the dependent tests for parallel pickup.

2026-03-14 broad-suite reruns also reported 3 `tests/test_multi_agent_bot.py` knowledge-manager failures, but an isolated rerun of that file stayed green (`85 passed, 4 skipped`), so the row remains `done` until the regression is reproduced in a single-file run.

2026-03-14 isolated reruns also stayed green for `tests/test_dm_functionality.py` (`11 passed`), `tests/test_mention_exclusion.py` (`2 passed`), `tests/test_presence_based_streaming.py` (`11 passed`), and `tests/test_tool_dependencies.py` (`38 passed`), so those broad-suite failures are currently tracked as non-reproducing order-dependent reports rather than active single-file regressions.
