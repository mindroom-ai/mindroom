# Coalescing redesign — Phase 0 test triage (issue #1241)

This table triages every test in the four ingress/coalescing test files (plus the six tests PR #1240 adds) against the first-principles spec P1–P5 from issue #1241.
No test is deleted, moved, or rewritten until this table is signed off.

## Layer key

- **L1 ingress validation** — trust/effective-requester validation, handled-event-id dedup, trusted router-echo drop, anti-spoofing, command routing out of the pipeline, sidecar hydration.
- **L2 conversation resolution** — canonical conversation id decided exactly once (thread id when threaded, room-level chain otherwise), per-(room, sender) FIFO for late-ready admissions (voice/STT).
- **L3 conversation inbox** — per-conversation debounce + upload grace (hard-capped) when idle; conversation-scoped queue → one combined follow-up when busy; batch construction.
- **L4 response runner** — execution ownership, running-response tracking, shutdown drains (graceful = await, bounded = cancel + cleanup-exactly-once).

## Verdict key

- **KEEP(P#, L#)** — real invariant; the test survives (possibly mechanically rehomed or reworded) in that layer's suite.
- **MOVE(L#)** — real behavior with the wrong home today; the test logic moves to that layer's suite.
- **REPLACE** — the behavior is superseded; a named replacement test covers the surviving invariant.
- **DELETE** — accidental contract, or the test exercises machinery being deleted; reason given.
- **INVERT** — asserts cross-conversation blocking; the inverted test asserts independence (decision 1).
- **OUT-OF-SCOPE** — not about ingress/coalescing; keep unchanged.

Rows marked ⚠A–⚠G reference the sign-off questions below.

## Summary

| Verdict | Count |
|---|---|
| KEEP | 143 |
| REPLACE | 37 |
| DELETE | 29 |
| MOVE | 19 |
| INVERT | 1 |
| OUT-OF-SCOPE | 9 |
| **Total** | **238** |

## Open questions / accepted degradations — RESOLVED (signed off 2026-06-11)

All seven questions below were approved as proposed, with two amendments:

- **A** is accepted with one replacement behavior: a command that semantically targets a conversation that does not exist yet (e.g. a thread-reply `!command` whose parent root is still pending ingress) must fail visibly — a loud no-op telling the user the target is not ready — never silently act on the wrong scope and never silently drop. The deleted ordering tests are replaced by that visible-failure test. This new behavior lands in Phase 2 (L2), not Phase 1.
- **F** is accepted with observability: when a late-resolving follow-up misses the combined follow-up and becomes its own turn, emit a structured log event (`follow_up_missed_combined_turn`) so the frequency of the degradation is measurable.

The 29 DELETEs and the INVERT are approved as written.
Phase 1 remains strictly behavior-preserving for the KEEP invariants; A's visible-failure behavior and B's complementary room-mode test land in Phase 2 where their machinery lives.

## Original questions (as presented for sign-off)

- **A — Commands stop ordering against conversational turns.**
  With commands routed out at L1, a `!command` executes immediately even when posted after not-yet-dispatched conversational messages (`test_child_command_waits_for_older_queued_room_root_parent`, `test_command_after_pending_voice_waits_for_same_resolved_thread`).
  The command-flush side effect also disappears: a command mid-batch no longer force-flushes the pending batch; the batch simply continues its own debounce (no loss, no early flush).
  Proposed: accept — commands are control inputs, not conversation content.
- **B — Room-mode conversation identity needs an explicit L2 carve-out.**
  For a room-mode agent, events carrying thread relations must still resolve to the room-level chain (`test_room_mode_voice_notice_survives_until_queued_dispatch_owns_it`, `test_room_mode_voice_burst_dispatches_as_one_turn`).
  Corollary: `test_room_level_messages_do_not_coalesce` encodes threaded-agent semantics (each room root starts its own conversation); for a room-mode agent the same two quick texts are one conversation and SHOULD coalesce per P1, which needs a new complementary test.
  Proposed: L2 rule is "thread id when threaded, room-level chain otherwise, where 'threaded' is evaluated against the agent's conversation mode".
- **C — In-window unready same-sender ingress may hold a conversation's open burst.**
  The per-sender FIFO holds later same-sender admissions behind an unready event, and an open burst window waits for in-window unready same-sender ingress (it may belong to this burst), bounded by readiness plus the upload-grace hard cap (`…unresolved_media_reservation`, `…waits_for_later_same_owner_reservation_inside_window`, `…resolving_to_different_thread_waits_then_splits`, and the voice-readiness family).
  Proposed: keep the hold with the hard-cap bound; the receive-time window itself is never widened.
- **D — Cross-sender dispatch interleaving within a conversation is no longer totally ordered.**
  Receipt order is guaranteed per sender only; a machine event (no debounce) may dispatch while another sender's burst is still debouncing, and that burst then lands as a P3 follow-up (`test_bypass_preserves_fifo_order_behind_existing_normal_work`, `test_automation_and_relay_source_kinds_dispatch_solo_with_human_neighbor`).
  Proposed: accept — issue requirement 3 is receipt-order per sender, and P5 forbids enforcing more.
- **E — Simultaneous idle double-fires degrade from one merged turn to turn + follow-up.**
  Already decided for scheduled fires (`test_overlapping_scheduled_checkins_coalesce`); the same acceptance extends to the zero-debounce deferred voice burst (`test_deferred_room_scope_voice_burst_stays_one_turn_under_null_thread_key`).
  Proposed: accept both.
- **F — A follow-up still resolving when its conversation's response completes may not join the combined follow-up.**
  The guarantee becomes no-loss plus per-sender order; it may land as its own next turn instead of joining (`test_later_slow_thread_lookup_active_follow_up_joins_open_backlog`, `test_unresolved_reservation_wait_keeps_debounce_gaps`).
  Proposed: accept — the wake-generation/backlog-freeze machinery that guaranteed joining is deleted.
- **G — STT-failure batch splitting at the gate is dropped.**
  Voice failures are handled at L2 (visible audio/text fallback, or loud rejection without a guessed key); surviving texts follow normal L2 identity and L3 batching instead of the gate's "failure splits surviving room roots" behavior (`test_failed_room_voice_does_not_coalesce_surviving_room_roots`).
  Proposed: accept; replacement coverage is the existing voice-fallback family.

Decided items applied throughout (not open): decision 1 (INVERT `test_thread_followups_wait_behind_first_turn_root_in_flight` — the inverted test asserts thread replies dispatch without waiting on the room-root turn's in-flight dispatch; same-thread response serialization remains the L4 response lock's job, never ingress), decision 2 (conversation-scoped busy queue), decision 3 (machines skip debounce, no source-kind coalescing rules).

## tests/test_coalescing.py (54 tests)

| Test | Line | What it actually asserts | P | Verdict | Target / reason |
|---|---|---|---|---|---|
| test_reserve_order_uses_local_monotonic_receipt_time | 167 | Reservations stamp local monotonic receipt time; later reservation gets larger order | P5 | REPLACE | Re-express on L2 per-(room,sender) FIFO receipt stamping |
| test_admit_rejects_released_reservation | 188 | Admitting into a released reservation raises; recreates no work | P5 | REPLACE | L2 FIFO: late admit into a released slot must not recreate work |
| test_release_order_removes_unadmitted_reservation_from_owner_work | 209 | Order-book owner-work clears on release; release idempotent | — | DELETE | Order-book internals; die with coalescing_order.py |
| test_admit_with_reservation_keeps_wall_clock_enqueue_time | 231 | Queued entry keeps monotonic receipt_time separate from wall-clock received_at | — | DELETE | Reservation→gate-queue field plumbing; both structures deleted |
| test_reservation_receipt_time_bounds_debounce_claim_window | 262 | Out-of-order admissions dispatch in receipt order; late admit doesn't widen window | P1,P5 | REPLACE | Re-express on L2 FIFO + L3 receive-time windows |
| test_command_barrier_does_not_widen_receive_time_debounce_window | 308 | Late prompts + command dispatch as three batches in receipt order | P1 | DELETE | In-gate command barrier dies (commands→L1); window invariant covered by row above |
| test_active_follow_up_source_kind_is_not_coalescing_exempt | 366 | Active-follow-up source kind is not coalescing-exempt | P3 | DELETE | Follow-up-as-source-kind dies; busy state becomes L3 inbox state |
| test_batch_construction_does_not_close_mixed_solo_metadata | 373 | build_coalesced_batch raises on solo+normal mix without metadata close | — | DELETE | requires_solo_batch segment machinery has no production producer |
| test_single_prepared_event_handoff_synthesizes_canonical_thread_relation | 394 | Handoff synthesizes m.thread relation from batch canonical key | P5 | KEEP(P5, L3) | Canonical conversation id governs dispatch relation; rehome to batch-construction suite |
| test_room_level_messages_do_not_coalesce | 427 | Two quick room-level texts dispatch as separate batches | P1,P4 | MOVE(L2) ⚠B | Threaded agents: each room root starts its own conversation; room-mode complement needed |
| test_room_level_messages_do_not_coalesce_during_upload_grace | 455 | Room-level roots stay separate with grace enabled | P4 | DELETE | Room-scope merge special case; separation structural via L2 identity |
| test_room_level_text_waits_for_late_media_upload_grace | 483 | Room-level text + image within grace form one batch | P2 | KEEP(P2, L3) | Attachment joins its utterance via upload grace |
| test_upload_grace_waits_for_same_window_unresolved_media_reservation | 508 | Grace holds text until reserved unresolved media admits | P2 | REPLACE ⚠C | Re-express on FIFO; hard-capped per P2 |
| test_upload_grace_barrier_does_not_wait_for_later_unresolved_reservation | 544 | Barrier after grace text doesn't wait for later reservation | — | DELETE | Commands→L1; later-ingress non-blocking is structural in FIFO |
| test_upload_grace_does_not_flatten_late_solo_ready_segment | 582 | Solo-metadata event during grace dispatches as own segment | — | DELETE | Solo-segment machinery has no production producer |
| test_upload_grace_does_not_flatten_claimed_solo_ready_segment | 624 | Claimed solo event dispatches as own segment | — | DELETE | Same: solo segment machinery, deleted |
| test_unresolved_solo_ready_event_dispatches_solo_without_metadata_close | 664 | Late solo metadata splits batch; close not called | — | DELETE | Solo-split machinery; metadata passthrough covered by turn-controller tests |
| test_voice_class_text_does_not_wait_for_upload_grace | 707 | Lone voice transcript dispatches despite nonzero grace | P2 | KEEP(P2, L3) | Grace is for attachments, not voice text |
| test_thread_messages_inside_debounce_window_still_coalesce | 738 | Two thread messages in window form one batch | P1 | KEEP(P1, L3) | Core burst coalescing |
| test_threaded_debounce_uses_trailing_quiet_time | 763 | Second message extends the quiet deadline | P1 | KEEP(P1, L3) | Trailing-debounce semantics |
| test_active_follow_up_backlog_ignores_debounce_gaps_after_idle | 791 | Multi-sender follow-ups flush as one ordered batch | P3 | KEEP(P3, L3) | Busy queue yields exactly one combined ordered follow-up |
| test_same_target_normal_gate_waits_behind_older_active_backlog | 850 | Later same-thread message dispatches after older active backlog | P3 | REPLACE | Absorbed: conversation-scoped busy queue merges it into the combined follow-up |
| test_different_thread_normal_gate_does_not_wait_behind_older_active_backlog | 919 | Other-thread message dispatches while this thread's backlog waits | P4 | KEEP(P4, L3) | Conversation independence |
| test_unresolved_reservation_wait_keeps_debounce_gaps | 984 | Events behind unready blocker dispatch per receive-time gaps | P1,P5 | REPLACE ⚠F | Burst boundaries respected at admission; once busy, P3 merging governs |
| test_in_flight_unresolved_reservations_obey_debounce_after_admission | 1025 | Messages reserved during dispatch later dispatch per-gap | P3 | REPLACE | Received-while-busy → one combined follow-up per P3 |
| test_voice_readiness_delay_does_not_extend_receive_time_debounce | 1074 | Slow STT doesn't widen the receive-time window | P1,P5 | KEEP(P1, L3) | Receive-time windows independent of readiness delay |
| test_failed_older_owner_admission_wakes_newer_thread_gate | 1115 | Failed cross-thread admission doesn't deadlock newer work | P4 | REPLACE | Successor: failed late-ready admission releases its L2 FIFO slot |
| test_bounded_shutdown_marks_internal_drain_failure_incomplete | 1157 | Internal drain failure reports completed=False | — | MOVE(L4) | Runner-owned bounded shutdown |
| test_bounded_shutdown_times_out_stuck_in_flight_dispatch | 1181 | Bounded drain cancels stuck dispatch, reports unsafe | — | MOVE(L4) | Runner-owned bounded shutdown |
| test_bounded_drain_does_not_wait_forever_on_external_dispatch_gate | 1216 | Bounded drain doesn't hang on follow-up idle gate | — | REPLACE | wait_until_dispatch_allowed hook dies; L4 bounded drain must not hang on a busy conversation |
| test_bounded_shutdown_closes_metadata_for_abandoned_ready_work | 1267 | Abandoned work metadata closes exactly once | — | MOVE(L4) | Cleanup-exactly-once is explicit L4 contract |
| test_drain_all_waits_for_order_reservation_to_admit | 1302 | Graceful drain waits for outstanding reservation, then dispatches | P5 | REPLACE | L4 graceful drain awaits unready FIFO slots |
| test_debounce_waits_for_later_same_owner_reservation_inside_window | 1341 | Debounce holds for in-window unresolved same-sender ingress | P1,P5 | REPLACE ⚠C | In-window same-sender unready event joins the utterance |
| test_debounce_does_not_wait_for_later_reservation_outside_window | 1375 | Out-of-window reservation doesn't delay flush | P1 | REPLACE | Out-of-window ingress never delays an already-quiet utterance |
| test_debounce_still_releases_prompt_when_command_barrier_arrives | 1409 | Command cuts a long debounce short; separate dispatches | — | REPLACE ⚠A | Replacement: command executes at L1 immediately, regardless of inbox debounce |
| test_command_barrier_does_not_wait_for_unresolved_reservation_after_barrier | 1433 | Command doesn't wait on later unresolved reservation | — | DELETE | Commands never enter the inbox |
| test_bypass_barrier_does_not_wait_for_unresolved_reservation_after_barrier | 1472 | Solo-bypass doesn't wait on later reservation | — | DELETE | Solo-bypass machinery has no production producer |
| test_front_command_does_not_wait_for_later_unresolved_reservation | 1515 | Front command dispatches immediately | — | DELETE | Commands→L1; subsumed by L1 routing test |
| test_child_command_waits_for_older_queued_room_root_parent | 1547 | Thread !command waits behind its queued room-root parent | — | DELETE ⚠A | Commands leave the pipeline at L1; command-vs-turn ordering dropped |
| test_front_bypass_does_not_wait_for_later_unresolved_reservation | 1573 | Front solo-bypass dispatches immediately | — | DELETE | Solo-bypass machinery, deleted with order book |
| test_claim_count_stops_before_unresolved_older_reservation | 1609 | Ready event stays queued behind unready older slot | P5 | REPLACE | FIFO: never reorder ready work past an unready same-sender slot |
| test_different_canonical_threads_do_not_serialize_after_admission | 1642 | Thread-b dispatches while thread-a dispatch runs | P4 | KEEP(P4, L3) | Conversation independence for same sender |
| test_zero_ready_claim_clears_claimed_state_and_wakes_waiters | 1677 | None-resolving admission clears claimed state | P4 | REPLACE | Dropped admission releases its FIFO slot with no residue |
| test_partial_ready_failure_dispatches_ready_events_and_clears_claim | 1711 | Burst with one failing member dispatches survivors | P1 | KEEP(P1, L3) | A dropped member must not poison the utterance |
| test_cancelled_resolve_requeues_claimed_admissions | 1744 | Cancelling drain mid-resolve requeues claimed admission | — | DELETE | Claim/requeue internals; L4 redefines cancellation as cancel + cleanup-exactly-once |
| test_upload_grace_requeue_removes_admissions_from_claimed_state | 1778 | GRACE phase keeps claimed_admissions empty | — | DELETE | GatePhase/claimed-state internals die |
| test_failed_room_media_signal_does_not_merge_surviving_room_text_roots | 1805 | Dropped media doesn't merge surrounding room texts | P4 | DELETE | Room-scope segment merge special case (decided drop) |
| test_same_window_reservation_resolving_to_different_thread_waits_then_splits | 1842 | In-window unresolved event holds debounce, splits to resolved key | P1,P5 | REPLACE ⚠C | In-window unready same-sender ingress holds burst, then routes to its own conversation |
| test_multi_segment_claim_remains_visible_until_last_segment_finishes | 1883 | claimed_admissions stays populated across split segments | — | DELETE | Segment-claim internals; L4 owns running tracking |
| test_root_in_flight_child_followup_reservations_obey_debounce | 1927 | Follow-ups during root response dispatch per-debounce-gap | P3 | REPLACE | One combined busy-queue follow-up per P3 |
| test_batch_order_follows_ingress_reservation_order_not_admission_order | 1977 | Batch orders by receipt despite reversed admission | P5 | REPLACE | Core P5 re-expressed on the FIFO |
| test_messages_in_different_rooms_do_not_coalesce | 2019 | Cross-room independence | P4 | KEEP(P4, L3) | Survives unchanged |
| test_messages_in_different_threads_do_not_coalesce | 2044 | Cross-thread independence | P4 | KEEP(P4, L3) | Survives unchanged |
| test_drain_all_flushes_pending_debounced_work_and_idles_gate | 2069 | Graceful drain flushes pending work immediately | — | MOVE(L4) | Runner-owned graceful shutdown flushes pending inbox turns |

## tests/test_live_message_coalescing.py — part 1 (lines ≤ 3650, 70 tests)

| Test | Line | What it actually asserts | P | Verdict | Target / reason |
|---|---|---|---|---|---|
| test_coalescing_config_rejects_removed_enabled_flag | 205 | Config validation rejects removed coalescing.enabled toggle | P1 | KEEP(P1, L3) | Config schema for debounce/grace knobs survives |
| test_single_message_dispatches_after_debounce_window | 443 | One text dispatches once after debounce, not before | P1 | KEEP(P1, L3) | Core trailing debounce |
| test_two_rapid_text_messages_dispatch_one_combined_turn | 477 | Two quick same-thread texts merge; both event ids handled | P1 | KEEP(P1, L3) | Burst→one turn |
| test_two_rapid_text_messages_forward_prompt_map_to_dispatch | 525 | Coalesced handoff carries per-source-event prompt map | P1 | KEEP(P1, L3) | Batch→runner handoff payload contract |
| test_image_and_text_coalesce_into_single_dispatch | 559 | Thread image + text become one dispatch with media event | P1,P2 | KEEP(P2, L3) | Attachment joins utterance |
| test_room_root_text_and_image_coalesce_into_single_dispatch | 607 | Room-level text and image share one batch | P1,P2 | KEEP(P2, L3) | Room-level chain is a conversation |
| test_text_first_image_during_debounce_dispatches_without_upload_grace_delay | 643 | Media within debounce skips the extra grace wait | P2 | KEEP(P2, L3) | Grace only when media still expected |
| test_text_first_image_during_grace_dispatches_once | 683 | Text-only batch held through grace so late image joins | P2 | KEEP(P2, L3) | Upload grace core |
| test_text_first_multiple_images_during_grace_dispatch_once | 735 | Multiple images during grace merge into one batch | P2 | KEEP(P2, L3) | Upload grace core |
| test_text_during_upload_grace_flushes_pending_batch_and_starts_new_turn | 789 | New plain text during grace flushes held batch; becomes second turn | P2 | KEEP(P2, L3) | Grace holds for media only |
| test_image_after_grace_expires_dispatches_as_second_batch | 841 | Image after grace expiry is a separate second turn | P2 | KEEP(P2, L3) | Grace boundary |
| test_different_senders_dispatch_separately | 887 | Two senders' room messages never share a batch | P1 | KEEP(P1, L3) | Per-(conversation, sender) debounce isolation |
| test_build_coalesced_batch_keeps_normalized_voice_out_of_media_events | 927 | Voice synthetic text contributes prompt, not media_events | P1 | KEEP(P1, L3) | Pure batch builder survives |
| test_build_coalesced_batch_preserves_fifo_order_with_synthetic_events | 947 | Batch keeps queue order despite server-timestamp disagreement | P5 | KEEP(P5, L3) | Receipt-order composition |
| test_build_coalesced_batch_prefers_media_source_kind_over_text_primary | 971 | Mixed batch source_kind is image despite text primary | P2 | KEEP(P2, L3) | Batch metadata precedence |
| test_build_coalesced_batch_prefers_voice_source_kind_over_media_and_text | 989 | Voice wins batch source_kind over image and text | P2 | KEEP(P2, L3) | Batch metadata precedence |
| test_same_sender_different_threads_dispatch_separately | 1015 | Same sender, two threads → two independent dispatches | P4 | KEEP(P4, L2) | Thread = conversation identity |
| test_room_message_and_plain_reply_to_known_thread_do_not_coalesce_together | 1056 | Plain reply resolving to a thread doesn't batch with room message | P4 | KEEP(P4, L2) | Conversation id decided exactly once |
| test_plain_reply_with_unproven_root_is_not_admitted_under_guessed_key | 1105 | Unresolvable thread root raises; nothing admitted under guessed key | P4 | KEEP(P4, L2) | Resolver must never guess conversation id |
| test_command_mid_batch_flushes_pending_then_processes_command | 1166 | Command flushes pending batch first, then dispatches solo | — | REPLACE ⚠A | Commands→L1; new test: command bypasses inbox, batch dispatches independently |
| test_command_flush_does_not_leave_stale_timer_for_next_message | 1210 | Command-flush leaves fresh debounce timer for next message | — | DELETE | Stale-timer cleanup of command-flush machinery; barrier removed |
| test_command_during_upload_grace_flushes_immediately | 1272 | Command bypasses upload-grace timer | — | REPLACE ⚠A | Commands never enter inbox, so grace can't hold them |
| test_already_queued_command_barrier_flushes_normal_without_debounce | 1320 | In-gate queued command flushes older work without debounce | — | DELETE | In-gate command-barrier machinery; commands exit at L1 |
| test_messages_during_active_response_wait_and_batch_after_completion | 1347 | Follow-ups during in-flight response buffer, then one combined batch | P3 | KEEP(P3, L3) | Busy→queue→one combined follow-up |
| test_active_follow_ups_share_target_gate_across_requesters | 1433 | Two requesters' follow-ups share one queue, requester kept per event | P3 | KEEP(P3, L3) | Decided: conversation-scoped busy queue |
| test_slow_thread_lookup_active_follow_up_stays_before_later_follow_up | 1481 | Slow-resolving older follow-up precedes later one in combined batch | P3,P5 | KEEP(P3, L3) | Combined follow-up in receipt order despite late resolution |
| test_later_slow_thread_lookup_active_follow_up_joins_open_backlog | 1569 | Follow-up still resolving at response end joins same combined batch | P3 | REPLACE ⚠F | Guarantee becomes no-loss + order; may land as own next turn |
| test_active_follow_up_owner_includes_later_media_payload | 1657 | Combined follow-up carries later media and queued_messages prompt | P3,P2 | KEEP(P3, L3) | Combined-payload content survives |
| test_in_flight_command_barrier_flushes_buffered_normal_without_debounce | 1790 | Command queued during dispatch wakes and flushes buffered work | — | DELETE | In-gate barrier wake machinery; commands exit at L1 |
| test_command_during_active_dispatch_preserves_fifo_order | 1832 | Command keeps in-gate FIFO slot among buffered messages | — | DELETE | In-gate command ordering machinery |
| test_room_scope_text_then_voice_live_debounce_coalesces_receive_time | 1871 | Room-scoped text + ready voice in window share one batch | P1,P5 | KEEP(P1, L3) | Mixed-modality burst is one turn |
| test_room_scope_text_then_pending_voice_waits_for_voice_class_admission | 1903 | Text batch waits for pending unready voice admitted in window | P1,P5 | KEEP(P5, L3) ⚠C | Late-ready STT joins its burst via FIFO + inbox |
| test_late_same_thread_text_does_not_join_expired_debounce_while_waiting_on_voice_ready | 1951 | Text after expiry doesn't join batch stalled on voice readiness | P1 | KEEP(P1, L3) | Burst window is bounded |
| test_front_command_does_not_wait_for_later_unresolved_voice | 2002 | Front command dispatches without waiting on later unresolved voice | — | REPLACE ⚠A | Subsumed by L1 command routing |
| test_interrupted_claimed_admission_is_retried_on_next_drain | 2050 | Cancelled drain doesn't lose claimed admissions; next drain dispatches | — | MOVE(L3) | Becomes: cancelled inbox dispatch never drops queued messages |
| test_voice_handoff_buffers_same_thread_followups_while_in_flight | 2093 | Follow-up on dispatched voice's thread buffers until completion | P3 | KEEP(P3, L3) | Busy-conversation queueing |
| test_voice_before_text_uses_stable_admission_key | 2148 | Pending voice + typed follow-up dispatch as one ordered batch | P1,P5 | KEEP(P5, L3) | Late-ready voice keeps receipt order, shares burst |
| test_text_before_voice_uses_stable_admission_key | 2200 | Queued text waits; later pending voice joins same ordered batch | P1,P5 | KEEP(P5, L3) | Same invariant, opposite arrival order |
| test_same_thread_followup_after_voice_claim_stays_on_admitted_gate | 2252 | Follow-up during voice claim stays on same admission key | — | DELETE | Claimed-admission key-stability machinery; L2 decides id once |
| test_plain_reply_voice_resolution_batches_related_text | 2310 | Text behind unresolved earlier voice waits, merges into one batch | P5 | KEEP(P5, L3) | Per-sender FIFO + burst merge |
| test_text_first_waits_for_plain_reply_voice_ready_during_debounce | 2361 | Debouncing text holds for later voice that belongs to it | P1,P5 | KEEP(P1, L3) ⚠C | Burst unity with late-resolving voice |
| test_later_different_thread_voice_does_not_hold_earlier_text | 2414 | Pending voice in another thread doesn't delay earlier text | P4 | KEEP(P4, L3) | Conversation independence |
| test_failed_room_voice_does_not_coalesce_surviving_room_roots | 2467 | Failed STT admission splits surrounding room texts | — | REPLACE ⚠G | STT failure handled at L2; survivors batch normally |
| test_command_after_pending_voice_waits_for_same_resolved_thread | 2505 | Command waits behind earlier unresolved voice in same thread | — | DELETE ⚠A | Commands→L1 execute immediately; ordering dropped |
| test_voice_admissions_resolving_to_different_threads_do_not_coalesce | 2556 | Voices resolving to different threads dispatch separately | P4 | KEEP(P4, L2) | Conversation identity separates turns |
| test_pending_thread_voice_does_not_capture_unrelated_thread_text | 2615 | Pending voice in thread A doesn't delay or absorb thread B text | P4 | KEEP(P4, L3) | Conversation independence |
| test_room_scope_voice_burst_coalesces_under_null_thread_key | 2675 | Two ready room-scope voices from one sender form one turn | P1 | KEEP(P1, L3) | Voice burst = one utterance |
| test_deferred_room_scope_voice_burst_stays_one_turn_under_null_thread_key | 2712 | Two deferred voices at zero debounce still combine into one turn | P1,P5 | REPLACE ⚠E | Claim machinery gone; may degrade to turn + follow-up at zero debounce |
| test_enqueue_for_dispatch_returns_while_drain_dispatch_blocks | 2764 | Ingress enqueue returns promptly while a drain dispatch blocks | P4 | KEEP(P4, L3) | Non-blocking ingress→inbox handoff |
| test_automation_source_kinds_are_coalescing_exempt | 2825 | scheduled/hook/hook_dispatch classified as coalescing-exempt | P1 | KEEP(P1, L3) | Machines skip debounce; predicate survives renamed |
| test_coalescing_exempt_source_kinds_bypass_gate | 2838 | Hook-originated events dispatch immediately without debounce | P1 | KEEP(P1, L3) | Non-human sender skips debounce |
| test_pending_dispatch_policy_preserves_active_followup_without_bypassing_modality | 2871 | dispatch_policy_source_kind stays metadata; voice stays coalescible | — | DELETE | Follow-up-as-source-kind dies; invariant survives via envelope tests (6299–6374) |
| test_untrusted_source_kind_content_does_not_bypass_or_promote | 2918 | Spoofed source_kind neither bypasses debounce nor promotes | P1 | KEEP(P1, L1) | Anti-spoofing is L1 |
| test_bypass_preserves_fifo_order_behind_existing_normal_work | 2965 | Hook event dispatches solo but in receipt order between user messages | P1,P5 | REPLACE ⚠D | Per-sender order only; machine turns may interleave with another sender's debouncing burst |
| test_room_mode_voice_queued_notice_is_solo_barrier_before_nearby_normal_message | 2994 | requires_solo_batch keeps voice notice solo; reservation kept | — | MOVE(L3) | Per-event handoff metadata survives; solo-turn honored at batch handoff |
| test_overlapping_scheduled_checkins_coalesce | 3043 | Second scheduled fire buffers behind in-flight first, dispatches after | — | REPLACE ⚠E | Decided: busy queue makes a mid-response second fire the follow-up |
| test_prepare_for_sync_shutdown_waits_for_active_flush_task | 3113 | Graceful shutdown blocks until in-flight flush completes | — | MOVE(L4) | Runner owns graceful drain |
| test_prepare_for_sync_shutdown_drains_pending_debounced_messages | 3160 | Shutdown flushes still-debouncing messages | — | MOVE(L4) | Runner-owned graceful drain |
| test_prepare_for_sync_shutdown_drains_pending_upload_grace | 3194 | Shutdown flushes text-only batch held in grace | — | MOVE(L4) | Runner-owned graceful drain cuts grace short |
| test_shutdown_during_in_flight_dispatch_does_not_start_grace | 3232 | Shutdown awaits in-flight turn, flushes remainder without grace | — | MOVE(L4) | No new grace during shutdown |
| test_thread_followups_wait_behind_first_turn_root_in_flight | 3290 | Thread replies blocked at the gate until room-root dispatch completes | P4 (violated) | INVERT | Decision 1: inverted test asserts thread replies dispatch without waiting on the root turn |
| test_active_approval_fallthrough_reserves_before_async_approval_lookup | 3351 | Slow async approval doesn't let later same-sender message overtake | P5 | KEEP(P5, L2) | Async prechecks must not reorder the per-sender FIFO |
| test_trusted_relay_approval_fallthrough_reserves_effective_requester | 3400 | Relay approval fallthrough orders by effective (human) requester | P5 | KEEP(P5, L2) | Effective requester (L1) feeds the per-sender FIFO |
| test_zero_debounce_immediate_flush_logs_pending_count_before_clearing | 3457 | Gate enqueue telemetry on zero-debounce path | — | DELETE | Logs internals of deleted gate enqueue paths |
| test_zero_debounce_with_upload_grace_logs_scheduled_grace_outcome | 3489 | Zero-debounce telemetry reports scheduled_drain for grace | — | DELETE | Logs internals of deleted gate paths |
| test_enqueue_for_dispatch_timing_events_include_explicit_scope | 3520 | Ingress-handoff timing events carry source-event timing scope | — | MOVE(L1) | Ingress→inbox handoff seam survives; re-express telemetry |
| test_matrix_ingress_logging_includes_receive_lag | 3551 | Callback log includes receive lag; receipt_time forwarded | P5 | KEEP(P5, L1) | Surviving ingress seam; receipt_time feeds the FIFO |
| test_matrix_ingress_logging_handles_missing_origin_timestamp | 3578 | Lag fields omitted when origin_server_ts missing | P5 | KEEP(P5, L1) | Missing-field tolerance on surviving seam |
| test_handle_coalesced_batch_timing_events_include_dispatch_scope | 3606 | Batch-handling telemetry carries batch event timing scope | — | MOVE(L3) | Inbox→runner handoff seam survives |
| test_handle_coalesced_batch_uses_batch_key_for_text_primary | 3637 | Mixed batch dispatch relation comes from the batch's canonical key even with text primary | P5 | KEEP(P5, L3) | Canonical conversation id governs dispatch relation |

## tests/test_live_message_coalescing.py — part 2 (lines ≥ 3651, 79 tests)

| Test | Line | What it actually asserts | P | Verdict | Target / reason |
|---|---|---|---|---|---|
| test_room_resolved_voice_batch_clears_stale_primary_thread_relation | 3666 | Room-keyed voice+typed handoff drops stale m.thread relation to pending voice | P5 | KEEP(P5, L3) | Canonical id applied over per-event relations |
| test_room_level_batch_preserves_plain_reply_relation_without_thread_target | 3697 | Room-level handoff keeps m.in_reply_to, adds no thread targeting | — | KEEP(—, L3) | Room-level relation shape |
| test_room_level_batch_preserves_mentions_while_removing_stale_thread_relation | 3718 | Mentions survive while stale thread relation is removed | — | KEEP(—, L3) | Mention preservation + identity-from-key |
| test_room_level_mention_batch_preserves_plain_reply_relation | 3741 | Mentions and plain in_reply_to both preserved | — | KEEP(—, L3) | Same family |
| test_coalesced_room_plain_reply_target_uses_prompt_thread_not_reply_thread | 3766 | Room-level dispatch targets thread rooted at prompt event | — | KEEP(—, L2) | Response targeting decided once in L2 |
| test_single_mentioned_followup_batch_uses_coalescing_thread_relation | 3799 | Batch key's thread id replaces event's stale thread; mentions kept | — | KEEP(—, L3) | Key wins over event relation |
| test_single_followup_batch_uses_coalescing_thread_relation | 3822 | Follow-up relation comes from coalescing key (post-STT thread) | P5 | KEEP(P5, L3) | Late-resolving voice conversation id applied |
| test_register_batch_media_attachments_emits_payload_timing_for_empty_batch | 3848 | Success timing with zero counts for empty media batch | P2 | KEEP(P2, L1) | Normalizer media-registration telemetry |
| test_register_batch_media_attachments_emits_payload_timing_on_failure | 3879 | Timing emitted with outcome=failed when download raises | P2 | KEEP(P2, L1) | Failure telemetry on L1 normalization |
| test_dispatch_payload_registers_unregistered_image_from_thread_history | 3912 | Unregistered thread-history image becomes persisted attachment | P2 | OUT-OF-SCOPE | Execution-side payload prep; untouched by redesign |
| test_trusted_current_attachment_ids_bypass_final_thread_filter | 3960 | Trusted current-turn attachment survives payload thread filter | P2 | KEEP(P2, L4) | Trust flag keeps utterance attachments through payload prep |
| test_flush_logs_failed_outcome_when_dispatch_batch_raises | 3995 | Flush telemetry reports outcome=failed when dispatch raises | — | REPLACE | L3 dispatch-outcome telemetry must not report success on failure |
| test_coalescing_enqueue_logs_pending_count | 4029 | Gate enqueue log carries pending_count and ids | — | REPLACE | Superseded by equivalent L3 inbox diagnostics |
| test_slow_coalescing_flush_warns_with_correlation_metadata | 4059 | Slow-flush warning carries event ids, room, thread, outcome | — | REPLACE | Superseded by L3 slow-dispatch diagnostics with correlation ids |
| test_timer_flush_logs_dispatch_failure_without_unhandled_task | 4093 | Timer-driven failure consumed/logged; no unhandled asyncio exception | — | KEEP(—, L3) | Debounce-timer failures must never leak task exceptions |
| test_failed_drain_does_not_poison_future_ingress | 4141 | After dispatch failure, later event dispatches normally | — | KEEP(—, L3) | Failure isolation |
| test_failed_drain_dispatches_buffered_ingress_without_waiting_for_another_event | 4189 | Events buffered during failing dispatch drain unprompted | P3 | KEEP(P3, L3) | Queued follow-up fires after failed turn without new ingress |
| test_cancelled_drain_cleans_state_for_later_message | 4241 | Cancelled dispatch leaves inbox idle; later message dispatches fresh | — | KEEP(—, L3) | Cancellation cleanup-exactly-once |
| test_cancelled_drain_dispatches_buffered_ingress_without_waiting_for_another_event | 4295 | Events buffered behind cancelled dispatch drain unprompted | P3 | KEEP(P3, L3) | Busy-queue drain after cancellation |
| test_coalescing_drain_logs_lifecycle_metadata | 4346 | Drain debug logs include enqueue/start/finish with counts/ages | — | REPLACE | Gate-internal log names; superseded by L3 lifecycle diagnostics |
| test_cleanup_drains_pending_debounce_tasks | 4383 | Bot cleanup awaits pending debounce and dispatches buffered turn | — | KEEP(—, L4) | Graceful shutdown drains pending batches |
| test_upload_grace_hard_cap_prevents_indefinite_extension | 4412 | Repeated media extends grace but batch dispatches within hard cap | P2 | KEEP(P2, L3) | Direct P2 invariant |
| test_turn_store_marks_all_batch_event_ids | 4469 | All batch source ids marked handled with shared anchor and prompts | P1 | KEEP(P1, L4) | Durable dedup covering all batch source ids |
| test_zero_debounce_dispatches_immediately | 4521 | debounce=0 dispatches immediately; idle after | P1 | KEEP(P1, L3) | Debounce config edge case |
| test_multiple_commands_each_dispatch_independently | 4555 | Rapid !commands each dispatch solo, in order | — | MOVE(L1) | Becomes L1 command-routing test |
| test_gate_entry_removed_after_dispatch_with_no_pending | 4599 | Gate entry cleaned up when idle | — | KEEP(—, L3) | Per-conversation inbox state must not leak |
| test_backlog_replay_skips_older_message_when_newer_exists | 4625 | Replayed older message skipped when newer unresponded turn exists | P1 | KEEP(P1, L1) | Restart replay guard |
| test_backlog_replay_degraded_thread_history_uses_cached_room_event_positive_proof | 4667 | Cached newer same-thread event suppresses older replayed message | P1 | KEEP(P1, L1) | Degraded-cache replay proof |
| test_backlog_replay_degraded_thread_history_ignores_equal_timestamp_cached_event | 4730 | Equal-timestamp cached event is not strictly newer | P1 | KEEP(P1, L1) | Strictness parity |
| test_backlog_replay_degraded_thread_history_counts_trusted_voice_command_body | 4793 | Trusted voice relay with command-shaped body counts as newer turn | P1 | KEEP(P1, L1) | Voice transcripts aren't commands for suppression |
| test_replay_guard_does_not_supersede_non_interactive_origin_turns | 4849 | Non-interactive-origin turns never superseded | P1 | KEEP(P1, L1) | Guards must not eat machine turns |
| test_full_history_replay_guard_ignores_visible_router_voice_echo | 4883 | Router's visible voice echo doesn't suppress the canonical turn | P1 | KEEP(P1, L1) | Router-echo dedup is L1 |
| test_full_history_replay_guard_counts_non_router_visible_echo_marker | 4929 | Echo marker on non-router trusted relay still suppresses | P1 | KEEP(P1, L1) | Only router echoes are display-only |
| test_backlog_replay_degraded_thread_history_ignores_visible_router_voice_echo | 4974 | Degraded path: cached router echo doesn't suppress | P1 | KEEP(P1, L1) | Same invariant, degraded path |
| test_backlog_replay_degraded_thread_history_counts_non_router_visible_echo_marker | 5040 | Degraded path: non-router echo marker still suppresses | P1 | KEEP(P1, L1) | Anti-spoof parity |
| test_backlog_replay_degraded_thread_history_uses_cache_indexed_plain_reply | 5106 | Cache-indexed plain reply counts as newer same-thread proof | P1 | KEEP(P1, L1) | Rebase plain-reply mapping onto L2 resolution |
| test_backlog_replay_degraded_thread_history_ignores_edit_events | 5168 | Cached m.replace edits don't count as newer turns | P1 | KEEP(P1, L1) | Edits belong to the edit subsystem |
| test_backlog_replay_degraded_thread_history_fails_open_without_positive_cached_proof | 5231 | No positive cached proof → dispatch proceeds | P1 | KEEP(P1, L1) | Fail-open default |
| test_media_dispatch_uses_replay_snapshot_instead_of_mutated_planning_history | 5284 | Replay guard reads immutable snapshot, not hydrated history | P1 | KEEP(P1, L1) | Snapshot immutability |
| test_thread_history_guard_does_not_interfere_with_normal_dispatch | 5336 | With no newer message, live dispatch completes and marks handled | P1 | KEEP(P1, L1) | Guard non-interference baseline |
| test_batch_dispatch_event_merges_mentions_across_events | 5411 | Mention from first event survives merge | P1 | KEEP(P1, L3) | Combined utterance preserves mentions |
| test_batch_dispatch_event_preserves_voice_fallback_metadata | 5437 | Trusted voice fallback key survives batch merge | — | KEEP(—, L3) | System-owned metadata propagation |
| test_single_prepared_batch_dispatch_event_preserves_source_kind | 5474 | Follow-up policy carried separately; source kind stays message | P3 | KEEP(P3, L3) | Busy-queue marker distinct from source kind |
| test_single_text_batch_dispatch_event_preserves_bypass_source_kind | 5505 | Same separation for raw nio text events | P3 | KEEP(P3, L3) | Same invariant, raw-event variant |
| test_batch_dispatch_event_preserves_original_sender | 5529 | Trusted relay's original_sender survives merge | — | KEEP(—, L3) | Effective-requester metadata propagated |
| test_batch_dispatch_event_preserves_attachment_ids | 5570 | Attachment ids from all trusted events merge | P2 | KEEP(P2, L3) | Utterance attachments aggregate |
| test_newer_command_does_not_suppress_older_message | 5631 | Newer !command doesn't suppress older message in replay guard | P1 | KEEP(P1, L1) | Commands aren't conversational turns |
| test_newer_command_with_whitespace_does_not_suppress | 5682 | Whitespace-padded command still excluded from suppression | P1 | KEEP(P1, L1) | Command-detection robustness |
| test_scheduled_event_not_suppressed | 5736 | Scheduled turn dispatches despite newer same-sender message | P1 | KEEP(P1, L1) | Machine events never suppressed |
| test_hook_event_not_suppressed | 5789 | Hook turn dispatches despite newer message | P1 | KEEP(P1, L1) | Same machine exemption |
| test_multiple_scheduled_fires_not_suppressed | 5839 | First scheduled fire executes despite newer second fire | P1 | KEEP(P1, L1) | Each machine fire is its own utterance |
| test_coalesced_user_batch_suppressed_by_thread_guard | 5897 | User-originated synthetic batch still suppressed by guard | P1 | KEEP(P1, L1) | Exemption keyed on origin, not wrapper |
| test_coalesced_media_batch_suppressed_by_replay_snapshot | 5939 | Media-backed user batch suppressed by replay snapshot | P1 | KEEP(P1, L1) | Same, media variant |
| test_normal_text_command_still_dispatches_as_command | 5987 | Text !command routes to command executor, never planning | — | MOVE(L1) | Becomes L1 command-routing test |
| test_active_voice_follow_up_preserves_voice_command_policy | 6020 | Voice follow-up plans as forced response, never command path | P3 | KEEP(P3, L3) | Rewrite against L3 busy-queue follow-up marker |
| test_older_command_not_suppressed_during_replay | 6075 | Replayed older !command executes despite newer message | — | KEEP(—, L1) | Commands bypass replay suppression |
| test_batch_dispatch_event_preserves_formatted_body_mentions | 6152 | Bridge pill mentions in formatted_body survive merge | P1 | KEEP(P1, L3) | Mention preservation, HTML variant |
| test_gate_final_envelope_preserves_active_voice_source_and_policy | 6299 | Voice source kind and follow-up policy reach envelope separately | P3 | KEEP(P3, L3) | End-to-end envelope preservation |
| test_gate_final_envelope_preserves_non_active_voice_command_policy | 6332 | Non-active voice "!help" keeps voice kind, no policy override | — | KEEP(—, L3) | Voice transcripts plan as turns, not commands |
| test_gate_final_envelope_preserves_active_text_source_and_policy | 6357 | Active text follow-up keeps message kind with separate policy | P3 | KEEP(P3, L3) | Same separation, text variant |
| test_gate_final_envelope_preserves_active_and_normal_media_sources | 6374 | Different senders' media dispatch separately; modality preserved | P1 | KEEP(P1, L3) | Per-(conversation, sender) separation |
| test_gate_final_envelope_preserves_raw_trusted_relay_source_kind | 6410 | Relay stays raw for hydration; envelope carries relay kind | — | KEEP(—, L3) | L1-validated metadata preserved through handoff |
| test_trusted_router_relay_context_uses_handoff_ingress_metadata | 6433 | Relay handoff selects trusted-router-relay context extractor | — | KEEP(—, L2) | Context resolution keyed by L1 trust metadata |
| test_gate_final_envelope_preserves_hook_metadata_with_original_sender | 6484 | Hook keeps kind plus hook_source/depth; not reclassified | — | KEEP(—, L3) | Hook classification preserved |
| test_automation_and_relay_source_kinds_dispatch_solo_with_human_neighbor | 6528 | Machine/relay event dispatches solo before human message, ordered | P1,P5 | KEEP(P1, L3) ⚠D | Solo emerges from machines skipping debounce; total cross-sender order not enforced |
| test_coalesced_attachment_ids_reach_envelope_and_model_payload | 6568 | Merged trusted attachment ids reach envelope and model payload | P2 | KEEP(P2, L3) | End-to-end attachment flow |
| test_coalesced_non_primary_mention_reaches_final_envelope | 6624 | Mention on non-primary batched event reaches envelope | P1 | KEEP(P1, L3) | Burst-wide mention visibility |
| test_untrusted_raw_payload_metadata_spoofing_does_not_reach_envelope_or_payload | 6650 | User-authored internal keys stripped from envelope/payload | — | KEEP(—, L1) | Anti-spoofing is L1 |
| test_untrusted_nested_skip_mentions_does_not_suppress_visible_mentions | 6683 | Nested m.new_content skip-mentions ignored; mention detected | — | KEEP(—, L1) | Anti-spoofing of edit-layer metadata |
| test_untrusted_coalesced_payload_metadata_spoofing_does_not_reach_envelope_or_payload | 6715 | Spoofed internal keys in coalesced primary stripped | — | KEEP(—, L1) | Anti-spoofing through batch merge |
| test_untrusted_synthetic_voice_payload_metadata_spoofing_is_not_trusted | 6751 | Synthetic voice wrapper without trust flag gets metadata stripped | — | KEEP(—, L1) | Trust is explicit, never inferred |
| test_trusted_voice_normalized_payload_metadata_reaches_envelope_and_payload | 6791 | Trust-flagged voice metadata reaches envelope/payload/trusted ids | P2 | KEEP(P2, L1) | Positive case of L1 trust contract |
| test_coalesced_root_voice_attachment_is_trusted_when_later_text_is_primary | 6837 | Voice attachment stays trusted when later text is primary | P2 | KEEP(P2, L3) | Attachments belong to utterance across primary change |
| test_untrusted_sidecar_payload_metadata_spoofing_does_not_reach_envelope_or_payload | 6880 | User sidecar JSON hydrates text but internal keys not trusted | — | KEEP(—, L1) | Sidecar hydration anti-spoofing |
| test_sidecar_hydration_preserves_trusted_attachment_metadata | 6984 | Trusted hydrated sidecar attachment ids reach envelope/payload | P2 | KEEP(P2, L1) | Trusted hydration positive case |
| test_sidecar_hydration_refreshes_prompt_and_mentions_before_dispatch | 7049 | Hydration replaces preview prompt/mentions before dispatch | — | KEEP(—, L1) | Hydration-as-normalization completes before policy |
| test_router_early_skip_keeps_sidecar_preview_for_hydration | 7100 | Router pre-ingress skip returns False for sidecar previews | — | KEEP(—, L1) | Early-skip must not drop sidecars before hydration |
| test_router_early_skip_labels_thread_snapshot_refresh | 7126 | Skip check reads dispatch-safe snapshot with caller label | — | KEEP(—, L1) | Attribution on surviving precheck path |
| test_router_early_skip_fails_open_for_thread_snapshot_failure | 7166 | Snapshot read failure → should_skip False (fail-open) | — | KEEP(—, L1) | Fail-open invariant |

## tests/test_voice_bot_threading.py (21 tests)

| Test | Line | What it actually asserts | P | Verdict | Target / reason |
|---|---|---|---|---|---|
| test_voice_message_in_main_room_creates_thread | 329 | Room-root voice yields response in thread rooted at the voice event | — | KEEP(—, L2) | Unthreaded voice starts the thread at the audio event |
| test_voice_message_in_thread_continues_thread | 375 | Threaded voice keeps thread root; attachment persisted under it | P2 | KEEP(P2, L2) | Identity plus attachment-to-utterance binding |
| test_voice_plain_reply_to_thread_message_stays_threaded_transitively | 431 | Plain-reply voice inherits thread root transitively | — | KEEP(—, L2) | Transitive reply-chain resolution |
| test_voice_plain_reply_unproven_thread_candidate_is_not_admitted | 489 | Indeterminate thread proof raises instead of guessing a key | — | KEEP(—, L2) | Resolution failure rejects ingress, never guesses |
| test_voice_message_signals_active_turn_before_stt | 527 | Voice raises queued-signal pending count pre-STT, cleared after | P3 | REPLACE | L3 inbox: voice queues on busy conversation pre-STT; signal internals superseded |
| test_voice_message_clears_active_turn_signal_when_post_stt_echo_fails | 602 | Echo failure releases pre-STT pending reservation; no leak | P3 | REPLACE | Ingress failure releases queued state; mechanics superseded |
| test_failed_or_disabled_visible_echo_does_not_affect_canonical_voice_dispatch | 684 | Echo failure/absence still yields exactly one canonical turn | P1 | KEEP(P1, L2) | Echo is display-only; never blocks canonical dispatch |
| test_voice_message_uses_canonical_target_for_queued_notice_before_stt | 732 | Queued notice keys off pre-STT canonical target; one dispatch | P3 | REPLACE | L2 decides id once; L3 accounting keyed by it |
| test_room_mode_voice_notice_survives_until_queued_dispatch_owns_it | 828 | Room-mode voice marks room-level turn pending until dispatch owns it | P3 | REPLACE ⚠B | Merge into mode-agnostic L3 busy-queue test |
| test_voice_and_text_followups_during_streaming_coalesce_in_receive_order | 922 | Two voice + one typed follow-up during streaming yield one combined follow-up, ordered | P3,P5 | KEEP(P3, L3) | Core busy-queue invariant; setup re-driven via public ingress |
| test_voice_first_text_second_uses_receive_order_when_stt_finishes_late | 1042 | Typed message waits for earlier voice with pending STT; ordered combined turn | P1,P5 | KEEP(P5, L2) | Exactly the per-(room, sender) FIFO |
| test_voice_first_text_second_waits_for_slow_thread_resolution | 1129 | Typed message waits while earlier voice's thread lookup pends | P5 | KEEP(P5, L2) | FIFO invariant for slow resolution, not just STT |
| test_root_voice_and_root_text_share_room_scope_while_stt_pending | 1227 | Room-root voice + room-root text dispatch once, ordered | P1,P5 | KEEP(P1, L3) | Reword away from key-sharing mechanics; outcome is one turn per burst |
| test_room_mode_voice_burst_dispatches_as_one_turn | 1332 | Room-mode agent: threaded voice burst batches into one turn | P1 | KEEP(P1, L3) ⚠B | Mode-agnostic inbox; depends on room-mode identity carve-out |
| test_trusted_router_visible_voice_echo_is_display_only | 1403 | Trusted router voice echo marked handled, never dispatched | — | MOVE(L1) | Becomes L1 trusted-echo-drop test |
| test_forged_visible_voice_echo_marker_still_dispatches | 1444 | Forged echo markers still dispatch, not marked handled | — | MOVE(L1) | Becomes L1 anti-spoofing test |
| test_raw_voice_normalization_exception_dispatches_audio_fallback | 1484 | STT exception dispatches raw-audio fallback into correct thread | P2 | KEEP(P2, L2) | Normalization failure terminates visibly with audio fallback |
| test_raw_voice_download_failure_dispatches_text_only_fallback | 1530 | Download failure dispatches text-only fallback, correct thread | P2 | KEEP(P2, L2) | Failure visible; no silent handled-marking |
| test_raw_voice_thread_resolution_exception_does_not_dispatch_guessed_fallback | 1565 | Thread-lookup exception propagates; no guessed-thread fallback | — | KEEP(—, L2) | Resolution failure rejects ingress |
| test_raw_voice_root_target_failures_do_not_dispatch_guessed_fallbacks | 1603 | Repeated failures yield zero dispatches; nothing handled or batched | — | KEEP(—, L2) | Same rejection invariant under a burst |
| test_raw_voice_cache_append_exception_does_not_dispatch_guessed_fallback | 1666 | Pre-admission cache failure aborts loudly; no dispatch, no STT | — | KEEP(—, L2) | Re-target whatever pre-admission step replaces the cache-append seam |

## tests/test_agent_order_preservation.py (8 tests)

| Test | Line | What it actually asserts | P | Verdict | Target / reason |
|---|---|---|---|---|---|
| test_check_agent_mentioned_preserves_order | 80 | check_agent_mentioned returns agents in m.mentions order | — | OUT-OF-SCOPE | Routing/team-formation mention ordering, not ingress |
| test_get_agents_in_thread_preserves_order | 102 | Agents returned in order of first thread participation | — | OUT-OF-SCOPE | Keep unchanged |
| test_get_agents_in_thread_excludes_router | 121 | Router excluded from thread-participant list | — | OUT-OF-SCOPE | Keep unchanged |
| test_get_all_mentioned_agents_preserves_order | 139 | First-mention order preserved across messages | — | OUT-OF-SCOPE | Keep unchanged |
| test_no_duplicates_in_mentioned_agents | 174 | Duplicate mentions deduped preserving first-mention order | — | OUT-OF-SCOPE | Keep unchanged |
| test_empty_thread_returns_empty_list | 215 | Empty thread history yields empty lists | — | OUT-OF-SCOPE | Keep unchanged |
| test_order_matters_for_coordinate_mode | 221 | Two differing mention orders preserved distinctly | — | OUT-OF-SCOPE | Keep unchanged |
| test_coordinate_mode_respects_order | 255 | decide_team_formation keeps tagged-agent order, picks COORDINATE | — | OUT-OF-SCOPE | Keep unchanged |

## PR #1240 tests (pending merge; triaged ahead of Phase 4)

PR #1240's started-dispatch tracking is deleted in Phase 4; its tests are triaged here so the deletion is pre-agreed.

| Test | File | What it asserts | P | Verdict | Target / reason |
|---|---|---|---|---|---|
| test_second_root_batch_dispatches_once_first_response_starts | test_coalescing.py | Second root batch dispatches once the first response has started | P4 | REPLACE | Superseded by stronger independence: second root dispatches immediately, no response-start gating |
| test_thread_reply_dispatches_while_root_response_is_running | test_coalescing.py | Thread reply dispatches while a root response runs | P4 | REPLACE | Superseded by the explicit P4 regression test (sub-second dispatch during a multi-minute turn) |
| test_drain_all_waits_for_started_dispatch_to_complete | test_coalescing.py | Graceful drain awaits started responses | — | MOVE(L4) | Runner-owned graceful drain awaits in-flight responses |
| test_bounded_drain_cancels_started_dispatch_and_reports_incomplete | test_coalescing.py | Bounded drain cancels started responses, reports incomplete | — | MOVE(L4) | Runner-owned bounded drain |
| test_started_dispatch_failure_closes_segment_metadata | test_coalescing.py | Response failing after gate release still closes metadata once | — | REPLACE | Metadata closes exactly once on failure; re-express against runner-owned lifecycle |
| test_handle_coalesced_batch_threads_response_start_signal_to_executor | test_live_message_coalescing.py | ResponseStartSignal reaches executor as lifecycle-lock callback | — | DELETE | Tests the ResponseStartSignal plumbing deleted in Phase 4 |
