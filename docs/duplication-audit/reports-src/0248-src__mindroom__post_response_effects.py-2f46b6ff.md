Summary: The main duplication candidate is the interactive-question delivery sequence: register the interactive question, then add Matrix reaction buttons.
`PostResponseEffectsSupport._register_interactive_delivery` performs this for agent responses, while `MatrixConversationOperations` repeats the same sequence for send/edit tools.
No other meaningful duplicated behavior was found in this primary file; most remaining symbols are typed data carriers or thin adapters around existing shared helpers.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ResponseOutcome	class	lines 32-47	related-only	ResponseOutcome dataclass fields response_run_id interactive_target thread_summary memory_prompt	tests/test_ai_user_id.py:2382; tests/test_multi_agent_bot.py:5447; src/mindroom/response_runner.py:1367; src/mindroom/response_runner.py:2388
PostResponseEffectsDeps	class	lines 51-66	related-only	PostResponseEffectsDeps register_interactive queue_memory_persistence persist_response_event_id strip_transient_enrichment	tests/test_cancelled_response_hook.py:43; tests/test_streaming_behavior.py:49; src/mindroom/response_lifecycle.py:448; src/mindroom/response_runner.py:2408
PostResponseEffectsSupport	class	lines 70-191	related-only	PostResponseEffectsSupport build_deps queue_thread_summary register interactive delivery	src/mindroom/bot.py:428; src/mindroom/response_runner.py:358; tests/test_queued_message_notify.py:479; tests/test_streaming_finalize.py:691
PostResponseEffectsSupport._client	method	lines 79-85	related-only	runtime.client if client is None Matrix client is not ready	src/mindroom/response_runner.py:388; src/mindroom/delivery_gateway.py:344; src/mindroom/turn_controller.py:203; src/mindroom/conversation_resolver.py:124
PostResponseEffectsSupport.should_queue_thread_summary	method	lines 87-99	related-only	should_queue_thread_summary message_count_hint thread_summary threshold	src/mindroom/thread_summary.py:184; src/mindroom/response_runner.py:1379; src/mindroom/response_runner.py:2403; tests/test_multi_agent_bot.py:5695
PostResponseEffectsSupport._timed_thread_summary	async_method	lines 102-108	related-only	@timed maybe_generate_thread_summary timed summary_coro	src/mindroom/thread_summary.py:358; src/mindroom/thread_summary.py:502; src/mindroom/timing.py:1; src/mindroom/background_tasks.py:21
PostResponseEffectsSupport._register_interactive_delivery	async_method	lines 110-133	duplicate-found	register_interactive_question add_reaction_buttons interactive_metadata options_as_list	src/mindroom/custom_tools/matrix_conversation_operations.py:109; src/mindroom/custom_tools/matrix_conversation_operations.py:680; src/mindroom/interactive.py:668; src/mindroom/interactive.py:714
PostResponseEffectsSupport.queue_thread_summary	method	lines 135-157	related-only	maybe_generate_thread_summary create_background_task thread_summary owner runtime	src/mindroom/thread_summary.py:502; src/mindroom/background_tasks.py:21; src/mindroom/response_runner.py:2176; tests/test_multi_agent_bot.py:5233
PostResponseEffectsSupport.build_deps	method	lines 159-191	related-only	build_deps register_interactive PostResponseEffectsDeps queue_memory_persistence	src/mindroom/response_runner.py:823; src/mindroom/response_runner.py:1384; src/mindroom/response_runner.py:2408; src/mindroom/response_lifecycle.py:500
PostResponseEffectsSupport.build_deps.<locals>.register_interactive	nested_async_function	lines 170-181	related-only	register_interactive closure room_id interactive_agent_name _register_interactive_delivery	src/mindroom/response_runner.py:823; src/mindroom/response_runner.py:1384; src/mindroom/response_runner.py:2408
apply_post_response_effects	async_function	lines 194-292	related-only	apply_post_response_effects persist_response_event_id strip_transient_enrichment queue_memory_persistence thread_summary	src/mindroom/response_lifecycle.py:514; src/mindroom/response_runner.py:631; src/mindroom/response_runner.py:2176; tests/test_queued_message_notify.py:331
```

Findings:

1. Interactive question registration plus reaction-button delivery is duplicated.
`src/mindroom/post_response_effects.py:120` stores the active interactive question, then `src/mindroom/post_response_effects.py:127` adds reaction buttons.
`src/mindroom/custom_tools/matrix_conversation_operations.py:125` and `src/mindroom/custom_tools/matrix_conversation_operations.py:132` do the same sequence for sent tool messages after parsing interactive text.
`src/mindroom/custom_tools/matrix_conversation_operations.py:681` and `src/mindroom/custom_tools/matrix_conversation_operations.py:688` repeat the same sequence again for edited tool messages.
The shared behavior is functionally the same: persist the event-to-options mapping with room/thread/agent metadata, then publish one Matrix reaction per option.
Differences to preserve: post-response effects receive already-extracted `InteractiveMetadata` from final delivery, while tool operations may first parse message text; tool operations use `ToolRuntimeContext.client`, while post-response effects resolves `runtime.client`.

No duplicate found for `apply_post_response_effects` as an overall workflow.
Its guarded post-delivery side effects are only invoked through `ResponseLifecycleCoordinator.apply_effects_safely` at `src/mindroom/response_lifecycle.py:514`.
The individual callback bodies for response-event persistence and memory persistence live in `src/mindroom/response_runner.py:631` and `src/mindroom/response_runner.py:2176`, but those are caller-specific effects passed into the shared coordinator, not duplicate coordinators.

No duplicate found for thread-summary queueing as a whole.
`PostResponseEffectsSupport.should_queue_thread_summary` delegates to the canonical threshold helper in `src/mindroom/thread_summary.py:184`, and `PostResponseEffectsSupport.queue_thread_summary` is the only found response-side background queue wrapper around `maybe_generate_thread_summary`.
The thread-summary module contains the actual generation and manual-send paths, but those are distinct from post-response queuing.

Proposed generalization:

Extract a small helper in `src/mindroom/interactive.py`, for example `async def register_interactive_question_with_reactions(client, room_id, event_id, thread_id, metadata, agent_name, *, config) -> None`.
It should call `register_interactive_question(...)` and `add_reaction_buttons(...)` in the existing order.
Then replace the three call sequences in `src/mindroom/post_response_effects.py` and `src/mindroom/custom_tools/matrix_conversation_operations.py`.
Keep parsing of interactive text outside the helper so tool send/edit behavior remains unchanged.

Risk/tests:

Behavior risk is low if the helper preserves call order and uses `metadata.option_map` plus `metadata.options_as_list()` exactly as today.
Tests to cover during a refactor would be existing post-response interactive registration tests in `tests/test_streaming_finalize.py` or `tests/test_queued_message_notify.py`, plus Matrix conversation operation tests for send/edit interactive messages if present.
No production code was changed in this audit.
