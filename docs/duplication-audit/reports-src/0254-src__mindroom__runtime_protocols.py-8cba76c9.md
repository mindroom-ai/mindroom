Summary: No meaningful runtime behavior duplication found.

The primary file defines narrow structural protocols used by extracted runtime collaborators.
The only notable overlap is type-surface duplication with `BotRuntimeView` / `BotRuntimeState` and with `MultiAgentOrchestrator`'s public runtime surface, but those are intentional protocol contracts rather than repeated executable behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SupportsRunningState	class	lines 29-32	related-only	SupportsRunningState running bool Protocol bot running	src/mindroom/team_exact_members.py:17, src/mindroom/team_exact_members.py:45, src/mindroom/orchestrator.py:246, src/mindroom/orchestrator.py:463
OrchestratorRuntime	class	lines 35-73	related-only	OrchestratorRuntime Protocol orchestrator runtime surface	src/mindroom/teams.py:97, src/mindroom/teams.py:1276, src/mindroom/team_exact_members.py:17, src/mindroom/bot.py:114, src/mindroom/orchestrator.py:238
OrchestratorRuntime.config	method	lines 39-39	related-only	orchestrator.config Config None runtime protocol field	src/mindroom/orchestrator.py:247, src/mindroom/team_exact_members.py:40, src/mindroom/teams.py:1285, src/mindroom/teams.py:1551, src/mindroom/teams.py:1944
OrchestratorRuntime.runtime_paths	method	lines 42-42	related-only	orchestrator.runtime_paths RuntimePaths protocol field	src/mindroom/orchestrator.py:242, src/mindroom/teams.py:1290, src/mindroom/teams.py:1599, src/mindroom/teams.py:2002
OrchestratorRuntime.agent_bots	method	lines 45-45	related-only	orchestrator.agent_bots runtime protocol field	src/mindroom/orchestrator.py:245, src/mindroom/team_exact_members.py:42, src/mindroom/orchestrator.py:280, src/mindroom/orchestrator.py:455
OrchestratorRuntime.knowledge_refresh_scheduler	method	lines 48-48	related-only	knowledge_refresh_scheduler orchestrator protocol property	src/mindroom/orchestrator.py:261, src/mindroom/orchestrator.py:285, src/mindroom/knowledge/utils.py:487, src/mindroom/teams.py:1295
OrchestratorRuntime.hook_message_sender	method	lines 50-50	related-only	hook_message_sender router backed sender protocol	src/mindroom/orchestrator.py:979, src/mindroom/hooks/context.py:185
OrchestratorRuntime.hook_room_state_querier	method	lines 52-52	related-only	hook_room_state_querier router backed room state protocol	src/mindroom/orchestrator.py:986, src/mindroom/hooks/context.py:186
OrchestratorRuntime.hook_room_state_putter	method	lines 54-54	related-only	hook_room_state_putter router backed room state protocol	src/mindroom/orchestrator.py:993, src/mindroom/hooks/context.py:187
OrchestratorRuntime.hook_matrix_admin	method	lines 56-56	related-only	hook_matrix_admin router backed admin protocol	src/mindroom/orchestrator.py:1000, src/mindroom/turn_controller.py:897, src/mindroom/hooks/context.py:188
OrchestratorRuntime.reload_plugins_now	method	lines 58-58	related-only	reload_plugins_now source command protocol	src/mindroom/orchestrator.py:793, src/mindroom/turn_controller.py:901, src/mindroom/hooks/context.py:189
OrchestratorRuntime.handle_bot_ready	method	lines 60-62	related-only	handle_bot_ready bot ready approval transport protocol	src/mindroom/orchestrator.py:1083, src/mindroom/bot.py:1053
OrchestratorRuntime.send_approval_notice	method	lines 64-73	related-only	send_approval_notice approval_event_id thread_id reason protocol	src/mindroom/orchestrator.py:335, src/mindroom/approval_inbound.py:103
SupportsConfig	class	lines 76-80	related-only	SupportsConfig protocol runtime config narrow dependency	src/mindroom/conversation_state_writer.py:15, src/mindroom/conversation_state_writer.py:30, src/mindroom/bot_runtime_view.py:17, src/mindroom/bot_runtime_view.py:86
SupportsConfig.config	method	lines 80-80	related-only	runtime.config SupportsConfig BotRuntimeView config	src/mindroom/bot_runtime_view.py:32, src/mindroom/bot_runtime_view.py:61, src/mindroom/conversation_state_writer.py:44, src/mindroom/conversation_state_writer.py:54
SupportsClientConfig	class	lines 83-90	related-only	SupportsClientConfig client config protocol delivery runtime	src/mindroom/delivery_gateway.py:34, src/mindroom/delivery_gateway.py:310, src/mindroom/bot_room_lifecycle.py:23, src/mindroom/inbound_turn_normalizer.py:33, src/mindroom/edit_regenerator.py:14
SupportsClientConfig.client	method	lines 87-87	related-only	runtime.client SupportsClientConfig BotRuntimeView client	src/mindroom/bot_runtime_view.py:29, src/mindroom/bot_runtime_view.py:60, src/mindroom/delivery_gateway.py:310, src/mindroom/inbound_turn_normalizer.py:116
SupportsClientConfig.config	method	lines 90-90	related-only	runtime.config SupportsClientConfig BotRuntimeView config	src/mindroom/bot_runtime_view.py:32, src/mindroom/bot_runtime_view.py:61, src/mindroom/delivery_gateway.py:310, src/mindroom/post_response_effects.py:73
SupportsConfigOrchestrator	class	lines 93-97	related-only	SupportsConfigOrchestrator protocol config orchestrator knowledge access	src/mindroom/knowledge/utils.py:26, src/mindroom/knowledge/utils.py:466, src/mindroom/bot_runtime_view.py:20, src/mindroom/bot_runtime_view.py:88
SupportsConfigOrchestrator.orchestrator	method	lines 97-97	related-only	runtime.orchestrator SupportsConfigOrchestrator BotRuntimeView orchestrator	src/mindroom/bot_runtime_view.py:41, src/mindroom/bot_runtime_view.py:64, src/mindroom/knowledge/utils.py:486
SupportsClientConfigOrchestrator	class	lines 100-107	related-only	SupportsClientConfigOrchestrator protocol client config orchestrator runtime_started_at	src/mindroom/turn_policy.py:33, src/mindroom/turn_policy.py:240, src/mindroom/hooks/context.py:10, src/mindroom/hooks/context.py:143, src/mindroom/bot_runtime_view.py:18
SupportsClientConfigOrchestrator.orchestrator	method	lines 104-104	related-only	runtime.orchestrator SupportsClientConfigOrchestrator BotRuntimeView orchestrator	src/mindroom/bot_runtime_view.py:41, src/mindroom/bot_runtime_view.py:64, src/mindroom/turn_policy.py:264, src/mindroom/hooks/context.py:143
SupportsClientConfigOrchestrator.runtime_started_at	method	lines 107-107	related-only	runtime_started_at protocol BotRuntimeView BotRuntimeState hook context cache snapshot	src/mindroom/bot_runtime_view.py:53, src/mindroom/bot_runtime_view.py:68, src/mindroom/hooks/context.py:206, src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:84, src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:88
```

## Findings

No real executable duplication was found for `src/mindroom/runtime_protocols.py`.

The protocol classes duplicate small slices of the broader `BotRuntimeView` protocol in `src/mindroom/bot_runtime_view.py:25`.
For example, `SupportsConfig`, `SupportsClientConfig`, `SupportsConfigOrchestrator`, and `SupportsClientConfigOrchestrator` restate subsets of `client`, `config`, `orchestrator`, and `runtime_started_at`, while `BotRuntimeState` provides the concrete dataclass fields at `src/mindroom/bot_runtime_view.py:60`.
This is a type-contract split used by narrow collaborators, and the type-only proof at `src/mindroom/bot_runtime_view.py:77` explicitly checks that `BotRuntimeView` satisfies those smaller protocols.

`OrchestratorRuntime` mirrors public fields and methods implemented by `MultiAgentOrchestrator`, including dataclass fields at `src/mindroom/orchestrator.py:242`, `src/mindroom/orchestrator.py:245`, and `src/mindroom/orchestrator.py:247`, plus hook/reload/approval methods at `src/mindroom/orchestrator.py:793`, `src/mindroom/orchestrator.py:979`, `src/mindroom/orchestrator.py:986`, `src/mindroom/orchestrator.py:993`, `src/mindroom/orchestrator.py:1000`, `src/mindroom/orchestrator.py:1083`, and `src/mindroom/orchestrator.py:335`.
That is also structural typing rather than duplicated implementation.
Callers such as `src/mindroom/team_exact_members.py:34`, `src/mindroom/knowledge/utils.py:466`, `src/mindroom/turn_policy.py:240`, and `src/mindroom/approval_inbound.py:103` depend on the narrowed surface instead of repeating the behavior themselves.

`SupportsRunningState` overlaps the concrete `running` flag on `MultiAgentOrchestrator` at `src/mindroom/orchestrator.py:246` and on managed bots checked through `src/mindroom/orchestrator.py:463`.
The only consumer found is `src/mindroom/team_exact_members.py:45`, where it casts the `agent_bots` mapping to objects with a `running` flag for exact team member availability.
That is not a duplicate running-state calculation.

## Proposed Generalization

No refactor recommended.

The small overlapping protocol declarations are intentional narrow contracts for collaborators that should not depend on the full bot or orchestrator implementation.
Combining them into a single shared protocol would reduce a few repeated property declarations but would make dependency surfaces less precise.

## Risk/Tests

No production code was edited.

If these protocols are changed later, run the type-boundary tests that exercise runtime protocol imports and assignability, especially `tests/test_tach_split_matrix_client_boundaries.py`.
Behavioral tests around turn policy, hook context construction, delivery, and knowledge access should cover any change that widens or narrows the protocol surfaces.
