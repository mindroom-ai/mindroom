# Transitive Thread Membership Restoration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current single-hop plain-reply inheritance model with one shared transitive thread-membership invariant that every runtime, cache, tool, and history path uses consistently.

**Architecture:** Restore reply-chain semantics as a single shared `effective_thread_id(event)` rule instead of scattered local special cases. Use the commit immediately before reply-chain removal as the semantic baseline, then re-implement that behavior on top of the current cache and runtime boundaries so all hot, cold, and tool paths agree.

**Contract decision:** This plan intentionally makes transitive thread inheritance the final product contract.
Current single-hop docs, tests, and helper behavior are expected to be rewritten, not preserved.

**Tech Stack:** Python 3.13, `matrix-nio`, SQLite-backed event cache, structlog, pytest, pre-commit.

---

## Baseline Commits

- **Semantic baseline before removal:** `2637e0e21`
- **Removal commit:** `380214dba` (`refactor: drop reply-chain thread inference`)
- **Current head at planning time:** `c23c28970`

## Invariant To Implement Everywhere

Use one shared rule:

```text
effective_thread_id(E) =
1. explicit m.thread root T, if present
2. else effective_thread_id(direct reply target of E), for plain replies
3. else effective_thread_id(original/target event of E), for edits, reactions, and redactions
4. else E, only when E is a root and authoritative data proves that thread children exist
5. else None
```

Additional requirements:

- Traversal must be cycle-safe and bounded.
- `event_id -> effective_thread_id` must be persisted once learned.
- Every caller must either use the shared invariant directly or consume a durable mapping produced by it.
- No subsystem may invent its own thread membership rule.

## Condition Matrix

These conditions must evaluate identically in every relevant subsystem:

| Event shape | Expected `effective_thread_id` |
| --- | --- |
| Explicit `m.thread` event to root `T` | `T` |
| Plain `m.in_reply_to` to event already in `T` | `T` |
| Plain `m.in_reply_to` to plain reply already in `T` | `T` |
| Edit of explicit thread event in `T` | `T` |
| Edit of promoted plain reply in `T` | `T` |
| Reaction/redaction to event in `T` | `T` |
| Plain reply chain that eventually reaches explicit thread `T` | `T` |
| Top-level room message with no proven children | `None` |
| Plain reply chain that never reaches explicit thread/root evidence | `None` |
| Root event with authoritative evidence of children | root event ID |

## Method-Level Impact Map

This refactor must be tracked at the method boundary, not only at the file boundary.

The exact rule is that every method below must either compute `effective_thread_id` through the shared transitive helper, or consume a durable `event_id -> effective_thread_id` mapping that was produced by that helper.

### Canonical graph parsing and membership traversal

- `src/mindroom/matrix/event_info.py: EventInfo.from_event(), EventInfo.next_related_event_id(), _analyze_event_relations(), _extract_thread_id_from_new_content()` define the relation graph edges that the invariant is allowed to follow.
- `src/mindroom/matrix/thread_membership.py: _next_related_event_target(), resolve_event_thread_id(), resolve_related_event_thread_id()` are the only places that may traverse reply, edit, reaction, redaction, and root-child edges to compute `effective_thread_id`.
- `src/mindroom/matrix/thread_membership.py: ThreadMembershipAccess.lookup_thread_id(), ThreadMembershipAccess.fetch_event_info(), ThreadMembershipAccess.thread_root_has_children()` define the three external data sources the invariant may consult.
- Traversal requirements at this seam are cycle-safe, bounded, and transitive across plain-reply chains until a real threaded ancestor or proven thread root is found.

### Inbound routing and response targeting

- `src/mindroom/conversation_resolver.py: ConversationResolver.build_message_target()` must convert canonical membership into `source_thread_id`, `resolved_thread_id`, and `thread_start_root_event_id` without inventing new thread rules.
- `src/mindroom/conversation_resolver.py: ConversationResolver.resolve_response_thread_root()` must normalize any inherited membership back to the canonical root used for delivery.
- `src/mindroom/conversation_resolver.py: ConversationResolver.coalescing_thread_id()` must use the same transitive answer as dispatch, not an earlier or narrower approximation.
- `src/mindroom/conversation_resolver.py: ConversationResolver._explicit_thread_id_for_event(), ConversationResolver._event_info_for_event_id(), ConversationResolver._thread_root_has_children(), ConversationResolver._resolve_thread_context()` must stop being single-hop or explicit-only seams and instead become thin wrappers around canonical membership.
- `src/mindroom/conversation_resolver.py: ConversationResolver.derive_conversation_context(), ConversationResolver.extract_dispatch_context(), ConversationResolver.extract_message_context(), ConversationResolver.extract_message_context_impl(), ConversationResolver.hydrate_dispatch_context(), ConversationResolver.fetch_thread_history()` must all see the same `effective_thread_id` for the same event.
- `src/mindroom/message_target.py: MessageTarget.from_runtime_context(), MessageTarget.resolve(), MessageTarget.with_thread_root()` must preserve the `source_thread_id` versus `resolved_thread_id` split while using one canonical thread identity underneath.
- `src/mindroom/delivery_gateway.py: DeliveryGateway.send_text(), DeliveryGateway.edit_text(), DeliveryGateway.deliver_final(), DeliveryGateway.deliver_stream(), DeliveryGateway.finalize_streamed_response(), DeliveryGateway.send_compaction_notice()` must send replies, edits, and compaction notices into the same resolved thread that routing chose.
- `src/mindroom/streaming.py: StreamingResponse._resolve_stream_status(), StreamingResponse._record_streaming_send(), StreamingResponse._record_streaming_edit(), StreamingResponse._send_initial_content(), StreamingResponse._edit_existing_content(), send_streaming_response()` must preserve the same resolved thread semantics as non-streaming delivery.

### Batching, coalescing, and active-thread bypass

- `src/mindroom/turn_controller.py: TurnController._should_bypass_coalescing_for_active_thread_follow_up(), TurnController._coalescing_key_for_event(), TurnController.prepare_request_after_lock(), TurnController.handle_coalesced_batch(), TurnController._dispatch_text_message(), TurnController.build_payload()` must use the same transitive thread identity before and after the lock.
- `src/mindroom/coalescing.py: build_coalesced_batch(), _merge_batch_source(), build_batch_dispatch_event(), CoalescingGate.retarget(), CoalescingGate.enqueue(), CoalescingGate._flush()` must preserve thread-aware grouping decisions made from canonical membership.
- `src/mindroom/bot.py: AgentBot._emit_reaction_received_hooks(), AgentBot._build_shared_execution_identity(), AgentBot._dispatch_coalesced_batch(), AgentBot._handle_reaction_inner(), AgentBot._should_queue_follow_up_in_active_response_thread(), AgentBot._send_welcome_message_if_empty()` must not recompute or weaken thread identity outside the shared rule.

### Cold reads, cache reconstruction, and durable membership persistence

- `src/mindroom/matrix/client.py: _resolve_thread_history_from_event_sources_timed(), _resolve_cached_thread_history(), _fetch_thread_history_with_events(), refresh_thread_history_from_source(), _store_thread_history_cache(), fetch_thread_history(), fetch_thread_snapshot(), fetch_dispatch_thread_history(), fetch_dispatch_thread_snapshot()` must all treat transitive inherited members as part of the same thread history.
- `src/mindroom/matrix/client.py: _record_scanned_room_message_source(), _resolve_scanned_thread_message_sources(), _fetch_thread_event_sources_via_room_messages()` are the cold room-scan reconstruction seam and must reproduce the same transitive membership as live dispatch.
- `src/mindroom/matrix/client.py: join_room()` is not a membership method, but it is part of the startup path touched by recent fixes and must still satisfy startup room seeding and welcome-message assumptions after refactoring.
- `src/mindroom/matrix/cache/event_cache.py: _EventCache.get_thread_events(), _EventCache.get_thread_id_for_event(), _EventCache.store_events_batch(), _EventCache.replace_thread(), _EventCache.append_event(), _event_thread_row(), _with_thread_root_self_rows(), _thread_event_ids_for_thread(), _thread_event_ids_for_room(), _write_lookup_index_rows()` are the durable storage seam for `event_id -> effective_thread_id`.
- `src/mindroom/matrix/cache/thread_reads.py: ThreadReadPolicy.get_thread_snapshot(), get_thread_history(), get_dispatch_thread_snapshot(), get_dispatch_thread_history(), get_latest_thread_event_id_if_needed()` must consume the durable mapping consistently and fail open cleanly when runtime support is absent.
- `src/mindroom/matrix/cache/thread_writes.py: ThreadWritePolicy._lookup_redaction_thread_id(), _invalidate_after_redaction(), _resolve_thread_id_for_mutation(), _cached_event_info_for_event_id(), _thread_root_has_children(), _append_event_to_cache(), _apply_outbound_message_notification(), notify_outbound_message(), _apply_outbound_redaction_notification(), notify_outbound_redaction(), _persist_threaded_sync_events(), _persist_room_sync_timeline_updates()` must persist and invalidate based on transitive membership, not only explicit thread metadata.
- `src/mindroom/matrix/conversation_cache.py: MatrixConversationCache.persist_lookup_event(), get_thread_snapshot(), get_thread_history(), get_dispatch_thread_snapshot(), get_dispatch_thread_history(), get_thread_id_for_event(), get_latest_thread_event_id_if_needed(), notify_outbound_message(), notify_outbound_redaction(), append_live_event(), apply_redaction(), cache_sync_timeline()` are the public facade methods that must expose the same invariant to higher layers.

### Tools, hooks, and runtime context

- `src/mindroom/custom_tools/matrix_api.py: MatrixApiTools._get_thread_id_for_event(), _event_info_for_event(), _thread_root_has_children(), _requires_conversation_cache_write(), _redaction_requires_conversation_cache_write(), _record_send_event_outbound_cache_write(), _send_event(), _redact()` must treat transitive membership as canonical while keeping dry runs purely local.
- `src/mindroom/custom_tools/matrix_message.py: MatrixMessageTools._send_matrix_text(), _message_send_or_reply(), _message_edit(), _message_context(), _dispatch_action()` must default to the same resolved thread context as the rest of the runtime and must record outbound cache writes against the same canonical membership model.
- `src/mindroom/custom_tools/subagents.py: _threaded_dispatch_error(), _send_matrix_text(), _spawn_followup_warnings(), SubAgentsTools.sessions_send(), SubAgentsTools.sessions_spawn()` must preserve the same room-versus-thread dispatch contract and the same `MessageTarget.from_runtime_context()` semantics as the canonical runtime path.
- `src/mindroom/thread_tags.py: _canonical_thread_id(), _event_info_for_event_id(), normalize_thread_root_event_id(), _lookup_thread_id_from_cache(), _thread_root_has_children(), get_thread_tags(), set_thread_tag(), remove_thread_tag(), list_tagged_threads()` must normalize to the same root that routing and history use.
- `src/mindroom/custom_tools/thread_tags.py: _resolve_target_thread_reference(), ThreadTagsTools.tag_thread(), untag_thread(), list_thread_tags()` must validate explicit room inputs and resolve thread targets through canonical membership.
- `src/mindroom/custom_tools/thread_summary.py: ThreadSummaryTools.set_thread_summary()` must default to the current resolved thread context, including inherited thread membership.
- `src/mindroom/thread_summary.py: _load_thread_history(), _recover_last_summary_count(), send_thread_summary_event(), maybe_generate_thread_summary()` must read and send summaries in the same canonical thread.
- `src/mindroom/custom_tools/attachments.py: _register_attachment_file_path(), send_context_attachments(), _resolve_send_target(), AttachmentTools.register_attachment()` must scope and send attachments using resolved thread context while preserving source provenance separately.
- `src/mindroom/custom_tools/attachment_helpers.py: resolve_requested_room_id(), resolve_context_thread_id()` must keep room validation and resolved-thread lookup aligned with the shared invariant.
- `src/mindroom/tool_system/runtime_context.py: ToolRuntimeSupport.build_context(), ToolRuntimeSupport.build_execution_identity(), emit_custom_event()` must preserve `source_thread_id` and `resolved_thread_id` correctly on the real tool-runtime path.
- `src/mindroom/tool_system/tool_hooks.py: _resolved_thread_id(), _resolve_tool_context(), _execute_bridge()` must use canonical resolved thread context for hook execution.
- `src/mindroom/commands/handler.py: _resolve_tool_dispatch_target(), _prepare_tool_call_arguments(), _run_skill_command_tool()` must preserve thread provenance and resolved-thread scope when routing command-triggered tool execution.

### Follow-up effects, persistence, and regeneration

- `src/mindroom/post_response_effects.py: PostResponseEffectsSupport.should_queue_thread_summary(), queue_thread_summary(), apply_post_response_effects()` must schedule summaries and compaction notices against the same canonical thread chosen during response generation.
- `src/mindroom/response_runner.py: ResponseRunner._resolve_request_target(), _refresh_thread_history_after_lock(), _prepare_request_after_lock(), _response_envelope_for_request(), process_and_respond(), process_and_respond_streaming(), send_skill_command_response()` must either refetch full transitive thread history or fail closed where the dispatch contract requires it.
- `src/mindroom/edit_regenerator.py: EditRegenerator.handle_message_edit()` must map edited events back to the same thread/session identity as live dispatch.
- `src/mindroom/handled_turns.py: HandledTurnLedger.record_handled_turn(), _normalized_conversation_target(), _serialized_record(), _conversation_target_for_record()` must persist and restore conversation targets with the same source-versus-resolved thread semantics.

### Startup, scheduling, and non-thread regressions to preserve

- `src/mindroom/bot.py: AgentBot.join_configured_rooms(), _post_join_room_setup(), ensure_rooms(), start()` and `src/mindroom/orchestrator.py` startup paths must still satisfy room-join, welcome-message, and scheduling restoration behavior after the thread refactor.
- `src/mindroom/hooks/sender.py: send_hook_message(), build_hook_message_sender()` are downstream consumers of already-resolved thread IDs; they are not expected to recompute membership, but they must be re-verified because they emit Matrix messages and notify outbound cache writes.
- `src/mindroom/scheduling.py: _build_workflow_message_content(), _build_scheduled_failure_content(), _notify_scheduled_workflow_failure(), _execute_scheduled_workflow()` are also downstream consumers of `MessageTarget.for_scheduled_task()` rather than membership calculators; keep them on the verification checklist even if no direct code change is needed.
- `src/mindroom/matrix/stale_stream_cleanup.py: cleanup_stale_streaming_messages(), auto_resume_interrupted_threads(), _assign_latest_thread_event_ids(), _cleanup_one_stale_message(), _build_auto_resume_content()` are restart/repair paths that may surface thread-order or latest-event assumptions and therefore need explicit regression coverage after the invariant changes.
- `tests/test_bot_scheduling.py`, `tests/test_multi_agent_bot.py`, and related startup tests remain mandatory guardrails because recent thread fixes already regressed startup once.

## Documentation Impact Map

The docs must be updated as a contract change, not as a generic polish pass.

### Primary architecture contract

- `docs/architecture/matrix.md`
  - Owns the canonical public description of Matrix thread semantics.
  - Must replace the current single-hop wording with the final transitive `effective_thread_id(event)` rule.
  - Must explicitly say that plain-reply chains inherit transitively when they eventually reach a threaded ancestor or proven thread root.
  - Must remove any “direct target only” or “does not walk plain reply chains” wording.

- `README.md`
  - Owns the user-facing product promise for how bridges and plain replies behave.
  - The “Agent Response Rules” section must match `docs/architecture/matrix.md`, not a simplified or outdated subset.
  - The `thread_summary` section must continue to describe resolved-thread defaults, not explicit-thread-only defaults.

- `docs/architecture/bot-runtime.md`
  - Owns the runtime vocabulary and boundary terminology.
  - Must stop saying the runtime resolves only “explicit thread identity”.
  - Must explain that runtime resolution now means canonical transitive thread membership plus the `source_thread_id` versus `resolved_thread_id` split.

### Tool contracts and operator docs

- `docs/tools/matrix-and-attachments.md`
  - Owns the behavior contract for `matrix_message`, `thread_tags`, `thread_summary`, `matrix_api`, and `attachments`.
  - Must document resolved-thread defaults consistently across all thread-aware tools.
  - Must describe `matrix_api` dangerous writes accurately: blocked by default, opt-in with `allow_dangerous=true`, and still hard-blocking truly forbidden event types.
  - Must keep the low-level `matrix_api` rule clear: it does not infer event IDs or state keys from thread context.

- `docs/tools/index.md`
  - Owns the high-level explanation of tool runtime context.
  - Must say tools run with the current room and resolved thread context, not imply a narrower explicit-thread-only model.

### Verification and contributor docs

- `docs/dev/exhaustive-live-test-checklist.md`
  - Owns the live verification matrix for thread behavior.
  - Must replace all single-hop expectations with transitive chain expectations.
  - Must explicitly require hot-path and cold-path parity checks for transitive plain-reply chains.

- `docs/dev/general-agent-guides/agents/testing-specialist.md`
  - Owns the specialized conversational testing instructions.
  - Must align the testing instructions with the final transitive inheritance behavior.
  - Must preserve the repo’s one-sentence-per-line Markdown rule while doing so.

### Generated mirrors that must stay in sync

- `skills/mindroom-docs/references/page__architecture__matrix__index.md`
- `skills/mindroom-docs/references/page__tools__matrix-and-attachments__index.md`
- `skills/mindroom-docs/references/page__tools__index.md`
- `skills/mindroom-docs/references/llms-full.txt`

These are not optional.

Any architecture or tool-doc contract change above must be mirrored here in the same pass, otherwise doc-driven agents and doc review will keep reporting contradictory behavior.

## Files And Responsibilities

### Canonical invariant and archaeology

- Inspect old implementation: `src/mindroom/matrix/reply_chain.py` at `2637e0e21`
- Modify: `src/mindroom/matrix/thread_membership.py`
- Modify: `src/mindroom/matrix/event_info.py`
- Test: `tests/test_threading_error.py`

### Inbound dispatch and batching

- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/coalescing.py`
- Modify: `src/mindroom/bot.py`
- Test: `tests/test_thread_mode.py`
- Test: `tests/test_threading_error.py`
- Test: `tests/test_live_message_coalescing.py`
- Test: `tests/test_multi_agent_bot.py`

### Cold history reconstruction and durable cache

- Modify: `src/mindroom/matrix/client.py`
- Modify: `src/mindroom/matrix/cache/event_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_reads.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Test: `tests/test_thread_history.py`
- Test: `tests/test_event_cache.py`
- Test: `tests/test_threading_error.py`

### Tool, hook, and normalization surfaces

- Modify: `src/mindroom/thread_tags.py`
- Modify: `src/mindroom/custom_tools/matrix_message.py`
- Modify: `src/mindroom/custom_tools/subagents.py`
- Modify: `src/mindroom/custom_tools/thread_tags.py`
- Modify: `src/mindroom/custom_tools/thread_summary.py`
- Modify: `src/mindroom/custom_tools/attachments.py`
- Modify: `src/mindroom/custom_tools/attachment_helpers.py`
- Modify: `src/mindroom/custom_tools/matrix_api.py`
- Modify: `src/mindroom/tool_system/runtime_context.py`
- Modify: `src/mindroom/tool_system/tool_hooks.py`
- Modify: `src/mindroom/commands/handler.py`
- Test: `tests/test_thread_tags.py`
- Test: `tests/test_thread_tags_tool.py`
- Test: `tests/test_thread_summary_tool.py`
- Test: `tests/test_attachments_tool.py`
- Test: `tests/test_matrix_api_tool.py`
- Test: `tests/test_matrix_message_tool.py`
- Test: `tests/test_subagents.py`
- Test: `tests/test_tool_hooks.py`
- Test: `tests/test_skills.py`

### Follow-up effects and persistence seams

- Modify: `src/mindroom/post_response_effects.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/thread_summary.py`
- Modify: `src/mindroom/message_target.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/edit_regenerator.py`
- Modify: `src/mindroom/handled_turns.py`
- Test: `tests/test_streaming_behavior.py`
- Test: `tests/test_streaming_edits.py`
- Test: `tests/test_edit_response_regeneration.py`
- Test: `tests/test_bot_scheduling.py`

### Docs and contract updates

- Modify: `docs/architecture/matrix.md`
- Modify: `docs/architecture/bot-runtime.md`
- Modify: `README.md`
- Modify: `docs/tools/index.md`
- Modify: `docs/tools/matrix-and-attachments.md`
- Modify: `docs/dev/exhaustive-live-test-checklist.md`
- Modify: `docs/dev/general-agent-guides/agents/testing-specialist.md`
- Modify mirrored references if docs change:
  - `skills/mindroom-docs/references/llms-full.txt`
  - `skills/mindroom-docs/references/page__tools__matrix-and-attachments__index.md`
  - `skills/mindroom-docs/references/page__architecture__matrix__index.md`
- Update PR body after implementation verification

## Task 1: Recover the semantic baseline from before reply-chain removal

**Files:**
- Inspect: `src/mindroom/matrix/reply_chain.py` at commit `2637e0e21`
- Inspect: commit `380214dba`
- Modify: `docs/superpowers/plans/2026-04-15-transitive-thread-membership-restoration.md`

- [ ] **Step 1: Extract the pre-removal reply-chain rules**

Run:

```bash
git --no-pager show 2637e0e21:src/mindroom/matrix/reply_chain.py | sed -n '1,260p'
git --no-pager show --stat --summary 380214dba
```

Expected:
- Understand how the old implementation traversed plain replies, edits, and roots
- Record any traversal guards that must survive in the new helper

- [ ] **Step 2: Write down the final invariant and edge conditions**

Update this plan with:
- the precise traversal rules
- the stop conditions
- which callers are allowed to use cached membership directly

- [ ] **Step 3: Commit the plan update if it changed materially**

```bash
git add docs/superpowers/plans/2026-04-15-transitive-thread-membership-restoration.md
git commit -m "docs: refine transitive thread membership plan"
```

## Task 2: Replace single-hop membership with one shared transitive invariant

**Files:**
- Modify: `src/mindroom/matrix/thread_membership.py`
- Modify: `src/mindroom/matrix/event_info.py`
- Test: `tests/test_threading_error.py`

- [ ] **Step 1: Write failing invariant tests**

Add tests for:
- plain reply to threaded event => threaded
- plain reply to promoted plain reply => threaded
- edit of promoted plain reply => threaded
- reaction/redaction to promoted plain reply => threaded
- cycles and hop limit => safe fail-closed

- [ ] **Step 2: Run the targeted failures**

Run:

```bash
.venv/bin/pytest tests/test_threading_error.py -k 'promoted_plain_reply or redaction or reaction or cycle' -x -n 0 --no-cov -q
```

Expected: FAIL against current single-hop behavior

- [ ] **Step 3: Rebuild `resolve_event_thread_id()` as the only canonical membership rule**

Implementation notes:
- follow direct parents transitively
- allow cached membership for any direct target because persisted mapping is now canonical
- preserve explicit thread metadata as the fastest path
- keep visited-set and hop limit

- [ ] **Step 4: Re-run the invariant tests**

Run:

```bash
.venv/bin/pytest tests/test_threading_error.py -k 'promoted_plain_reply or redaction or reaction or cycle' -x -n 0 --no-cov -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/thread_membership.py src/mindroom/matrix/event_info.py tests/test_threading_error.py
git commit -m "refactor: restore transitive thread membership"
```

## Task 3: Route inbound resolution, coalescing, and batching through the invariant

**Files:**
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/turn_controller.py`
- Modify: `src/mindroom/coalescing.py`
- Modify: `src/mindroom/bot.py`
- Test: `tests/test_thread_mode.py`
- Test: `tests/test_live_message_coalescing.py`
- Test: `tests/test_threading_error.py`

- [ ] **Step 1: Write failing routing and coalescing tests**

Cover:
- room-level message + bridged reply to thread must not coalesce together incorrectly
- active-thread bypass must trigger for transitive plain-reply chains
- reactions should surface the same thread ID as resolver

- [ ] **Step 2: Run the targeted failures**

Run:

```bash
.venv/bin/pytest tests/test_thread_mode.py tests/test_live_message_coalescing.py tests/test_threading_error.py -k 'coalesc or active_thread or reaction' -x -n 0 --no-cov -q
```

- [ ] **Step 3: Replace local membership logic with shared invariant calls**

Implementation notes:
- `coalescing_thread_id()` must use the same `effective_thread_id`
- pre-gate coalescing and active-thread bypass must see the same answer as later dispatch
- reaction hook context must stay on the same rule

- [ ] **Step 4: Re-run the targeted slice**

Run the same command and verify PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/conversation_resolver.py src/mindroom/turn_controller.py src/mindroom/coalescing.py src/mindroom/bot.py tests/test_thread_mode.py tests/test_live_message_coalescing.py tests/test_threading_error.py
git commit -m "fix: align routing and batching with transitive thread membership"
```

## Task 4: Make cold history reconstruction and durable cache obey the same rule

**Files:**
- Modify: `src/mindroom/matrix/client.py`
- Modify: `src/mindroom/matrix/cache/event_cache.py`
- Modify: `src/mindroom/matrix/cache/thread_reads.py`
- Modify: `src/mindroom/matrix/cache/thread_writes.py`
- Modify: `src/mindroom/matrix/conversation_cache.py`
- Test: `tests/test_thread_history.py`
- Test: `tests/test_event_cache.py`
- Test: `tests/test_threading_error.py`

- [ ] **Step 1: Write failing hot/cold parity tests**

Cover:
- room-scan fallback includes full transitive plain-reply chain
- cache hit order matches fresh room-scan order
- outbound live/sync writes persist transitive membership
- missing write coordinator fails open without spurious background error noise

- [ ] **Step 2: Run the failures**

Run:

```bash
.venv/bin/pytest tests/test_thread_history.py tests/test_event_cache.py tests/test_threading_error.py -k 'room_scan or transitive or cache_hit or write_coordinator' -x -n 0 --no-cov -q
```

- [ ] **Step 3: Update room-scan reconstruction and cache persistence**

Implementation notes:
- `_resolve_scanned_thread_message_sources()` should reach a fixpoint using the shared transitive invariant
- event cache should preserve causal ordering for tied timestamps
- thread reads should not assume a coordinator exists
- thread writes should persist every transitive member of `T`

- [ ] **Step 4: Re-run the slice**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/matrix/client.py src/mindroom/matrix/cache/event_cache.py src/mindroom/matrix/cache/thread_reads.py src/mindroom/matrix/cache/thread_writes.py src/mindroom/matrix/conversation_cache.py tests/test_thread_history.py tests/test_event_cache.py tests/test_threading_error.py
git commit -m "fix: align hot and cold thread history with transitive membership"
```

## Task 5: Reconnect tools, hooks, and normalization to the invariant

**Files:**
- Modify: `src/mindroom/thread_tags.py`
- Modify: `src/mindroom/custom_tools/matrix_message.py`
- Modify: `src/mindroom/custom_tools/subagents.py`
- Modify: `src/mindroom/custom_tools/thread_tags.py`
- Modify: `src/mindroom/custom_tools/thread_summary.py`
- Modify: `src/mindroom/custom_tools/attachments.py`
- Modify: `src/mindroom/custom_tools/attachment_helpers.py`
- Modify: `src/mindroom/custom_tools/matrix_api.py`
- Modify: `src/mindroom/tool_system/runtime_context.py`
- Modify: `src/mindroom/tool_system/tool_hooks.py`
- Modify: `src/mindroom/commands/handler.py`
- Test: `tests/test_thread_tags.py`
- Test: `tests/test_thread_tags_tool.py`
- Test: `tests/test_thread_summary_tool.py`
- Test: `tests/test_attachments_tool.py`
- Test: `tests/test_matrix_api_tool.py`
- Test: `tests/test_matrix_message_tool.py`
- Test: `tests/test_subagents.py`
- Test: `tests/test_tool_hooks.py`
- Test: `tests/test_skills.py`

- [ ] **Step 1: Write failing tool-path regressions**

Cover:
- tag/summary/attachment helpers accept transitive inherited context
- `matrix_api` real sends/redactions use transitive membership
- `matrix_api` dry runs stay purely local
- `matrix_message` and `subagents` default to the same resolved thread context and outbound cache bookkeeping contract as the main runtime
- tool runtime preserves source vs resolved thread correctly

- [ ] **Step 2: Run the failures**

Run:

```bash
.venv/bin/pytest tests/test_thread_tags.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py tests/test_attachments_tool.py tests/test_matrix_api_tool.py tests/test_matrix_message_tool.py tests/test_subagents.py tests/test_tool_hooks.py tests/test_skills.py -k 'thread or attachment or dry_run' -x -n 0 --no-cov -q
```

- [ ] **Step 3: Remove local thread logic from tools**

Implementation notes:
- normalize by canonical `effective_thread_id`, then root
- `matrix_api` must keep dry-run classification local
- thread-affecting outbound bookkeeping must use canonical membership
- `matrix_message` and `subagents` must not preserve their own narrower room/thread defaulting rules

- [ ] **Step 4: Re-run the slice**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/thread_tags.py src/mindroom/custom_tools/matrix_message.py src/mindroom/custom_tools/subagents.py src/mindroom/custom_tools/thread_tags.py src/mindroom/custom_tools/thread_summary.py src/mindroom/custom_tools/attachments.py src/mindroom/custom_tools/attachment_helpers.py src/mindroom/custom_tools/matrix_api.py src/mindroom/tool_system/runtime_context.py src/mindroom/tool_system/tool_hooks.py src/mindroom/commands/handler.py tests/test_thread_tags.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py tests/test_attachments_tool.py tests/test_matrix_api_tool.py tests/test_matrix_message_tool.py tests/test_subagents.py tests/test_tool_hooks.py tests/test_skills.py
git commit -m "fix: align tools and hooks with transitive thread membership"
```

## Task 6: Align follow-up effects, persistence, and delivery seams

**Files:**
- Modify: `src/mindroom/post_response_effects.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/thread_summary.py`
- Modify: `src/mindroom/message_target.py`
- Modify: `src/mindroom/delivery_gateway.py`
- Modify: `src/mindroom/streaming.py`
- Modify: `src/mindroom/edit_regenerator.py`
- Modify: `src/mindroom/handled_turns.py`
- Test: `tests/test_streaming_behavior.py`
- Test: `tests/test_streaming_edits.py`
- Test: `tests/test_edit_response_regeneration.py`
- Test: `tests/test_bot_scheduling.py`
- Test: `tests/test_multi_agent_bot.py`

- [ ] **Step 1: Write failing follow-up tests**

Cover:
- thread summaries on transitive reply chains
- compaction notices stay in the resolved thread
- regeneration sees the same thread/session identity as live dispatch
- startup welcome / scheduling restoration still work after any routing changes

- [ ] **Step 2: Run the failures**

Run:

```bash
.venv/bin/pytest tests/test_streaming_behavior.py tests/test_streaming_edits.py tests/test_edit_response_regeneration.py tests/test_bot_scheduling.py tests/test_multi_agent_bot.py -k 'thread_summary or compaction or regeneration or welcome or restore' -x -n 0 --no-cov -q
```

- [ ] **Step 3: Update follow-up surfaces to consume the canonical resolved thread**

Implementation notes:
- no post-response path should recompute thread membership ad hoc
- summary fallback and delivery targeting should stay consistent with routing

- [ ] **Step 4: Re-run the slice**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/post_response_effects.py src/mindroom/response_runner.py src/mindroom/thread_summary.py src/mindroom/message_target.py src/mindroom/delivery_gateway.py src/mindroom/streaming.py src/mindroom/edit_regenerator.py src/mindroom/handled_turns.py tests/test_streaming_behavior.py tests/test_streaming_edits.py tests/test_edit_response_regeneration.py tests/test_bot_scheduling.py tests/test_multi_agent_bot.py
git commit -m "fix: align follow-up effects with transitive thread membership"
```

## Task 7: Rewrite docs and contracts to match the final invariant

**Files:**
- Modify: `docs/architecture/matrix.md`
- Modify: `docs/architecture/bot-runtime.md`
- Modify: `README.md`
- Modify: `docs/tools/index.md`
- Modify: `docs/tools/matrix-and-attachments.md`
- Modify: `docs/dev/exhaustive-live-test-checklist.md`
- Modify: `docs/dev/general-agent-guides/agents/testing-specialist.md`
- Modify mirrored references if needed

- [ ] **Step 1: Update the public contract**

Document:
- transitive plain-reply inheritance
- no special single-hop exception
- explicit `m.thread` still primary, but inherited membership is transitive
- what thread tools default to

- [ ] **Step 2: Update generated mirrors**

Regenerate or hand-update:
- `skills/mindroom-docs/references/llms-full.txt`
- `skills/mindroom-docs/references/page__tools__matrix-and-attachments__index.md`
- `skills/mindroom-docs/references/page__architecture__matrix__index.md`

- [ ] **Step 3: Update the PR body**

Run:

```bash
gh pr edit 575 --body-file /tmp/pr575-body.md
```

Expected:
- Summary says transitive thread membership, not explicit-only or single-hop

- [ ] **Step 4: Commit**

```bash
git add README.md docs/architecture/matrix.md docs/architecture/bot-runtime.md docs/tools/index.md docs/tools/matrix-and-attachments.md docs/dev/exhaustive-live-test-checklist.md docs/dev/general-agent-guides/agents/testing-specialist.md skills/mindroom-docs/references/llms-full.txt skills/mindroom-docs/references/page__tools__matrix-and-attachments__index.md skills/mindroom-docs/references/page__architecture__matrix__index.md
git commit -m "docs: describe transitive thread membership"
```

## Task 8: Final verification from the restored invariant backward

**Files:**
- Modify only if verification finds a real issue
- Test: full affected thread/matrix suite

- [ ] **Step 1: Run the broad focused suite**

```bash
.venv/bin/pytest tests/test_thread_history.py tests/test_threading_error.py tests/test_thread_mode.py tests/test_live_message_coalescing.py tests/test_matrix_api_tool.py tests/test_thread_tags.py tests/test_thread_tags_tool.py tests/test_thread_summary_tool.py tests/test_attachments_tool.py tests/test_multi_agent_bot.py tests/test_streaming_behavior.py tests/test_streaming_edits.py tests/test_edit_response_regeneration.py tests/test_bot_scheduling.py -x -n 0 --no-cov -q
```

- [ ] **Step 2: Run touched-file pre-commit**

```bash
.venv/bin/pre-commit run --files <touched files>
```

- [ ] **Step 3: Push and watch CI**

```bash
git push
gh pr checks 575 --watch
```

- [ ] **Step 4: Commit only if verification uncovered a final real bug**

```bash
git add <touched files>
git commit -m "fix: close transitive thread membership verification gaps"
```

## Notes For Execution

- Do **not** try to preserve the current single-hop behavior.
- Do **not** reintroduce ad hoc local reply walking in multiple modules.
- Use `2637e0e21` / `380214dba` as the semantic reference point, but do **not** resurrect `reply_chain.py` wholesale if the current cache/runtime boundaries already provide cleaner integration points.
- The expected outcome is not “old file restored.”
- The expected outcome is “old semantics restored through one modern shared invariant.”
