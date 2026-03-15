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
| `tests/test_openai_compat.py` | done | codex / 2026-03-14 | FastAPI test apps now carry explicit runtime paths and runtime-backed auth settings. |
| `tests/api/test_api.py` | done | codex / 2026-03-14 | Dashboard API tests now pass with explicit runtime injection and the fixed app-state runtime lookup. |
| `tests/test_memory_auto_flush.py` | done | codex / 2026-03-14 | Replaced repo-config loading with a self-contained runtime-bound file-memory config aligned to the test storage root. |
| `tests/test_memory_mem0_backend.py` | done | codex / 2026-03-14 | Bound a self-contained general/calculator config to explicit runtime paths that match the test storage root. |
| `tests/test_memory_tools.py` | done | codex / 2026-03-14 | Bound a self-contained explicit runtime config and finished runtime-bearing mock expectations. |
| `tests/test_multi_agent_e2e.py` | done | codex / 2026-03-14 | Updated e2e assertions to use the config's bound `runtime_paths.storage_root`. |
| `tests/test_multi_agent_bot.py` | done | claude-opus / 2026-03-14 | Fixed config.ids Pydantic assignment via __dict__, Config.from_yaml→load_config patches, storage_root assertions, and mock orchestrator.runtime_paths. |
| `tests/test_matrix_agent_manager.py` | done | codex / 2026-03-14 | Replaced repo-config dependency with explicit runtime-bound agent/team config and fixed partial-failure expectations. |
| `tests/test_bot_scheduling.py` | done | claude-opus / 2026-03-14 | Fixed config.ids Pydantic assignment and extract_agent_name/schedule mock signatures. |
| `tests/test_cli_config.py` | done | claude-opus / 2026-03-14 | Fixed runtime_storage_root → runtime_paths.storage_root. |
| `tests/test_error_handling_in_callbacks.py` | not_needed |  | Already passes. |
| `tests/test_homeassistant_tools.py` | not_needed |  | Already passes. |
| `tests/test_interactive.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_interactive_thread_fix.py` | not_needed |  | Already passes. |
| `tests/test_large_messages_integration.py` | not_needed |  | Already passes. |
| `tests/test_matrix_message_tool.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_matrix_spaces.py` | done | codex / 2026-03-14 | Matrix Space orchestration and persistence tests pass with the current explicit-runtime contract. |
| `tests/test_mention_exclusion.py` | not_needed |  | Already passes. |
| `tests/test_multiple_edits.py` | not_needed |  | Already passes. |
| `tests/test_plugins.py` | not_needed |  | Already passes. |
| `tests/test_presence_based_streaming.py` | not_needed |  | Already passes. |
| `tests/test_response_tracking_regression.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_restore_dedup.py` | not_needed |  | Already passes. |
| `tests/test_room_invites.py` | not_needed |  | Already passes. |
| `tests/test_routing.py` | done | codex / 2026-03-14 | Fixed async/runtime-path updates and replaced repo-config-dependent describe_agent tests with deterministic in-test config. |
| `tests/test_routing_integration.py` | not_needed |  | Already passes. |
| `tests/test_routing_regression.py` | done | codex / 2026-03-14 | Fixed MatrixID.agent_name() expectations to use the config's bound runtime_paths. |
| `tests/test_sandbox_proxy.py` | not_needed |  | Already passes. |
| `tests/test_schedule_agent_validation.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_config_discovery.py` | done | codex / 2026-03-14 | Updated stale runtime-discovery tests to the explicit-runtime contract and current `set_runtime_paths` compatibility boundary. |
| `tests/test_agent_datetime_context.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_agent_order_preservation.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_agent_response_logic.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_agno_history.py` | done | codex / 2026-03-14 | Fixed helper-level runtime binding to pass explicit RuntimePaths into test configs. |
| `tests/test_authorization_config_update.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_scheduler_tool.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_agent_mentions.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_bot_threading.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_voice_handler_thread.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_streaming_behavior.py` | done | codex / 2026-03-14 | Added explicit test RuntimePaths instead of implicit binding. |
| `tests/test_team_media_fallback.py` | claimed | codex / 2026-03-14 | Replace implicit runtime binding with explicit test RuntimePaths. |
| `tests/test_voice_handler.py` | claimed | codex / 2026-03-14 | Replace implicit runtime binding with explicit test RuntimePaths. |
| `tests/api/test_knowledge_api.py` | unclaimed |  | 7 failures. |
| `tests/api/test_credentials_api.py` | unclaimed |  | 24 failures. |
| `tests/test_config_manager_consolidated.py` | unclaimed |  | 26 failures. |
| `tests/test_config_reload.py` | unclaimed |  | 2 failures. |
| `tests/test_edit_response_regeneration.py` | unclaimed |  | 4 failures. |
| `tests/test_ai_error_message_display.py` | unclaimed |  | 4 failures. |
| `tests/test_dynamic_config_update.py` | unclaimed |  | 6 failures. |
| `tests/test_dm_room_preservation.py` | done | codex / 2026-03-14 | Updated local runtime binding helper and stale get_configured_bots_for_room mock signature for explicit-runtime cleanup tests. |
| `tests/test_ai_user_id.py` | unclaimed |  | 2 failures. |
| `tests/test_edit_after_restart.py` | unclaimed |  | 2 failures. |
| `tests/test_matrix_room_access.py` | unclaimed |  | 3 failures. |
| `tests/test_memory_file_backend.py` | unclaimed |  | 14 failures. |
| `tests/test_memory_facade.py` | unclaimed |  | 5 failures. |
| `tests/test_memory_integration.py` | unclaimed |  | 3 failures. |
| `tests/test_delegate_tools.py` | done | claude-opus / 2026-03-14 | Fixed _bind_runtime_paths wrapper to create RuntimePaths, captured bound config return values, and added runtime_paths as 3rd positional arg in create_agent assertions. |
| `tests/test_router_rooms.py` | done | codex / 2026-03-14 | Updated local runtime binding helper and stale restore_scheduled_tasks mock signature for explicit-runtime tests. |
| `tests/test_extra_kwargs.py` | unclaimed |  | 4 failures. |
| `tests/test_scheduled_task_restoration.py` | unclaimed |  | 2 failures. |
| `tests/test_gemini_integration.py` | unclaimed |  | 5 failures. |
| `tests/test_multi_agent_bot.py` | regressed |  | Was fixed (85 passed) but 1 failure regressed: test_agent_bot_thread_response[True]. Likely broken by concurrent changes. |
| `tests/test_skills.py` | unclaimed |  | 2 failures. |
| `tests/test_self_config.py` | unclaimed |  | 8 failures. |
| `tests/test_skip_mentions.py` | unclaimed |  | 2 failures. |
| `tests/test_subagents.py` | done | codex / 2026-03-14 | Updated stale get_entity_thread_mode mock signature and explicit runtime-path assertion in the target-room dispatch test. |
| `tests/test_team_mode_decision.py` | unclaimed |  | 4 failures. |
| `tests/test_sync_task_cancellation.py` | claimed | codex / 2026-03-14 | Investigate the remaining sync-task cancellation failure under explicit runtime paths. |
| `tests/test_tool_config_sync.py` | done | codex / 2026-03-14 | Ignored injected `runtime_paths` in the sync test and removed stale `config_path` metadata from `config_manager`. |
| `tests/test_voice_command_processing.py` | claimed | codex / 2026-03-14 | Fix the remaining explicit-runtime expectation in voice command processing tests. |
| `tests/test_voice_thread_agent_response.py` | unclaimed |  | 6 failures. |
| `tests/test_config_discovery.py` (regressions) | unclaimed |  | 7 new failures after prior fix: 6 in TestResolveConfigRelativePath + 1 in TestRuntimeGuardrails. |

## Notes

If a test file only needs a mechanical `runtime_paths` argument update, claim it alone and finish it in one small commit.

If a test file requires changing one shared fixture or one shared source file, claim that shared file first and note the dependency here before editing.

If a change requires touching both a source file and many tests, the source file owner should land the source change first, then release the dependent tests for parallel pickup.
