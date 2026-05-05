## Summary

Top duplication candidate: `SelfConfigTools.get_own_config` duplicates the agent-config YAML rendering flow in `ConfigManagerTools._get_agent_config`.
Top duplication candidate: `SelfConfigTools.update_own_config` substantially overlaps with `ConfigManagerTools._update_agent_config` for loading config, checking agent existence, validating tools and knowledge bases, preserving tool overrides, applying agent field updates, saving a runtime-validated config, and returning a change summary.
No production code was edited.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SelfConfigTools	class	lines 31-218	duplicate-found	SelfConfigTools ConfigManagerTools self_config config_manager Toolkit get_own_config update_own_config	src/mindroom/custom_tools/config_manager.py:103; src/mindroom/custom_tools/config_manager.py:307; src/mindroom/custom_tools/config_manager.py:668; src/mindroom/custom_tools/config_manager.py:889; src/mindroom/tools/self_config.py:4
SelfConfigTools.__init__	method	lines 34-41	related-only	Toolkit init name tools runtime_paths config_path agent_name	src/mindroom/custom_tools/config_manager.py:110; src/mindroom/custom_tools/delegate.py:35
SelfConfigTools.get_own_config	method	lines 43-63	duplicate-found	get agent config authored_model_dump yaml.dump Configuration for load_config_or_user_error	src/mindroom/custom_tools/config_manager.py:889; src/mindroom/commands/config_commands.py:188
SelfConfigTools.update_own_config	method	lines 65-218	duplicate-found	update agent config load_config_and_tool_metadata_or_error Unknown tools preserve_tool_overrides validate_knowledge_bases AgentConfig.model_validate save_runtime_validated_config Successfully updated	src/mindroom/custom_tools/config_manager.py:44; src/mindroom/custom_tools/config_manager.py:53; src/mindroom/custom_tools/config_manager.py:83; src/mindroom/custom_tools/config_manager.py:319; src/mindroom/custom_tools/config_manager.py:668; src/mindroom/commands/config_commands.py:316; src/mindroom/api/config_lifecycle.py:220
```

## Findings

### Agent configuration YAML rendering is duplicated

`SelfConfigTools.get_own_config` loads the current config, checks `self.agent_name` exists, calls `authored_model_dump()` on the agent config, dumps it with `yaml.dump(default_flow_style=False, sort_keys=False)`, and returns the same fenced heading format at `src/mindroom/custom_tools/self_config.py:50` and `src/mindroom/custom_tools/self_config.py:61`.
`ConfigManagerTools._get_agent_config` performs the same behavior for a provided `agent_name` at `src/mindroom/custom_tools/config_manager.py:891` and `src/mindroom/custom_tools/config_manager.py:899`.

The behavior is functionally the same: both expose one authored agent config as YAML to a tool caller.
Differences to preserve: `SelfConfigTools` uses the stricter self-scoped missing-agent message with "in configuration", while `ConfigManagerTools` uses a shorter general admin-tool message.

### Agent update workflow is duplicated across self and manager tools

`SelfConfigTools.update_own_config` and `ConfigManagerTools._update_agent_config` share most of the agent-update pipeline.
Both load a runtime config with plugin load errors tolerated, reject unknown target agents, validate requested tools against runtime tool metadata, validate knowledge bases through `validate_knowledge_bases`, preserve existing per-tool overrides through `_preserve_tool_overrides`, update only non-null provided fields, save via `_save_runtime_validated_config`, catch validation/runtime-validation errors with `format_invalid_config_message`, and return a human-readable change list.

The relevant self-config lines are `src/mindroom/custom_tools/self_config.py:114`, `src/mindroom/custom_tools/self_config.py:123`, `src/mindroom/custom_tools/self_config.py:127`, `src/mindroom/custom_tools/self_config.py:150`, `src/mindroom/custom_tools/self_config.py:155`, `src/mindroom/custom_tools/self_config.py:177`, `src/mindroom/custom_tools/self_config.py:182`, `src/mindroom/custom_tools/self_config.py:209`, and `src/mindroom/custom_tools/self_config.py:217`.
The matching manager-tool lines are `src/mindroom/custom_tools/config_manager.py:684`, `src/mindroom/custom_tools/config_manager.py:693`, `src/mindroom/custom_tools/config_manager.py:699`, `src/mindroom/custom_tools/config_manager.py:704`, `src/mindroom/custom_tools/config_manager.py:711`, `src/mindroom/custom_tools/config_manager.py:719`, `src/mindroom/custom_tools/config_manager.py:758`, `src/mindroom/custom_tools/config_manager.py:761`, and `src/mindroom/custom_tools/config_manager.py:764`.

Differences to preserve: self-config blocks privileged `config_manager` assignment directly and through inherited defaults at `src/mindroom/custom_tools/self_config.py:136` and `src/mindroom/custom_tools/self_config.py:140`.
Self-config validates by creating a new `AgentConfig` from merged data at `src/mindroom/custom_tools/self_config.py:179`, while the manager mutates the existing Pydantic model field-by-field at `src/mindroom/custom_tools/config_manager.py:711`.
Self-config supports more fields than the manager method, including `show_tool_calls`, `thread_mode`, history settings, compression settings, and `context_files`.
Manager output includes a checkmark and agent name; self-config intentionally says "own configuration".

### Shared helper functions already reduce some duplication

`SelfConfigTools.update_own_config` imports `_preserve_tool_overrides`, `_save_runtime_validated_config`, and `validate_knowledge_bases` from `ConfigManagerTools`' module at `src/mindroom/custom_tools/self_config.py:14`.
Those helpers live at `src/mindroom/custom_tools/config_manager.py:44`, `src/mindroom/custom_tools/config_manager.py:53`, and `src/mindroom/custom_tools/config_manager.py:83`.

This means the active duplication is not the persistence and knowledge-base validation internals themselves.
The remaining duplication is in the surrounding orchestration: load config, resolve tool metadata, reject missing agents, compute changed fields, persist, and format messages.

## Proposed Generalization

Move only the neutral shared agent-config behaviors out of `config_manager.py` into a focused helper module, for example `src/mindroom/custom_tools/agent_config_updates.py`.
The helper should stay small and pure where possible.

Suggested minimal helpers:

1. `render_agent_config_yaml(config: Config, agent_name: str) -> str | None` or `format_agent_config_yaml(agent_name: str, agent: AgentConfig) -> str`.
2. `validate_tool_names(tool_names: list[str], tool_metadata: Mapping[str, ToolMetadata]) -> list[str]`.
3. `build_validated_agent_update(agent: AgentConfig, requested_updates: Iterable[tuple[str, object | None]]) -> tuple[AgentConfig, list[AgentConfigChange]]`.
4. `format_agent_changes(changes: Sequence[AgentConfigChange]) -> str`.

Keep self-config authorization rules in `self_config.py`.
Keep admin-only `manage_agent` wording in `config_manager.py`.
No broad architecture change is recommended.

## Risk/tests

Primary risk is message-format drift for tools that agents may rely on.
Tests should assert exact output for `get_own_config` and `_get_agent_config`, including YAML sort order and missing-agent messages.
Update tests should cover no-op updates, invalid tools, invalid knowledge bases, preserved tool overrides, validation failure formatting, and self-config privileged-tool blocking.
Because self-config currently validates a merged `AgentConfig` before assignment while `ConfigManagerTools._update_agent_config` mutates with assignment validation, any shared update helper must preserve validated final values and not bypass Pydantic defaults or validators.
