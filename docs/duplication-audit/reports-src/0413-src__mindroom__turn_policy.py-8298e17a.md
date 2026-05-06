Summary: The only meaningful duplication found is the active-response-thread follow-up gate, which remains in both `TurnPolicy` and `AgentBot`.
The rest of `turn_policy.py` is mostly policy-level composition over shared helpers from `authorization.py`, `teams.py`, `thread_utils.py`, and `hooks`, without independently duplicated behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ResponseAction	class	lines 64-69	not-a-behavior-symbol	"ResponseAction response_action kind form_team rejection_message"	src/mindroom/turn_controller.py:1201, src/mindroom/turn_store.py:135
PreparedDispatch	class	lines 73-80	not-a-behavior-symbol	"PreparedDispatch requester_user_id context target correlation_id envelope"	src/mindroom/turn_controller.py:852, src/mindroom/turn_controller.py:1205
DispatchPlan	class	lines 84-93	not-a-behavior-symbol	"DispatchPlan kind route respond ignore router_message media_events"	src/mindroom/turn_controller.py:1778, src/mindroom/turn_policy.py:464
PreparedHookedPayload	class	lines 97-103	not-a-behavior-symbol	"PreparedHookedPayload strip_transient_enrichment_after_run system_enrichment_items"	src/mindroom/turn_controller.py:1301, src/mindroom/turn_controller.py:1313
IngressHookRunner	class	lines 107-233	related-only	"IngressHookRunner EVENT_MESSAGE_ENRICH EVENT_SYSTEM_ENRICH emit_collect"	src/mindroom/hooks/execution.py:338, src/mindroom/hooks/context.py:389, src/mindroom/hooks/context.py:409, src/mindroom/turn_controller.py:828
IngressHookRunner.emit_message_received_hooks	async_method	lines 112-131	related-only	"emit_message_received_hooks EVENT_MESSAGE_RECEIVED HookIngressPolicy suppress"	src/mindroom/turn_controller.py:826, src/mindroom/turn_controller.py:828, src/mindroom/edit_regenerator.py:273
IngressHookRunner.apply_message_enrichment	async_method	lines 133-200	related-only	"apply_message_enrichment MessageEnvelope MessageEnrichContext render_enrichment_block model_prompt"	src/mindroom/turn_controller.py:1301, src/mindroom/hooks/enrichment.py:30, src/mindroom/hooks/execution.py:338
IngressHookRunner.apply_system_enrichment	async_method	lines 202-233	related-only	"apply_system_enrichment SystemEnrichContext emit_collect response_payload.apply_system_enrichment"	src/mindroom/turn_controller.py:1307, src/mindroom/hooks/context.py:409, src/mindroom/hooks/execution.py:338
IngressHookRunner.apply_system_enrichment.<locals>.finish	nested_function	lines 214-223	none-found	"response_payload.apply_system_enrichment enrichment_item_count finish(items)"	none
TurnPolicyDeps	class	lines 237-244	not-a-behavior-symbol	"TurnPolicyDeps runtime logger runtime_paths agent_name matrix_id"	src/mindroom/turn_controller.py:95
TurnPolicy	class	lines 248-598	related-only	"TurnPolicy plan_turn resolve_response_action team_response_action router dispatch"	src/mindroom/turn_controller.py:1216, src/mindroom/turn_controller.py:1725, src/mindroom/bot.py:1875
TurnPolicy.can_reply_to_sender	method	lines 253-260	related-only	"is_sender_allowed_for_agent_reply can_reply_to_sender"	src/mindroom/thread_utils.py:301, src/mindroom/turn_controller.py:318, src/mindroom/bot.py:1433
TurnPolicy.materializable_agent_names	method	lines 262-267	related-only	"resolve_live_shared_agent_names materializable_agent_names orchestrator config"	src/mindroom/teams.py:1288, src/mindroom/bot.py:1875, src/mindroom/api/openai_compat.py:1414
TurnPolicy.filter_materializable_agents	method	lines 269-282	related-only	"materializable_agent_names agent_name username filter materializable"	src/mindroom/teams.py:910, src/mindroom/team_exact_members.py:60
TurnPolicy.available_agents_for_sender	async_method	lines 284-304	related-only	"get_available_agents_for_sender_authoritative get_available_agents_for_sender client is None"	src/mindroom/authorization.py:215, src/mindroom/authorization.py:257, src/mindroom/voice_handler.py:502, src/mindroom/scheduling.py:1268
TurnPolicy.response_owner_for_team_resolution	method	lines 306-325	none-found	"response_owner_for_team_resolution eligible_members TeamOutcome.REJECT TeamIntent.EXPLICIT_MEMBERS min full_id"	src/mindroom/handled_turns.py:710, src/mindroom/edit_regenerator.py:171
TurnPolicy.team_response_action	method	lines 327-349	none-found	"team_response_action TeamOutcome.TEAM INDIVIDUAL REJECT ResponseAction skip"	src/mindroom/teams.py:706, src/mindroom/turn_store.py:138
TurnPolicy.configured_team_response_action	method	lines 351-377	related-only	"configured_team_response_action resolve_configured_team TeamMode.COORDINATE TeamMode.COLLABORATE"	src/mindroom/bot.py:1874, src/mindroom/api/openai_compat.py:1408, src/mindroom/teams.py:1087
TurnPolicy.effective_response_action	method	lines 379-384	none-found	"effective_response_action configured_team_response_action action.kind team"	src/mindroom/turn_controller.py:1216
TurnPolicy.decide_team_for_sender	async_method	lines 386-432	related-only	"decide_team_for_sender decide_team_formation has_multiple_non_agent_users_in_thread all_mentioned_in_thread"	src/mindroom/teams.py:641, src/mindroom/thread_utils.py:321
TurnPolicy.plan_router_dispatch	async_method	lines 434-472	none-found	"plan_router_dispatch ROUTER_AGENT_NAME thread_requires_explicit_agent_targeting only one agent present"	src/mindroom/routing.py:1, src/mindroom/turn_controller.py:1725
TurnPolicy.plan_turn	async_method	lines 475-512	none-found	"plan_turn plan_router_dispatch resolve_response_action DispatchPlan respond ignore"	src/mindroom/turn_controller.py:1725
TurnPolicy.resolve_response_action	async_method	lines 514-574	related-only	"resolve_response_action should_agent_respond decide_team_for_sender available_agents_in_room responder_pool"	src/mindroom/thread_utils.py:301, src/mindroom/teams.py:641, src/mindroom/authorization.py:257
TurnPolicy._should_queue_follow_up_in_active_response_thread	method	lines 576-598	duplicate-found	"_should_queue_follow_up_in_active_response_thread active response thread follow up is_automation_source_kind is_agent_id"	src/mindroom/bot.py:1494, src/mindroom/response_runner.py:549, src/mindroom/response_lifecycle.py:124
```

## Findings

### Active response-thread follow-up gate is duplicated

`TurnPolicy._should_queue_follow_up_in_active_response_thread` at `src/mindroom/turn_policy.py:576` duplicates the same gate still present in `AgentBot._should_queue_follow_up_in_active_response_thread` at `src/mindroom/bot.py:1494`.
Both functions return false when there is no target/envelope, the turn is not a thread, explicit mentions are present, the source is automation, or the sender is an agent.
Both then check whether the target already has an active response.

The behavior is not identical.
The `TurnPolicy` version also treats `dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND` as an unconditional queue signal at `src/mindroom/turn_policy.py:595`, while the `AgentBot` version always delegates to `has_active_response_for_target` at `src/mindroom/bot.py:1510`.
That difference must be preserved if the old bot method still has callers.

## Proposed generalization

Move the shared predicate into a focused helper, for example `mindroom.turn_policy.should_queue_follow_up_in_active_response_thread` or a small helper in a new turn-policy support module only if import direction requires it.
The helper should accept `context`, `target`, `source_envelope`, `config`, `runtime_paths`, and an optional `has_active_response_for_target` callback.
It should also preserve the policy-source bypass used by `TurnPolicy`.
Then delete the stale `AgentBot` method if it has no callers, or make it delegate to the helper if a compatibility call site still exists.

No refactor is recommended for the hook runner, team-response decision, or dispatch-plan symbols.
They are composition over existing shared helpers rather than independently duplicated implementations.

## Risk/tests

The main risk is changing whether follow-up human messages are queued during active streamed responses, especially for coalesced events using `ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND`.
Targeted tests should cover unmentioned human thread replies, agent-sent replies, automation sources, explicit mentions, inactive targets, active targets, and the policy-source bypass.

No production code was edited.
