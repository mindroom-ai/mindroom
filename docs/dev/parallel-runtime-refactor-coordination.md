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
| `src/mindroom/api/main.py` | claimed | codex / 2026-03-14 | API app runtime scoping and request/runtime consistency. |
| `src/mindroom/api/openai_compat.py` | claimed | codex / 2026-03-14 | OpenAI-compatible runtime propagation and auth/runtime tests. |
| `src/mindroom/api/credentials.py` | claimed | codex / 2026-03-14 | Explicit runtime-based dashboard execution identity. |
| `src/mindroom/api/google_integration.py` | claimed | codex / 2026-03-14 | Remove ambient env reads and use explicit runtime. |
| `src/mindroom/api/integrations.py` | claimed | codex / 2026-03-14 | Remove ambient integration env reads. |
| `src/mindroom/bot.py` | claimed | codex / 2026-03-14 | Tool execution identity cleanup. |
| `src/mindroom/commands/handler.py` | claimed | codex / 2026-03-14 | Explicit runtime in skill execution identity. |
| `tests/conftest.py` | claimed | codex / 2026-03-14 | Shared runtime-binding fixtures and reset behavior. |

## Reserved Files

| File | Status | Owner | Notes |
| --- | --- | --- | --- |
| `tests/test_agents.py` | done | codex / 2026-03-14 | Aligned the local test config helper with the shared explicit-runtime binding fixture. |

## Claimable Test Files

These are the known test files that still need coordinated runtime-refactor work.

| File | Status | Owner | Notes |
| --- | --- | --- | --- |
| `tests/test_openai_compat.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the file now passes cleanly with the explicit runtime setup. |
| `tests/api/test_api.py` | done | codex / 2026-03-14 | Dashboard API tests now pass with explicit runtime injection and the fixed app-state runtime lookup. |
| `tests/test_memory_auto_flush.py` | done | codex / 2026-03-14 | Replaced repo-config loading with a self-contained runtime-bound file-memory config aligned to the test storage root. |
| `tests/test_memory_mem0_backend.py` | done | codex / 2026-03-14 | Bound a self-contained general/calculator config to explicit runtime paths that match the test storage root. |
| `tests/test_memory_tools.py` | done | codex / 2026-03-14 | Bound a self-contained explicit runtime config and finished runtime-bearing mock expectations. |
| `tests/test_multi_agent_e2e.py` | regressed |  | Was done; now 4 failures (env_value). Broken by concurrent changes. |
| `tests/test_multi_agent_bot.py` | regressed |  | Was done; now ~30 failures (env_value). Broken by concurrent changes. |
| `tests/test_matrix_agent_manager.py` | done | codex / 2026-03-14 | Replaced repo-config dependency with explicit runtime-bound agent/team config and fixed partial-failure expectations. |
| `tests/test_bot_scheduling.py` | regressed |  | Was done; now 15 ERRORs (env_value). Broken by concurrent changes. |
| `tests/test_cli_config.py` | done | claude-opus / 2026-03-14 | Fixed runtime_storage_root → runtime_paths.storage_root. |
| `tests/test_error_handling_in_callbacks.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the callback error tests already pass with explicit runtime paths. |
| `tests/test_homeassistant_tools.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the Home Assistant tool tests already pass with explicit RuntimePaths. |
| `tests/test_interactive.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_interactive_thread_fix.py` | claimed | codex / 2026-03-14 | Fix stale `bind_runtime_paths(Config.from_yaml(), tmp_path)` calls with explicit RuntimePaths. |
| `tests/test_large_messages_integration.py` | not_needed |  | Already passes. |
| `tests/test_matrix_message_tool.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_matrix_identity.py` | claimed | codex / 2026-03-14 | Fix stale `_bind_runtime_paths(..., tmp_path)` helper to pass explicit RuntimePaths. |
| `tests/test_matrix_spaces.py` | done | codex / 2026-03-14 | Matrix Space orchestration and persistence tests pass with the current explicit-runtime contract. |
| `tests/test_mention_exclusion.py` | not_needed |  | Already passes. |
| `tests/test_multiple_edits.py` | done | codex / 2026-03-14 | Bound the bot to the config's explicit runtime_paths in the multiple-edit regression test. |
| `tests/test_plugins.py` | claimed | codex / 2026-03-14 | Reproduce and fix the current env_value/runtime regression in the plugin tests. |
| `tests/test_presence_based_streaming.py` | not_needed |  | Already passes. |
| `tests/test_response_tracking_regression.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_restore_dedup.py` | not_needed |  | Already passes. |
| `tests/test_room_invites.py` | claimed | codex / 2026-03-14 | Fix stale `bind_runtime_paths(..., tmp_path)` calls with explicit RuntimePaths. |
| `tests/test_routing.py` | regressed |  | Was done; now 5 FAILED + 14 ERRORs (env_value). Broken by concurrent changes. |
| `tests/test_routing_integration.py` | done | codex / 2026-03-14 | Re-ran on the current tree; the routing integration test already passes with the explicit RuntimePaths construction. |
| `tests/test_routing_regression.py` | regressed |  | Was done; now 6 failures (env_value). Broken by concurrent changes. |
| `tests/test_sandbox_proxy.py` | not_needed |  | Already passes. |
| `tests/test_schedule_agent_validation.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_config_discovery.py` | done | codex / 2026-03-14 | Updated stale runtime-discovery tests to the explicit-runtime contract and current `set_runtime_paths` compatibility boundary. |
| `tests/test_agent_datetime_context.py` | regressed |  | Was done; now 2 failures (bind_runtime_paths missing arg). Broken by concurrent changes. |
| `tests/test_agent_order_preservation.py` | regressed |  | Was done; now 8 ERRORs (bind_runtime_paths missing arg). Broken by concurrent changes. |
| `tests/test_agent_response_logic.py` | regressed |  | Was done; now 4 ERRORs (bind_runtime_paths missing arg). Broken by concurrent changes. |
| `tests/test_agno_history.py` | done | codex / 2026-03-14 | Fixed helper-level runtime binding to pass explicit RuntimePaths into test configs. |
| `tests/test_authorization_config_update.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_scheduler_tool.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_agent_mentions.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_bot_threading.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_handler_thread.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_streaming_behavior.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_team_media_fallback.py` | regressed |  | Was done; now 2 failures (AssertionError). Broken by concurrent changes. |
| `tests/test_voice_handler.py` | regressed |  | Was done; now 3 failures (missing attribute, KeyError, timeout). Broken by concurrent changes. |
| `tests/api/test_knowledge_api.py` | done | codex / 2026-03-14 | Bound the API app to explicit RuntimePaths and updated stale `Config.from_yaml` patches to the current `_load_runtime_config` contract. |
| `tests/api/test_credentials_api.py` | done | codex / 2026-03-14 | Bound the shared API app to explicit RuntimePaths and updated the tenant-ID test to mutate app runtime instead of relying on ambient env. |
| `tests/test_config_manager_consolidated.py` | unclaimed |  | 26 failures. |
| `tests/test_config_reload.py` | regressed |  | Was done; now 5 ERRORs (NoneType env_value). Broken by concurrent changes. |
| `tests/test_edit_response_regeneration.py` | done | codex / 2026-03-14 | Bound the file to an explicit example.com runtime context so agent-edit detection, voice relays, and auth assertions match the runtime-aware bot contract. |
| `tests/test_ai_error_message_display.py` | done | codex / 2026-03-14 | Replaced stale bot/config doubles with runtime-bound test setup. |
| `tests/test_dynamic_config_update.py` | done | codex / 2026-03-14 | Patched the tests to mock `mindroom.orchestrator.load_config(...)`, which is the current update_config entrypoint. |
| `tests/test_dm_room_preservation.py` | done | codex / 2026-03-14 | Updated local runtime binding helper and stale get_configured_bots_for_room mock signature for explicit-runtime cleanup tests. |
| `tests/test_ai_user_id.py` | done | codex / 2026-03-14 | Updated the remaining direct runtime binding calls and bot doubles to carry explicit RuntimePaths. |
| `tests/test_edit_after_restart.py` | done | codex / 2026-03-14 | Tightened the mocked config to implement `get_ids(runtime_paths)` like the production contract. |
| `tests/test_matrix_room_access.py` | done | codex / 2026-03-14 | Updated stale room-access assertions and mock signatures for explicit-runtime calls. |
| `tests/test_memory_file_backend.py` | done | codex / 2026-03-14 | Replaced repo-config loading with a self-contained runtime-bound file-memory config that matches the test storage root. |
| `tests/test_memory_facade.py` | done | codex / 2026-03-14 | Replaced repo-config loading with a self-contained runtime-bound memory config that matches the test storage root. |
| `tests/test_memory_integration.py` | done | codex / 2026-03-14 | Updated stale prompt-builder expectations to include runtime_paths and aligned the test agent with the current config. |
| `tests/test_delegate_tools.py` | done | claude-opus / 2026-03-14 | Fixed _bind_runtime_paths wrapper to create RuntimePaths, captured bound config return values, and added runtime_paths as 3rd positional arg in create_agent assertions. |
| `tests/test_router_rooms.py` | done | codex / 2026-03-14 | Updated local runtime binding helper and stale restore_scheduled_tasks mock signature for explicit-runtime tests. |
| `tests/test_extra_kwargs.py` | done | claude-opus / 2026-03-14 | Updated _config_with_runtime_paths to return (Config, RuntimePaths) tuple and pass runtime_paths as 2nd arg to get_model_instance. |
| `tests/test_scheduled_task_restoration.py` | regressed |  | Was done; now 5 failures (env_value). Broken by concurrent changes. |
| `tests/test_gemini_integration.py` | done | codex / 2026-03-14 | Updated stale get_model_instance call sites to pass explicit RuntimePaths and aligned the API-key assertion with the new signature. |
| `tests/test_multi_agent_bot.py` | regressed |  | Was done; now ~30 failures (env_value). Massively regressed by concurrent changes. |
| `tests/test_skills.py` | done | codex / 2026-03-14 | Updated the stale `get_tool_by_name` test doubles for the new positional `runtime_paths` argument. |
| `tests/test_self_config.py` | claimed | claude-opus / 2026-03-14 | Fix runtime-path binding for self_config tests. 8 failures. |
| `tests/test_skip_mentions.py` | done | codex / 2026-03-14 | Added explicit runtime_paths to the bot/config mocks used by the skip-mentions tests. |
| `tests/test_subagents.py` | done | codex / 2026-03-14 | Updated stale get_entity_thread_mode mock signature and explicit runtime-path assertion in the target-room dispatch test. |
| `tests/test_team_mode_decision.py` | regressed |  | Was done; now 12 ERRORs (env_value). Broken by concurrent changes. |
| `tests/test_sync_task_cancellation.py` | regressed |  | Was done; now 1 failure (create_bot_for_entity not called). Broken by concurrent changes. |
| `tests/test_tool_config_sync.py` | regressed |  | Was done; now 1 failure (config_path ConfigField validation). Broken by concurrent changes. |
| `tests/test_voice_command_processing.py` | done | codex / 2026-03-14 | Bound explicit RuntimePaths through the shared helper and removed the last unbound mock config from the bot setup. |
| `tests/test_voice_thread_agent_response.py` | done | codex / 2026-03-14 | Bound the bot fixture to explicit RuntimePaths and updated the stale extract-agent test helper signature. |
| `tests/test_config_discovery.py` (regressions) | unclaimed |  | 7 new failures after prior fix: 6 in TestResolveConfigRelativePath + 1 in TestRuntimeGuardrails. |
| `tests/test_preformed_team_routing.py` | done | codex / 2026-03-14 | Switched the local bind helper to explicit RuntimePaths so the file no longer passes bare paths into runtime-bound config validation. |
| `tests/test_stop_emoji_reuse.py` | done | codex / 2026-03-14 | Bound the remaining reply-permission test to explicit RuntimePaths via the shared orchestrator helper. |
| `tests/test_team_collaboration.py` | done | codex / 2026-03-14 | Verified the current helper-based explicit RuntimePaths cleanup; the file passes on the current tree. |
| `tests/test_team_scheduler_context.py` | done | codex / 2026-03-14 | Updated the team bot factory to bind explicit RuntimePaths before constructing the scheduler-context tests. |
| `tests/test_streaming_edits.py` | done | codex / 2026-03-14 | Tightened the bot setup helper to build explicit RuntimePaths once and reuse them instead of passing bare paths into runtime-bound config binding. |
| `tests/test_streaming_e2e.py` | done | codex / 2026-03-14 | Replaced the stale `bind_runtime_paths(config, tmp_path)` shorthand with an explicit orchestrator RuntimePaths object. |
| `tests/test_thread_mode.py` | unclaimed |  | 7 FAILED + 7 ERRORs (NoneType env_value). New — never tracked. |
| `tests/test_threading_error.py` | unclaimed |  | 4 FAILED + 14 ERRORs (env_value). New — never tracked. |
| `tests/test_unknown_command_response.py` | unclaimed |  | 3 failures (env_value). New — never tracked. |
| `tests/test_workflow_scheduling.py` | unclaimed |  | 5 FAILED + 6 ERRORs (env_value). New — never tracked. |
| `tests/test_team_invitations.py` | claimed | codex / 2026-03-14 | Fix stale `_bind_runtime_paths(..., tmp_path)` helper to pass explicit RuntimePaths. |
| `tests/test_router_configured_agents.py` | unclaimed |  | 4 ERRORs (env_value). New — never tracked. |

## Notes

If a test file only needs a mechanical `runtime_paths` argument update, claim it alone and finish it in one small commit.

If a test file requires changing one shared fixture or one shared source file, claim that shared file first and note the dependency here before editing.

If a change requires touching both a source file and many tests, the source file owner should land the source change first, then release the dependent tests for parallel pickup.
