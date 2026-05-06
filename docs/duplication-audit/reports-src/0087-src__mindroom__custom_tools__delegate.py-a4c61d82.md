## Summary

Top duplication candidate: `DelegateTools.delegate_task` and `_build_delegated_runtime_context` manually retarget execution/runtime state for a child agent, overlapping with the canonical `ToolRuntimeSupport` and runtime-context identity helpers in `src/mindroom/tool_system/runtime_context.py`.
The rest of the module is mostly delegation-specific validation, prompt instructions, and one-shot `ai_response` orchestration.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
DelegateTools	class	lines 34-182	related-only	DelegateTools Toolkit delegate dynamic toolkit registration	src/mindroom/agents.py:500, src/mindroom/tools/delegate.py:19, src/mindroom/tool_system/dynamic_toolkits.py:129
DelegateTools.__init__	method	lines 37-59	related-only	Toolkit __init__ name instructions tools custom_tools	src/mindroom/custom_tools/compact_context.py:23, src/mindroom/agents.py:518
DelegateTools._build_instructions	method	lines 61-71	related-only	describe_agent delegate_to agent description instructions	src/mindroom/agent_descriptions.py:17, src/mindroom/agents.py:500
DelegateTools.delegate_task	async_method	lines 73-159	duplicate-found	delegate_task ai_response execution_identity replace resolve_agent_knowledge_access runtime_context	src/mindroom/tool_system/runtime_context.py:268, src/mindroom/tool_system/runtime_context.py:326, src/mindroom/tool_system/runtime_context.py:432, src/mindroom/teams.py:1555, src/mindroom/teams.py:1950
DelegateTools._build_delegated_runtime_context	method	lines 161-182	duplicate-found	replace ToolRuntimeContext active_model_name session_id resolve_runtime_model child context	src/mindroom/tool_system/runtime_context.py:190, src/mindroom/tool_system/runtime_context.py:268, src/mindroom/history/runtime.py:1345
_resolve_delegated_room_id	function	lines 185-195	related-only	resolve room_id runtime_context execution_identity current room fallback	src/mindroom/tool_system/runtime_context.py:416, src/mindroom/custom_tools/attachment_helpers.py:47, src/mindroom/custom_tools/compact_context.py:56
```

## Findings

### Child execution/runtime retargeting is partly duplicated

`src/mindroom/custom_tools/delegate.py:95` creates a delegated session id, then uses `dataclasses.replace` on `ToolExecutionIdentity` to retarget `agent_name` and `session_id`.
`src/mindroom/custom_tools/delegate.py:161` similarly derives a child `ToolRuntimeContext` by resolving the target model and replacing `agent_name`, `active_model_name`, and `session_id`.

The same behavior family already exists in `src/mindroom/tool_system/runtime_context.py:268`, where `ToolRuntimeSupport.build_dispatch_context` creates matched execution identity plus live runtime context, and in `src/mindroom/tool_system/runtime_context.py:326`, where `build_execution_identity` centralizes Matrix execution identity construction.
`src/mindroom/tool_system/runtime_context.py:432` also defines the canonical conversion from a live runtime context back into `ToolExecutionIdentity`.

The delegate path is not a literal duplicate because it has to preserve the parent Matrix room/thread/requester and retarget only the child agent/session.
The risk is that future fields added to `ToolRuntimeContext` or `ToolExecutionIdentity` may be missed in this local `replace` flow, while the canonical builders evolve elsewhere.

### Room context fallback is related but not enough to refactor alone

`src/mindroom/custom_tools/delegate.py:185` resolves the delegated room id from live runtime context first, then execution identity.
This is related to `src/mindroom/tool_system/runtime_context.py:416`, which resolves the current session id from explicit execution/runtime state, and to custom-tool room target helpers such as `src/mindroom/custom_tools/attachment_helpers.py:47`.

This is only a small precedence helper, not meaningful standalone duplication.
It should be considered only if a broader child-dispatch helper is introduced.

## Proposed Generalization

Add a focused helper in `src/mindroom/tool_system/runtime_context.py`, for example `retarget_tool_dispatch_context_for_agent(...)`, that accepts an optional parent `ToolRuntimeContext`, optional parent `ToolExecutionIdentity`, child `agent_name`, child `session_id`, config/runtime paths, and optional room id override.
It would return the child execution identity, child runtime context, and resolved room id.

Keep behavior differences explicit:

- Preserve parent room/thread/requester/transport metadata.
- Retarget only child `agent_name`, `session_id`, and runtime `active_model_name`.
- Support detached execution when no live `ToolRuntimeContext` exists.
- Preserve current `None` behavior when neither live context nor execution identity has a room id.

Refactor plan:

1. Add tests around current delegate context inheritance before changing behavior.
2. Move the retargeting rules into a small helper in `tool_system/runtime_context.py`.
3. Replace `DelegateTools._build_delegated_runtime_context` and `_resolve_delegated_room_id` with calls to that helper.
4. Keep delegation validation, knowledge resolution, logging, and `ai_response` call in `delegate.py`.
5. Run focused delegate/runtime-context tests plus full `pytest`.

## Risk/tests

Primary risk is changing the scope used for delegated tool credentials, history/session state, or room-aware model resolution.
Tests should cover live Matrix runtime context inheritance, detached execution identity inheritance, missing-context fallback, child model resolution by room id, and preservation of requester/thread/transport fields.

No production code was edited.
