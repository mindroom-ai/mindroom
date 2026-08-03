# Invariant Test Map (Phase 0)

Companion artifact to `2026-08-02-turn-pipeline-lifecycle-refactor.md`.
Inventoried at `a66c8bec` against the working tree.
Maps the plan's "existing behavior to cite rather than duplicate" list to concrete tests, and records where Phase 0 adds new invariant tests.

## Existing behavior to cite (already pinned)

### 1. Obligation persistence followed by crash and restart

- `tests/test_dispatch_obligations.py:271` `test_pending_row_survives_new_store_instance` — pending row re-read by a fresh store over the same dir.
- `tests/test_dispatch_obligations.py:444` `test_terminal_settlement_survives_restart_and_blocks_recreation` — terminal settlement persists; cold replay returns `ALREADY_TERMINAL`.
- `tests/test_dispatch_obligations.py:285` `test_aged_interrupted_command_journal_survives_pending_obligation_recovery` — restarted ledger cleanup keeps the command checkpoint a pending obligation still needs.
- `tests/test_dispatch_obligations.py:319` `test_startup_recovers_aged_completed_turn_before_cleanup` — restarted store+ledger settle obligations from durable turn truth before cleanup can erase it.
- `tests/test_dispatch_obligations.py:894` `test_persisted_work_can_be_scheduled_after_durable_acceptance` — persist happens before any background scheduling.
- `tests/test_dispatch_obligations.py:1566` `test_admission_persists_once_before_event_callback_execution` — admission writes exactly once; later callback only executes it.
- `tests/test_dispatch_obligations.py:921` `test_pending_duplicate_runs_first_durably_accepted_payload` and `:951` `test_failed_callback_retries_directly_without_later_sync_response` — replay executes the durably accepted payload, not the redelivery.

### 2. Downstream ownership followed by crash and restart

- `tests/test_dispatch_obligations.py:319` (deferred row via `mark_callback_deferred` + restarted store/ledger) and `:1353` `test_deferred_message_remains_pending_until_turn_store_is_terminal` — ordinary-turn deferred ownership.
- `tests/test_dispatch_obligations.py:485` `test_semantic_consumer_claim_is_durable_and_single_owner` — semantic-consumer claim survives store re-open; second consumer rejected.
- `tests/test_dispatch_obligations.py:1096` `test_replay_observes_consumer_claimed_before_interrupted_side_effect` — restarted runner sees the prior consumer claim.

### 3. Terminal turn persistence before obligation tombstoning

- `tests/test_turn_store.py:358` `test_terminal_turn_notifies_only_after_durable_write` — `on_terminal_turn_persisted` fires strictly after the ledger write lands.
- `tests/test_turn_store.py:146` `test_only_terminal_turn_notifies_exact_indexed_event_ids`.
- `tests/test_turn_settlement_retry.py:17` `test_terminal_turn_settlement_retries_autonomously` and `:51` `test_terminal_turn_settlement_succeeds_in_persistence_worker_without_retry_task`.
- `tests/test_dispatch_obligations.py:1353` / `:1330` `test_turn_store_terminal_truth_replaces_message_obligation` — tombstone follows terminal truth, not queue acceptance.

### 4. Voice readiness followed by same-sender text ordering

- `tests/test_coalescing.py:1324` `test_ready_text_waits_behind_unready_older_voice_lane_slot` — receipt order `["$voice", "$text"]`.
- `tests/test_coalescing.py:940` `test_voice_readiness_delay_combines_backlog_in_receipt_order` and `:1560` `test_batch_order_follows_lane_receipt_order_not_readiness_order`.
- `tests/test_live_message_coalescing.py:1746` `test_voice_ready_release_combines_same_thread_backlog_in_receipt_order`, `:1947` `test_voice_before_text_uses_stable_admission_key`, `:2000` `test_text_before_voice_uses_stable_admission_key`.
- `tests/test_ingress_lanes.py:786` `test_hung_voice_download_fallback_releases_later_sender_slot`.
- `tests/test_bot_media_dispatch.py:236` `test_audio_dispatch_resolves_thread_key_before_admit_and_defers_stt`.

### 5. Distinct requesters batch independently but share response serialization

- `tests/test_live_message_coalescing.py:888` `test_different_senders_dispatch_separately`; `:1016` `test_same_sender_different_threads_dispatch_separately`; `tests/test_coalescing.py:1600`/`:1624`; `tests/test_ingress_lanes.py:125`.
- `tests/test_live_message_coalescing.py:1315` `test_active_follow_ups_share_target_gate_across_requesters` — shared serialization across requesters.
- `tests/test_ingress_lanes.py:307` `test_busy_conversation_queues_any_sender_into_one_combined_follow_up`.
- `tests/test_queued_message_notify.py:1245` `test_generate_response_waits_for_lock_before_starting_placeholder_lifecycle`.

### 6. Commands bypass ordinary coalescing

- `tests/test_live_message_coalescing.py:1167` `test_command_executes_immediately_while_text_batch_debounces`.
- `tests/test_live_message_coalescing.py:1202` `test_command_during_media_debounce_executes_immediately`.
- `tests/test_live_message_coalescing.py:1810` `test_command_executes_immediately_despite_unresolved_ingress`.

## New Phase 0 invariant tests

| Invariant | Prior coverage | New test file |
| --- | --- | --- |
| Pending turn claim acquired before text/media normalization, released on every non-admission path | Partial (`test_bot_media_dispatch.py:324/:360/:389/:421`, `test_turn_dispatch_pipeline.py:1401`, `test_turn_controller_focused.py:1211/:1249`, `test_turn_store.py:390/:460/:545/:563`) | `tests/test_pending_turn_claim_invariants.py` |
| Persisted replay text byte-stable relative to the live model-facing prompt | Adjacent only (`test_partial_reply_context.py:326`, `test_history_prepare_integration.py:150/:178/:222`, `test_turn_store.py:1696/:1731`) | `tests/test_replay_text_stability.py` |
| Dynamic-tool continuation does not call final delivery at the gateway seam between attempts | Driver-level only (`test_response_turn.py:466/:633/:948/:1132`) | `tests/test_dynamic_tool_continuation_delivery.py` |
| Cancellation-note markers compatible with `stale_stream_cleanup.py` recovery matching | Partial (`test_stale_stream_cleanup.py:1926/:2114`, `test_streaming_behavior.py:753/:760`) | `tests/test_cancellation_note_markers.py` |
| `settle_pending_from_turn_store` does not compact a pending callback whose body has not run | Adjacent only (`test_dispatch_obligations.py:1353/:1378`) | `tests/test_settle_pending_eligibility.py` |
| Lifecycle-lock table eviction cannot evict a target with a live queued signal or active lock | Missing (implementation at `response_lifecycle.py:163-179`) | `tests/test_lifecycle_lock_eviction.py` |
| User-stop visible reconciliation and durable terminal reconciliation converge (live path) | Recovery-side only (`test_bot_reactions_approvals.py:1189/:1246/:1300/:1360`, `test_turn_store.py:165/:206/:258/:292`) | `tests/test_user_stop_convergence.py` |

## Injectable controls and restart harnesses (reuse, do not extend)

- Gate-level: `CoalescingGate(dispatch_batch=..., debounce_seconds=lambda: X, is_shutting_down=...)` (`src/mindroom/coalescing.py:186-201`); explicit `asyncio.Event` readiness gates; `FakeMonotonicClock` (`tests/test_coalescing.py:314`).
- Bot-level: `_make_bot(tmp_path, debounce_ms=...)` (`tests/test_live_message_coalescing.py:107-155`, `tests/test_ingress_lanes.py`); `drain_coalescing(*bots)` (`tests/conftest.py:420`).
- Store/ledger restart: `_store(tmp_path)` re-instantiation and `_reset_handled_turn_ledger_runtime()` (`tests/test_dispatch_obligations.py`, `tests/test_turn_store.py:2355`).
- Full-bot restart: second `AgentBot` over the same `tmp_path` (`tests/test_bot_reactions_approvals.py:1227/:1283/:1344`).
- Conftest seam installers: `replace_turn_controller_deps` (:1353), `replace_turn_store_deps` (:1283), `replace_delivery_gateway_deps` (:1293), `replace_response_runner_deps` (:1305), `replace_edit_regenerator_deps` (:1316), `replace_turn_policy_deps` (:1258), `install_generate_response_mock` (:1588), `install_send_response_mock` (:1536), `install_edit_message_mock` (:1627), `install_shutdown_drain_mocks` (:1518), `make_matrix_client_mock` (:716).
- Stale-stream harness: `_run_cleanup` scripted client (`tests/test_stale_stream_cleanup.py`); focused controller harness `_Harness`/`_build_harness` (`tests/test_turn_controller_focused.py:267-293`).
