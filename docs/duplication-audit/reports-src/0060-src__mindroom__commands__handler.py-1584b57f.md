Summary: Top duplication candidates are scheduling runtime construction in command and tool surfaces, agent/team description formatting split between welcome messages and routing prompts, and local Matrix event-id normalization that already exists in handled-turn helpers.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_scheduling_runtime	function	lines 41-51	duplicate-found	SchedulingRuntime construction build_scheduling_runtime runtime collaborators	src/mindroom/tool_system/runtime_context.py:477-492; src/mindroom/scheduling.py:155-165; src/mindroom/custom_tools/scheduler.py:52-54
CommandEvent	class	lines 54-60	related-only	event protocol sender event_id body source nio RoomMessageText	src/mindroom/voice_handler.py:45; src/mindroom/agents.py:106; src/mindroom/matrix/thread_projection.py:17-36
DeriveConversationContext	class	lines 63-74	related-only	derive_conversation_context protocol callable EventInfo thread history	src/mindroom/turn_controller.py:904-918; src/mindroom/inbound_turn_normalizer.py:161-166; src/mindroom/matrix/thread_room_scan.py:20-26
DeriveConversationContext.__call__	async_method	lines 66-74	related-only	derive_conversation_context room_id event_info event_id caller_label tuple	src/mindroom/turn_controller.py:909; src/mindroom/inbound_turn_normalizer.py:161-166
CommandHandlerContext	class	lines 78-92	related-only	CommandHandlerContext dependencies dataclass handle_command context	src/mindroom/turn_controller.py:894-918; src/mindroom/bot_runtime_view.py:25; src/mindroom/runtime_protocols.py:35-100
_format_agent_description	function	lines 95-124	duplicate-found	agent role tools team role Team of agents describe_agent	src/mindroom/agent_descriptions.py:17-55; src/mindroom/routing.py:56-62; src/mindroom/voice_handler.py:401-421; src/mindroom/scheduling.py:649-664
_generate_welcome_message	function	lines 127-172	related-only	Welcome to MindRoom Available agents get_configured_agents_for_room welcome message	src/mindroom/bot_room_lifecycle.py:153-175; src/mindroom/commands/parsing.py:319-331; src/mindroom/voice_handler.py:423-455
_normalized_response_event_id	function	lines 175-177	duplicate-found	normalize event_id non-empty string response_event_id isinstance str	src/mindroom/handled_turns.py:705-707; src/mindroom/history/interrupted_replay.py:226-239; src/mindroom/history/storage.py:280-282
_format_plugin_reload_summary	function	lines 180-186	none-found	Reloaded plugin cancelled task active_plugin_names cancelled_task_count	none
handle_command	async_function	lines 189-372	duplicate-found	command dispatch scheduler tool config confirmation send response record handled turn	src/mindroom/custom_tools/scheduler.py:31-132; src/mindroom/turn_controller.py:960-1010; src/mindroom/turn_controller.py:1228-1242; src/mindroom/turn_controller.py:1398-1408
```

## Findings

### 1. Scheduling runtime construction is duplicated across command and tool contexts

- `src/mindroom/commands/handler.py:41-51` builds a `SchedulingRuntime` from `CommandHandlerContext` plus a Matrix room.
- `src/mindroom/tool_system/runtime_context.py:477-492` builds the same `SchedulingRuntime` fields from `ToolRuntimeContext`.
- Both helpers map the same live collaborators: `client`, `config`, `runtime_paths`, `room`, `conversation_cache`, `event_cache`, and `matrix_admin`.
- The difference to preserve is validation: the tool helper raises when `context.room` is missing, while the command helper receives a concrete `room`.

### 2. Agent/team description formatting is split across multiple source surfaces

- `src/mindroom/commands/handler.py:95-124` formats compact welcome-message descriptions from agent roles, first three effective tools, and team roles/member counts.
- `src/mindroom/agent_descriptions.py:17-55` formats the same underlying agent/team facts for routing prompts, including roles, effective tools, delegates, first instruction, team members, and team mode.
- `src/mindroom/routing.py:56-62`, `src/mindroom/voice_handler.py:401-421`, and `src/mindroom/scheduling.py:649-664` each build separate agent capability/list representations from the same config concepts.
- The behavior is not literally identical because each surface has a different audience: welcome copy needs compact Matrix markdown mentions, routing wants model-readable capability detail, voice wants spoken display names, and scheduling wants Matrix usernames.

### 3. Event-id normalization is locally duplicated

- `src/mindroom/commands/handler.py:175-177` converts non-string or empty send results to `None`.
- `src/mindroom/handled_turns.py:705-707` already implements the same non-empty string normalization for handled-turn event IDs.
- `src/mindroom/history/interrupted_replay.py:226-239` and `src/mindroom/history/storage.py:280-282` repeat equivalent `isinstance(str) and non-empty` checks for Matrix event IDs.
- The command helper is behaviorally identical to the handled-turn helper, but the existing canonical helper is private to `handled_turns.py`.

### 4. Scheduler command execution overlaps with scheduler tools

- `src/mindroom/commands/handler.py:240-291` dispatches `!schedule`, `!list_schedules`, `!cancel_schedule`, and `!edit_schedule` into the scheduling backend.
- `src/mindroom/custom_tools/scheduler.py:31-132` exposes the same backend operations as tools.
- Both surfaces correctly share the scheduling backend, so this is mostly surface-level duplication: context extraction and error handling differ.
- The command path must preserve command-specific mention parsing from `event.source` and support cancel-all, which the tool path does not expose.

### 5. Response send-and-record flow recurs, but no small source-level helper is clearly shared yet

- `src/mindroom/commands/handler.py:304-352` and `src/mindroom/commands/handler.py:358-372` send Matrix responses, normalize the returned event ID, and record a `HandledTurnState`.
- `src/mindroom/turn_controller.py:960-1010`, `src/mindroom/turn_controller.py:1228-1242`, and `src/mindroom/turn_controller.py:1398-1408` have related response-id-to-handled-turn recording flows.
- These are related but not identical: command confirmation registration needs early return and reaction setup, while normal turn-controller paths carry requester/correlation/timing state.
- No refactor is recommended from this file alone unless more command handlers repeat the same send/register pattern.

## Proposed Generalization

1. Move Matrix event-id normalization to a small shared helper, for example `mindroom.matrix.event_ids.normalized_event_id`, then use it from `handled_turns.py`, command handling, and history readers.
2. Add a tiny scheduling runtime factory that accepts already-resolved live collaborators, or make `SchedulingRuntime.from_live_context(...)` a classmethod, while preserving the tool-specific missing-room validation at the tool boundary.
3. Consider extending `agent_descriptions.py` with separate formatter functions for `welcome`, `routing`, `voice`, and `schedule` contexts only if future changes need consistent agent capability display across those surfaces.
4. Leave scheduler command/tool dispatch separate for now because they serve different user interfaces and only share the backend calls.
5. Do not extract a generic command response recorder until another command module repeats the same confirmation-aware send/record/reaction flow.

## Risk/tests

- Event-id normalization is low risk but should cover empty strings, `None`, non-string metadata values, and valid Matrix event IDs in `handled_turns`, command handler, and interrupted replay tests.
- Scheduling runtime factory changes are medium risk because both command execution and scheduler tools depend on live Matrix collaborators; tests should assert all `SchedulingRuntime` fields are preserved and the tool path still fails clearly when no room is available.
- Agent description consolidation is higher copy/regression risk because model prompts, voice normalization, schedule parsing, and human welcome messages intentionally use different wording; snapshot-style tests for each surface would be needed before refactoring.
- Scheduler command/tool unification is not recommended without tests for `!schedule`, tool scheduling, edit, list, cancel, command mention extraction, and command-only cancel-all behavior.
