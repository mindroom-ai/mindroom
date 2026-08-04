# Method-Ownership Ledger (Phase 0)

Companion artifact to `2026-08-02-turn-pipeline-lifecycle-refactor.md`.
Inventoried at `a66c8bec` against the working tree.
Files: `src/mindroom/turn_controller.py` (2778 lines) and `src/mindroom/response_runner.py` (3081 lines), total 5859 lines.
Line ranges are exact (def/class line -> last body line).
"Owner" = candidate home after extraction; **stay** = coordination/wiring that belongs in the shell.
Phase 8 moves remaining domain decisions out of these composition roots only where this ledger identifies a focused owner.

Cross-file call facts that shape ownership: `text_ingress_dispatch.py` calls `controller._prepare_dispatch` (:261), `controller._has_newer_unresponded_cached_thread_event` (:358), `controller._has_newer_unresponded_in_thread` (:374), `controller._router_handled_turn_outcome` (:434), `controller._execute_response_action` (:493), `controller._execute_router_relay` (:600).
`bot.py` calls `handle_coalesced_batch` (:2187), `reserve_prompt_ingress_order` (:2253), `handle_text_event` (:2271), `handle_media_event` (:2446) and wires `handle_interactive_selection`/`reserve_prompt_ingress_order` into `reaction_dispatch.py`.
The plan demands relocating five non-gateway terminal constructions out of `response_runner.py` into `delivery_gateway.py`/`response_terminal.py` (plan Phase 5).

## 1. `turn_controller.py` — module level (L1-451)

| Lines | Item | Purpose | Calls | Candidate owner |
|---|---|---|---|---|
| 1-153 | imports + 4 constants | Notice/claim metadata kinds, router fallback texts | — | stay (texts move with router cluster) |
| 156-164 | `_gate_router_target_readiness` | Drop unready/stale router selection | OrchestratorRuntime | new `router_relay.py` |
| 167-174 | `_RouterTargetResolution` | Router target + readiness result dataclass | — | new `router_relay.py` |
| 177-205 | `_resolve_router_target` | Resolve relay target or retain for recovery | `_gate_router_target_readiness`, `turn_dispatch_recovery_active` | new `router_relay.py` |
| 208-214 | `_RouterRelayDelivery` | Final relay delivery decision dataclass | — | new `router_relay.py` |
| 217-254 | `_send_router_relay_after_readiness_recheck` | Recheck target readiness immediately before send; fallback text | DeliveryGateway, OrchestratorRuntime | new `router_relay.py` |
| 257-275 | `_room_level_context_event` | Strip `m.relates_to` so room-level dispatch can't pull thread context | PreparedTextEvent | conversation_resolver.py |
| 278-298 | `_scheduled_history_budget_for_dispatch` | Derive trusted history budget for scheduled dispatch | dispatch_source helpers, EventInfo | conversation_resolver.py (or dispatch_source.py) |
| 301-317 | `_queued_notice_dispatch_metadata` | Wrap notice reservation as pending metadata | QueuedHumanNoticeReservation | response_lifecycle.py |
| 320-331 | `_consume_queued_notice_reservations_from_metadata` | Consume vs cancel reservations on batch flush | QueuedHumanNoticeReservation | response_lifecycle.py |
| 334-366 | `_raw_voice_fallback_event` | Build dispatchable raw-audio fallback text event | attachment parsing, media caption | inbound_turn_normalizer.py |
| 369-379 | `_EditRegenerator` Protocol | Minimal edit-regenerator seam | — | stay (or edit_regenerator.py) |
| 382-391 | `_PrecheckedEvent` + aliases | Event that passed ingress prechecks | — | ingress_validation.py |
| 394-397 | `_IngressAdmissionOutcome` | admitted/consumed/ignored enum | — | prompt_ingress_reservation.py |
| 400-406 | `_ReplayGuardContext` | Replay-guard evidence bundle | — | dispatch_replay_guard.py |
| 409-414 | `_DispatchPreparation` | PreparedDispatch + replay guard pair | — | conversation_resolver.py (follows `_prepare_dispatch`) |
| 417-422 | `_ReadyVoiceFallback` | Voice fallback + ready wrapper | — | inbound_turn_normalizer.py |
| 425-451 | `TurnControllerDeps` | 22-collaborator wiring record | — | stay |

## 2. `turn_controller.py` — `TurnController` (L453-2778)

| Lines | Method | Purpose | Calls | Candidate owner |
|---|---|---|---|---|
| 459-464 | `_client` | Require ready Matrix client | runtime | stay |
| 466-481 | `reserve_prompt_ingress_order` | Reserve receipt-order lane slot | CoalescingGate, PromptIngressReservationOwner | stay (public seam) |
| 483-494 | `_precheck_dispatch_event` | Typed precheck wrapper | IngressValidator | stay (thin delegate) |
| 496-518 | `_has_newer_unresponded_in_thread` | Bind deps into replay-guard check | dispatch_replay_guard, IngressValidator, TurnStore | dispatch_replay_guard.py |
| 520-547 | `_has_newer_unresponded_cached_thread_event` | Cached-event replay proof when history degraded | dispatch_replay_guard, event cache, conversation cache | dispatch_replay_guard.py |
| 549-566 | `_should_skip_deep_synthetic_full_dispatch` | Stop deep synthetic hook relays pre-dispatch | hook_ingress_policy | turn_policy.py |
| 568-571 | `_same_response_lifecycle_target` | Same-lock target comparison | — | message_target.py |
| 573-590 | `_queued_notice_reservation_if_busy` | Reserve mid-turn notice when conversation busy | ResponseRunner, envelope origin | response_lifecycle.py |
| 592-618 | `_voice_queued_notice_reservation` | Keep/replace/cancel notice once voice target final | ResponseRunner | response_lifecycle.py |
| 620-671 | `_enqueue_prepared_text_for_dispatch` | Build target, reserve notice, enqueue w/ cancel cleanup | ConversationResolver, `_enqueue_for_dispatch` | stay (coordination; notice part -> response_lifecycle.py) |
| 673-717 | `_enqueue_media_for_dispatch` | Media variant of enqueue | ConversationResolver, `_enqueue_for_dispatch` | stay (same split) |
| 719-779 | `_should_skip_router_before_shared_ingress_work` | Router pre-ingress skip decision (mentions, thread snapshot, explicit targeting) | command_parser, thread_utils, conversation cache, TurnPolicy | turn_policy.py |
| 781-793 | `_coalescing_key_for_event` | Canonical sender/thread scope | ConversationResolver | conversation_resolver.py |
| 795-808 | `_append_live_event_with_timing` | Cache append + timing marks | conversation cache | stay (timing wiring) |
| 810-825 | `_resolve_text_event_with_ingress_timing` | Normalize + timing marks | InboundTurnNormalizer | stay (timing wiring) |
| 827-910 | `_dispatch_prepared_text_like_ingress` | Central text-ingress switchboard: echo drop -> interactive answer -> command -> enqueue | ConversationResolver, IngressValidator, interactive, TurnStore, `_dispatch_command_control_input` | text_ingress_dispatch.py |
| 912-931 | `_handle_edit_event` | Append + hand edit to regenerator | conversation cache, edit_regenerator | stay |
| 933-976 | `_notify_command_target_not_ready` | Fail command visibly when thread unresolved | command_parser, ConversationResolver, VisibleResponseReconciler, TurnStore | command_turn_executor.py |
| 978-1008 | `_dispatch_command_control_input` | Batch + handoff a command bypassing the gate | coalescing_batch, dispatch_handoff, TurnRecord | command_turn_executor.py |
| 1010-1099 | `_enqueue_for_dispatch` | Gate admission: relay detection, claim/notice metadata, admit + timing | IngressValidator, `_coalescing_key_for_event`, PromptIngressReservationOwner, TurnStore | stay core; metadata assembly -> prompt_ingress_reservation.py, relay detection -> ingress_validation.py |
| 1101-1282 | `_prepare_dispatch` | Build dispatch context: extract context (command/relay/normal), coalesced-thread override, target, envelope, hooks, managed-sender block, replay guard | ConversationResolver, IngressValidator, IngressHookRunner, VisibleResponseReconciler, hook_ingress_policy | conversation_resolver.py (context/envelope) + turn_policy.py (hook suppression + sender block) |
| 1284-1303 | `handle_interactive_selection` | Commit/restore claim shell around execution | interactive | stay (public seam for reaction_dispatch) |
| 1305-1474 | `_execute_interactive_selection` | Full selection turn: durable-terminal check, history, ack delivery, payload+attachments, envelope, response, settlement | ConversationResolver, TurnStore, VisibleResponseReconciler, InboundTurnNormalizer, ResponseRunner, entity registry | interactive.py (as injected-deps executor) |
| 1476-1489 | `_interactive_selection_is_durably_terminal` | Both-ids durable-terminal query | TurnStore | interactive.py (with executor) |
| 1491-1499 | `_require_durable_interactive_selection` | Retryable raise until terminal truth | TurnStore | interactive.py |
| 1501-1504 | `_interactive_selection_retry_error` | Shared retry error | — | interactive.py |
| 1506-1545 | `_router_handoff_extra_content` | Normalize relay extra content (per-fire thread root, original sender) | turn_origin, IngressValidator, dispatch_source | new `router_relay.py` |
| 1547-1583 | `_router_handoff_with_attachments` | Register routed media, merge attachment IDs | InboundTurnNormalizer, attachment ids | new `router_relay.py` (calls normalizer) |
| 1585-1728 | `_execute_router_relay` | Whole router relay: candidates, deterministic/AI routing, readiness gating, target, handoff metadata, visible delivery turn, send w/ recheck, record | TurnPolicy, routing.suggest_responder, ConversationResolver, VisibleResponseReconciler, DeliveryGateway, TurnStore | new `router_relay.py` |
| 1730-1742 | `_router_handled_turn_outcome` | Adopt finalized visible echo as terminal outcome for ignored router turn | TurnStore, canonicalize_turn_record | visible_response_reconciliation.py |
| 1744-1775 | `_finalize_dispatch_failure` | Convert dispatch setup failure into visible terminal message | error_handling, DeliveryGateway | visible_response_reconciliation.py |
| 1777-1804 | `_build_response_settlement_callbacks` | Interrupted/deferred/user-stop recording callbacks | InterruptedTurnRooms, TurnStore, with_user_stop | turn_store.py |
| 1806-1987 | `_execute_response_action` (noqa C901/PLR0912/PLR0915) | Final response path: reject delivery, team member names, payload prep, ad-hoc team mode, settlement callbacks, ResponseRequest assembly, team vs individual run, failure finalize | TurnPolicy, entity registry, teams, ResponsePayloadPreparation, ResponseRunner, VisibleResponseReconciler, TurnStore | new `response_action_executor.py` (reject path -> visible_response_reconciliation.py; request assembly -> response_payload_preparation.py) |
| 1989-2026 | `handle_coalesced_batch` | Gate flush callback: handoff, consume notices, TurnRecord, dispatch, timing | dispatch_handoff, `_consume_queued_notice_reservations_from_metadata`, ConversationResolver | stay (public seam); TurnRecord-from-handoff construction -> dispatch_handoff.py |
| 2028-2039 | `_queued_notice_target_key_for_handoff` | Target key for notice matching | `_room_level_context_event`, ConversationResolver | conversation_resolver.py |
| 2041-2058 | `_dispatch_handoff` | Unpack handoff into text dispatch | `_dispatch_text_message` | stay |
| 2060-2077 | `_claim_live_turn` | Claim live source or classify competing-owner outcome | TurnStore | turn_store.py |
| 2079-2094 | `handle_text_event` | Public text entry + cache scope | ConversationResolver | stay |
| 2096-2162 | `_handle_message_inner` | Text ingress sequencing: drops, precheck, timing, reservation, edit branch, claim, ingest, release | IngressValidator, TurnStore, reservation owner | stay (turn-sequencing core); stream-status drop -> ingress_validation.py |
| 2164-2219 | `_ingest_live_text_event` | Resolve -> router-skip -> append -> normalize -> dispatch chain | ConversationResolver, command notify, router skip | stay (coordination chain) |
| 2221-2259 | `_dispatch_text_message` | Adapter into `dispatch_text_message` | text_ingress_dispatch, CommandTurnExecutor, VisibleResponseReconciler | stay |
| 2261-2270 | `handle_media_event` | Public media entry + cache scope | ConversationResolver | stay |
| 2272-2366 | `_handle_media_message_inner` | Media ingress sequencing: precheck, agent-audio drop, claim, reservation, audio/file/media branches, release | IngressValidator, TurnStore, reservation owner | stay; agent-audio drop -> ingress_validation.py |
| 2368-2388 | `_dispatch_special_media_as_text` | File-sidecar branch | `_dispatch_file_sidecar_text_preview` | stay (or fold into caller) |
| 2390-2426 | `_on_audio_media_message` | Resolve voice key, spawn ready task, admit | `_resolve_ready_voice_target`, reservation owner | stay (admission wiring); body -> new `voice_ingress.py` |
| 2428-2450 | `_resolve_ready_voice_target` | Append + coalescing thread + voice target | ConversationResolver | new `voice_ingress.py` |
| 2452-2589 | `_ready_voice_event` | Voice readiness: echo start, envelope, notice, normalize-or-fallback, publication wait, claim transfer, fallback on error | VisibleVoiceEchoLifecycle, ConversationResolver, InboundTurnNormalizer, TurnStore | new `voice_ingress.py` |
| 2591-2617 | `_prepare_raw_voice_fallback_event` | Raw-audio fallback w/ warning | InboundTurnNormalizer, `_raw_voice_fallback_event` | inbound_turn_normalizer.py |
| 2619-2690 | `_ready_voice_fallback_event` | Fallback readiness wrapper + envelope/metadata | ConversationResolver, `_voice_queued_notice_reservation` | new `voice_ingress.py` |
| 2692-2747 | `_normalize_voice_event_or_fallback` | Normalize voice w/ fallback + timing | InboundTurnNormalizer | inbound_turn_normalizer.py |
| 2749-2778 | `_dispatch_file_sidecar_text_preview` | Sidecar preview through text pipeline | InboundTurnNormalizer, `_dispatch_prepared_text_like_ingress` | inbound_turn_normalizer.py (prep) + stays thin |

## 3. `response_runner.py` — module level (L1-476)

| Lines | Item | Purpose | Calls | Candidate owner |
|---|---|---|---|---|
| 1-130 | imports, type vars | — | — | stay |
| 132-140 | `_merge_response_extra_content` | Merge attachment IDs into extra content | — | final_delivery.py |
| 143-154 | `_split_delivery_tool_trace` | Split trace into completed/interrupted | — | response_terminal.py |
| 157-163 | `_materialize_matrix_run_metadata` | Concrete metadata dict | — | response_turn.py |
| 166-177 | `_agent_has_matrix_messaging_tool` | Agent can issue Matrix message actions | visible_tool_surface | execution_preparation.py |
| 180-188 | `_cached_room_display_name` | Room name from sync cache | nio client | execution_preparation.py |
| 191-219 | `_matrix_message_target_item` | Build Matrix targeting enrichment item | `_cached_room_display_name` | execution_preparation.py |
| 222-230 | `_with_matrix_message_target` | Replace hook-provided target enrichment | — | execution_preparation.py |
| 233-257 | `_timestamp_thread_history_user_turns` | Add local timestamps to user history entries | entity registry, prefix_user_turn_time | execution_preparation.py (already owns history rendering) |
| 260-288 | `prepare_memory_and_model_context` | Raw memory inputs + timestamped model context | `_timestamp_thread_history_user_turns`, memory helpers | execution_preparation.py |
| 291-337 | `ResponseRequest` | Public request carrier + 3 properties | — | stay (public contract) |
| 340-350 | `PostLockRequestPreparationError` | Post-lock prep failure w/ placeholder id | — | response_payload_preparation.py (moves with `_prepare_request_after_lock`) |
| 353-359 | `_EarlyPlaceholderState` | Early placeholder tracking | — | response_lifecycle.py |
| 362-392 | `_DeliveryProgress` | Mutable pre/post-delivery settle-once state | FinalDeliveryOutcome | response_terminal.py |
| 395-400 | `_ResponseGenerationOutcome` | Generation outcome pair | — | response_turn.py |
| 403-414 | `_NonStreamingGeneration` | Non-streaming artifacts bundle | — | response_turn.py |
| 417-425 | `_generation_outcome` | Assemble outcome from recorder | TurnRecorder | response_turn.py |
| 428-436 | `_TeamResponseRequest` | Team request carrier | — | response_turn.py (with team driver) |
| 439-456 | `ResponseRunnerDeps` | 14-collaborator wiring record | — | stay |
| 459-468 | `_PreparedResponseRuntime` | Resolved per-request runtime context | — | execution_preparation.py |
| 471-476 | `_InboxResponseOwnership` | Detached inbox recovery callbacks | — | new `inbox_response_tracker.py` |

## 4. `response_runner.py` — `ResponseRunner` (L479-3081)

| Lines | Method | Purpose | Calls | Candidate owner |
|---|---|---|---|---|
| 496-511 | `track_inbox_response` | Own detached inbox response task | asyncio, ownership | new `inbox_response_tracker.py` (public delegate stays) |
| 513-516 | `pending_inbox_response_count` | Unsettled inbox count | — | inbox tracker |
| 518-521 | `incomplete_inbox_responses_recoverable` | Recovery-proof flag | — | inbox tracker |
| 523-540 | `_finish_inbox_response_task` | Done-callback error handling | ResponseAdmissionRefusedError | inbox tracker |
| 542-570 | `drain_inbox_responses` | Graceful/bounded drain of inbox tasks | request_task_cancel | inbox tracker |
| 572-578 | `_client` | Require ready client | runtime | stay |
| 580-592 | `_log_delivery_failure` | Log delivery failure | logger | response_terminal.py (or stay) |
| 594-597 | `in_flight_response_count` | Busy-count property | — | stay |
| 599-602 | `_admission_gate` | Gate property | runtime | stay |
| 604-606 / 608-610 | `resume/refuse_pending_admissions` | Shutdown event set/clear | — | stay (or response_admission.py) |
| 612-628 | `wait_for_admission_or_shutdown` | Race admission-open vs shutdown | ResponseAdmissionGate | response_admission.py |
| 630-635 | `_show_tool_calls` | Tool visibility config | agents | stay |
| 637-659 | `_build_turn_recorder` | Seed recorder w/ Matrix run metadata | TurnRecorder, build_matrix_run_metadata | response_turn.py |
| 661-689 | `_persist_interrupted_turn` | Persist interrupted snapshot exactly once | ConversationStateWriter, persist_interrupted_replay_snapshot | history/interrupted_replay.py |
| 691-694 | `_ensure_recorder_interrupted` | Mark pending recorder interrupted | TurnRecorder | history/turn_recorder.py |
| 696-717 | `_persist_interrupted_recorder` | Mark + persist combo | above | history/interrupted_replay.py |
| 719-752 | `_persist_interrupted_recorder_off_loop` | Shielded off-loop persist w/ loss-tolerant error | background_tasks | history/interrupted_replay.py |
| 754-779 | `_record_stream_delivery_error` | Build interrupted replay from failed stream | streaming cleaners, `_split_delivery_tool_trace`, TurnRecorder | response_terminal.py |
| 781-783 / 785-787 / 789-791 | `has_active_response_for_target` / `active_thread_ids_for_room` / `wait_for_thread_response_idle` | Lifecycle-coordinator delegates | ResponseLifecycleCoordinator | stay |
| 793-822 | `finalize_user_stop` | Cancel live response then finalize under same lock, receipt-order tracking | StopManager, ResponseLifecycleCoordinator | user_stop_reconciliation.py |
| 824-834 | `reserve_waiting_human_message` | Notice reservation delegate | ResponseLifecycleCoordinator | stay |
| 836-849 / 851-864 | `_run_in_tool_context` / `_stream_in_tool_context` | Tool/execution-identity context wrappers | ToolRuntimeSupport, worker_routing | stay |
| 866-872 | `_active_response_event_ids` | Running response event ids per room | StopManager | stay |
| 874-939 | `_run_locked_response_lifecycle` | Admission loop -> in-flight count -> locked run -> early-placeholder error translation | ResponseAdmissionGate, ResponseLifecycleCoordinator | stay (admission/lock shell); placeholder translation -> response_terminal.py |
| 941-968 | `_finalize_early_placeholder_cancellation` | Terminalize early placeholder pre-settlement | DeliveryGateway, classify_cancel_source | response_terminal.py |
| 970-982 | `_request_with_locked_target` | Constrain request to lock-owning target | replace | response_lifecycle.py |
| 984-1006 | `_build_persist_response_event_id_effect` | Session-run persistence callback | ConversationStateWriter | conversation_state_writer.py |
| 1008-1019 | `_request_for_delivery` | Attach visible event id to request | replace | delivery_gateway.py |
| 1021-1037 | `_build_compaction_lifecycle` | Compaction notice adapter | MatrixCompactionLifecycle | delivery_gateway.py (class already lives there) |
| 1039-1073 | `_has_queued_forced_compaction` | Forced-compaction pre-check via storage | ConversationStateWriter, history.storage | history/storage.py (or conversation_state_writer.py) |
| 1075-1109 | `_refresh_model_history_after_lock` | Post-lock thread-history refresh | ConversationResolver | conversation_resolver.py |
| 1111-1131 | `_prepare_request_after_lock` | Refresh history + rebuild payload once locked | ResponsePayloadPreparer | response_payload_preparation.py |
| 1133-1146 | `_note_pipeline_metadata` | Timing notes | DispatchPipelineTiming | stay |
| 1148-1150 | `_correlation_id_for_request` | Correlation id resolution | — | ResponseRequest (property) |
| 1152-1158 | `_response_identity` | Build ResponseIdentity | DeliveryGateway type | stay (wiring) |
| 1160-1197 | `_agent_turn_context` | Build per-turn ResponseTurnContext incl. Matrix target item | `_matrix_message_target_item`, `_agent_has_matrix_messaging_tool` | response_turn.py |
| 1199-1230 | `_notify_interrupted_response_recoverable` | Decide+notify recoverable interrupted turn (note-suffix match) | streaming note constants, cancel-source helpers | sync_restart_retry.py |
| 1232-1252 | `_record_user_stop_handled` | Durable user-stop settlement before lock release | run_blocking_until_complete | user_stop_reconciliation.py |
| 1254-1332 | `_begin_locked_turn` | Locked-turn begin: retry currency, lock callback, source suppression, placeholder send, post-lock prep | TurnStore (via request cb), DeliveryGateway, `_prepare_request_after_lock` | response_lifecycle.py |
| 1334-1370 | `_sync_restart_retry_is_current` | Fail-closed retry currency vs persisted history | ConversationStateWriter, interrupted_source_needs_retry | sync_restart_retry.py |
| 1372-1425 | `_finalize_pre_delivery_terminal` | Pre-delivery terminal finalization (real pending-visible shape) | DeliveryGateway, response_terminal | delivery_gateway.py (plan Phase 5 #1) |
| 1427-1457 | `_settle_missing_delivery_outcome` | Settle missing outcome post-delivery-start | `_finalize_pre_delivery_terminal`, `_DeliveryProgress` | response_terminal.py (plan Phase 5 #2) |
| 1459-1490 | `_finalize_locked_outcome` | Lifecycle finalize w/ late-cancel -> terminal note conversion | ResponseLifecycle, cancel helpers | response_lifecycle.py (plan Phase 5 #3) |
| 1492-1607 | `_run_and_settle_locked_response` (noqa C901) | The outer settlement matrix: run attempt, settle on cancel/error, finalize lifecycle, recovery notify, user-stop, deferred outcome | `_run_cancellable_response`, response_terminal helpers, `_finalize_locked_outcome`, sync_restart + user-stop recorders | response_lifecycle.py (plan Phase 6 target) |
| 1609-1623 | `_build_lifecycle` | Build ResponseLifecycle | ResponseLifecycle | stay (wiring) |
| 1625-1661 | `_finalize_empty_prompt_locked` | Empty-prompt finalization | `_begin_locked_turn`, ResponseLifecycle, post_response_effects | response_lifecycle.py |
| 1663-1690 | `generate_team_response_helper` | Public team entry -> locked lifecycle | `_run_locked_response_lifecycle` | stay |
| 1692-1707 | `generate_response_for_empty_prompt` | Public empty-prompt entry | `_run_locked_response_lifecycle` | stay |
| 1709-2265 | `_generate_team_response_helper_locked` (noqa C901/PLR0915) | Entire team turn: placeholder, resolution-reason shortcut, memory/model context, model select, streaming decision, tool dispatch, session watch, recorder, streaming + blocking generation closures, streaming-error settle, post-response outcome | teams, DeliveryGateway, ResponseAttempt machinery, TurnRecorder, tool runtime, orchestrator | response_turn.py (team driver; possibly new sibling `team_response_turn.py`) |
| 2267-2304 | `_run_cancellable_response` | Adapter into ResponseAttemptRunner | response_attempt | stay (already extracted) |
| 2306-2343 | `prepare_response_runtime` | Resolve shared runtime context (target, session, model, tool dispatch) | config, ToolRuntimeSupport | execution_preparation.py |
| 2345-2436 | `generate_non_streaming_ai_response` | Blocking AI call: knowledge, enrichment, typing, cancel-persist | ai_response, knowledge access, typing | response_turn.py (blocking driver) |
| 2438-2554 | `generate_streaming_ai_response` | Streaming AI call + deliver_stream + interrupt persist | stream_agent_response, DeliveryGateway | response_turn.py (streaming driver) |
| 2556-2701 | `_process_and_respond` (noqa C901) | Non-streaming turn: runtime prep, session watch, recorder, generation, cancel -> visible note, deliver_final | `prepare_response_runtime`, lifecycle, DeliveryGateway | response_turn.py |
| 2703-2887 | `_process_and_respond_streaming` (noqa C901/PLR0915) | Streaming turn + StreamingDeliveryError terminal settlement | `generate_streaming_ai_response`, DeliveryGateway, response_terminal | response_turn.py |
| 2889-2899 | `generate_response` | Public agent entry -> locked lifecycle | `_run_locked_response_lifecycle` | stay |
| 2901-3081 | `_generate_response_locked` | Agent locked turn: memory/model context, auto-flush, streaming decision, recorder, generate closure, post-response outcome, settle | memory fns, should_use_streaming, `_process_and_respond(_streaming)` | response_turn.py (agent driver); memory queue -> memory/ |

## 5. Summary

**Totals**: `turn_controller.py` 2778 lines (25 module-level items + 48 methods); `response_runner.py` 3081 lines (20 module-level items + 53 methods/properties). Combined 5859 lines.

### turn_controller.py — largest domain-logic blocks (best extraction candidates)

1. **`_execute_response_action`** L1806-1987 (~182 lines) — response-path execution; -> new `response_action_executor.py`, with reject settlement -> visible_response_reconciliation.py and request assembly -> response_payload_preparation.py.
2. **`_prepare_dispatch`** L1101-1282 (~182 lines) — dispatch-context/envelope assembly; -> conversation_resolver.py (+ hook-suppression policy -> turn_policy.py).
3. **`_execute_interactive_selection`** L1305-1474 (~170 lines) — selection turn execution; -> interactive.py (with its three helpers L1476-1504).
4. **`_execute_router_relay`** L1585-1728 (~144 lines) — router relay; -> new `router_relay.py` together with module-level cluster L156-254 and `_router_handoff_extra_content`/`_router_handoff_with_attachments` L1506-1583 (~290 lines total).
5. **`_ready_voice_event`** L2452-2589 (~138 lines) + voice cluster L2390-2747 (~360 lines) — voice readiness/fallback; -> new `voice_ingress.py` + inbound_turn_normalizer.py (fallback construction L2591-2617, L2692-2747, L334-366).
6. **`_enqueue_for_dispatch`** L1010-1099 (~90 lines) — gate admission; core stays, metadata assembly -> prompt_ingress_reservation.py, relay detection -> ingress_validation.py.
7. **`_dispatch_prepared_text_like_ingress`** L827-910 (~84 lines) — echo->interactive->command->enqueue switchboard; -> text_ingress_dispatch.py.
8. **`_should_skip_router_before_shared_ingress_work`** L719-779 (~61 lines) — router pre-ingress skip policy; -> turn_policy.py. Plus mid-size moves: `_notify_command_target_not_ready` + `_dispatch_command_control_input` L933-1008 (~76) -> command_turn_executor.py; replay-guard adapters L496-547 (~52) -> dispatch_replay_guard.py; `_claim_live_turn` L2060-2077 -> turn_store.py; `_build_response_settlement_callbacks` L1777-1804 -> turn_store.py; `_finalize_dispatch_failure` L1744-1775 + `_router_handled_turn_outcome` L1730-1742 -> visible_response_reconciliation.py.

**Stays (coordination/wiring)**: `_client`, `reserve_prompt_ingress_order`, `_precheck_dispatch_event`, `handle_text_event`/`_handle_message_inner`/`_ingest_live_text_event` (turn-sequencing core), `_dispatch_text_message`, `handle_media_event`/`_handle_media_message_inner`, `_dispatch_special_media_as_text`, `_on_audio_media_message` (admission wiring), `_handle_edit_event`, `_dispatch_handoff`, `handle_coalesced_batch` (public seam), the two `_enqueue_*_for_dispatch` wrappers, `_append_live_event_with_timing`, `_resolve_text_event_with_ingress_timing`, `handle_interactive_selection` (commit/restore shell), and `TurnControllerDeps`.

### response_runner.py — largest domain-logic blocks (best extraction candidates)

1. **`_generate_team_response_helper_locked`** L1709-2265 (~556 lines, the single biggest block) — entire team turn incl. nested `generate_team_response`/`settle_team_streaming_delivery_error` closures; -> response_turn.py (documented owner of team/agent turn drivers) or new `team_response_turn.py`.
2. **`_process_and_respond_streaming`** L2703-2887 (~185 lines) — streaming turn + delivery-error settlement; -> response_turn.py.
3. **`_generate_response_locked`** L2901-3081 (~181 lines) — agent locked turn; -> response_turn.py; memory-persistence queue -> memory/.
4. **`_process_and_respond`** L2556-2701 (~146 lines) — blocking turn; -> response_turn.py.
5. **`_run_and_settle_locked_response`** L1492-1607 (~116 lines) — the "outer settlement matrix" the plan explicitly targets; -> response_lifecycle.py.
6. **`generate_streaming_ai_response`** L2438-2554 (~116) and **`generate_non_streaming_ai_response`** L2345-2436 (~91) — AI call + delivery glue; -> response_turn.py.
7. **`_begin_locked_turn`** L1254-1332 (~79 lines) — locked-turn begin protocol (placeholder, source suppression, post-lock prep); -> response_lifecycle.py, with `_prepare_request_after_lock` L1111-1131 -> response_payload_preparation.py and `_sync_restart_retry_is_current` L1334-1370 -> sync_restart_retry.py.
8. **Terminal-settlement cluster** L1372-1490 (~119 lines): `_finalize_pre_delivery_terminal` -> delivery_gateway.py, `_settle_missing_delivery_outcome` -> response_terminal.py, `_finalize_locked_outcome` -> response_lifecycle.py — exactly the plan's "five non-gateway constructions" (the other two are the team/agent blocking-cancellation branches inside items 1 and 4).

Smaller moves: inbox cluster L471-570 (~100) -> new `inbox_response_tracker.py`; interrupted-recorder cluster L637-752 + L754-779 (~145) -> history/interrupted_replay.py + response_terminal.py; enrichment/history helpers L166-288 (~123) -> execution_preparation.py (with `prepare_response_runtime` L2306-2343); `finalize_user_stop` + `_record_user_stop_handled` L793-822, L1232-1252 -> user_stop_reconciliation.py; `_notify_interrupted_response_recoverable` -> sync_restart_retry.py; `wait_for_admission_or_shutdown` -> response_admission.py; `_build_compaction_lifecycle`/`_request_for_delivery` -> delivery_gateway.py; `_has_queued_forced_compaction` -> history/storage.py; `_refresh_model_history_after_lock` -> conversation_resolver.py.

**Stays (coordination/wiring)**: `ResponseRequest`, `ResponseRunnerDeps`, public entries `generate_response`, `generate_team_response_helper`, `generate_response_for_empty_prompt`, `_run_locked_response_lifecycle` (admission->lock shell after placeholder translation leaves), `_client`, `_show_tool_calls`, `_run_in_tool_context`/`_stream_in_tool_context`, `_active_response_event_ids`, `_build_lifecycle`, `_response_identity`, `_note_pipeline_metadata`, `_run_cancellable_response`, the four lifecycle-coordinator delegates, `reserve_waiting_human_message`, `resume/refuse_pending_admissions`, `in_flight_response_count`, and thin public delegates over the extracted inbox tracker.

**Note on new modules**: only four new modules are proposed — `router_relay.py` (no existing module owns router relay delivery; turn_policy.py owns decisions, routing.py owns suggestion), `voice_ingress.py` (visible_voice_echo.py owns echo lifecycle, not readiness/fallback orchestration), `response_action_executor.py` (the PreparedDispatch->ResponseRequest bridge has no owner; putting it in response_runner.py would grow the file the plan shrinks), and `inbox_response_tracker.py` (background_tasks.py is a generic utility, not turn-domain). Everything else maps onto existing modules.
