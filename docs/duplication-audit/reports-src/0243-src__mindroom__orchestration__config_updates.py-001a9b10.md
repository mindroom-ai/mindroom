## Summary

Top duplication candidate: MCP server ids are repeatedly converted to MCP tool names and resolved back to dependent entities in `src/mindroom/orchestration/config_updates.py` and `src/mindroom/orchestrator.py`.
The rest of this module is mostly the centralized config-reload planning logic, with related call sites but no meaningful duplicated behavior elsewhere under `src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ConfigUpdatePlan	class	lines 23-51	related-only	ConfigUpdatePlan entities_to_restart new_entities removed_entities only_support_service_changes	src/mindroom/orchestrator.py:1146, src/mindroom/orchestrator.py:1235, src/mindroom/orchestrator.py:1308, src/mindroom/hooks/context.py:549
ConfigUpdatePlan.has_entity_changes	method	lines 38-40	none-found	has_entity_changes entities_to_restart new_entities removed_entities	src/mindroom/orchestrator.py:1363, tests/test_multi_agent_bot.py:13321
ConfigUpdatePlan.only_support_service_changes	method	lines 43-51	none-found	only_support_service_changes mindroom_user_changed matrix_room_access_changed matrix_space_changed authorization_changed	src/mindroom/orchestrator.py:1363, tests/test_multi_agent_bot.py:13321
_config_entries_differ	function	lines 54-58	none-found	model_dump exclude_none compare config entries old_entry new_entry	src/mindroom/api/main.py:568, src/mindroom/history/compaction.py:1469, src/mindroom/orchestrator.py:1160
_identify_entities_to_restart	function	lines 61-78	related-only	identify entities restart config changes router changed agents teams mcp	src/mindroom/orchestrator.py:1235, src/mindroom/orchestrator.py:1263, src/mindroom/orchestrator.py:1353
_get_changed_agents	function	lines 81-114	none-found	changed agents config culture agent_bots new_agent removed_agent	src/mindroom/config/main.py:974, src/mindroom/agents.py:783
_culture_signature_for_agent	function	lines 117-123	related-only	culture signature mode description get_agent_culture	src/mindroom/agents.py:783, src/mindroom/agents.py:797, src/mindroom/config/main.py:974
_get_changed_teams	function	lines 126-146	none-found	changed teams config agent_bots new_team removed_team	src/mindroom/orchestrator.py:722, src/mindroom/config/main.py:1679
_router_needs_restart	function	lines 149-156	related-only	router restart configured rooms get_all_configured_rooms	src/mindroom/bot.py:184, src/mindroom/matrix/rooms.py:416, src/mindroom/avatar_generation.py:173
_changed_mcp_servers	function	lines 159-171	none-found	changed mcp servers config.mcp_servers sync_servers changed_server_ids	src/mindroom/mcp/manager.py:86, src/mindroom/mcp/manager.py:111, src/mindroom/orchestrator.py:1347
_entities_referencing_mcp_servers	function	lines 174-183	duplicate-found	mcp_tool_name get_entities_referencing_tools changed_server_ids affected entities	src/mindroom/orchestrator.py:701, src/mindroom/orchestrator.py:1214, src/mindroom/orchestrator.py:1263, src/mindroom/orchestrator.py:1353
build_config_update_plan	function	lines 186-215	related-only	build config update plan configured_entities existing_entities removed_entities current_config new_config	src/mindroom/orchestrator.py:1317, src/mindroom/orchestrator.py:1325, src/mindroom/orchestrator.py:1363
```

## Findings

### 1. MCP-server dependency resolution is duplicated

`src/mindroom/orchestration/config_updates.py:174` converts changed MCP server ids to tool names and unions entities referencing those tools in the old and new config snapshots.
`src/mindroom/orchestrator.py:1214` repeats the same conversion and old/new config union before stopping affected entities ahead of MCP sync.
`src/mindroom/orchestrator.py:1353` repeats the same single-config variant when runtime MCP sync reports changed servers.
`src/mindroom/orchestrator.py:701` and `src/mindroom/orchestrator.py:1263` are related single-config variants for failed-server blocking and catalog-change restart.

The duplicated behavior is the server-id-to-tool-name projection followed by `Config.get_entities_referencing_tools(...)`.
The difference to preserve is whether the caller needs one config snapshot or the union across old and new snapshots.

## Proposed Generalization

Add a small pure helper near the existing planner helpers, for example in `src/mindroom/orchestration/config_updates.py` or a focused orchestration utility module:

```python
def entities_referencing_mcp_server_ids(config: Config, server_ids: set[str]) -> set[str]:
    return config.get_entities_referencing_tools({mcp_tool_name(server_id) for server_id in server_ids})
```

Then keep `_entities_referencing_mcp_servers(...)` as the old/new union wrapper and update orchestrator call sites that currently inline the same conversion.
No broader refactor is recommended.

## Risk/tests

Risk is low if the helper remains a pure set transformation.
Tests should cover config reload with changed MCP server config, runtime MCP catalog/server changes, failed MCP server startup blocking, and the pre-sync stop path that unions old and new config references.
