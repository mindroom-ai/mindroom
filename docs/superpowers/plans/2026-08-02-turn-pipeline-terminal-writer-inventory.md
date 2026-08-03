# Terminal-Writer Call-Site Inventory (Phase 0)

Companion artifact to `2026-08-02-turn-pipeline-lifecycle-refactor.md`.
Inventoried at `a66c8bec` against the working tree.
Covers every writer of terminal turn records and every writer of non-terminal durable turn records.
Phase 7 extracts shared `TurnRecord` construction only where this inventory proves real duplication.

## 1. The write surface (`src/mindroom/turn_store.py`)

All durable turn-record writes funnel through **`HandledTurnLedger.update_handled_turn`** (`src/mindroom/handled_turns.py:422`), the single atomic read-merge-write primitive (`wait_for_persist` + `on_persisted` knobs).
`TurnStore` methods on top of it:

| Method | Signature / line | Durability | Writes |
|---|---|---|---|
| `record_turn` | `(turn_record)` — turn_store.py:106 | async (no wait) | Terminal (`completed=True` forced at :152) |
| `record_responded_turn` | `(turn_record)` — :110; requires `response_event_id` | async | Terminal, delegates to `record_turn` (:115) |
| `record_turn_durably` | `(turn_record)` — :117 | waits for exact persist | Terminal |
| `_record_terminal_turn` | `(turn_record, *, wait_for_persist)` — :121 | both; ledger write at :161, `on_persisted=_notify_terminal_turn_persisted` | **The chokepoint**: canonicalize -> skip empty sources -> merge with compatible existing record -> `completed=True`, merged redaction markers, `visible_echo_event_id`, `timestamp=0.0` |
| `record_pending_turn` | `(turn_record)` — :315 | `wait_for_persist=True` (:356) | **Non-terminal** (`completed=False`, :348), merges redaction markers, computes pending cleanup ids |
| `record_visible_echo` | `(source_event_id, echo_event_id)` — :180 | async (:191) | Non-terminal: sets `visible_echo_event_id`, preserves `completed` |
| `record_finalized_visible_echo` | `(source_event_id, echo_event_id, *, is_fallback)` — :193 | async (:218) | Sets `visible_echo_is_fallback`; sets `response_event_id=echo_event_id` **only when record not completed** (:213) — does not set `completed` |
| `record_user_stopped_response` | `(response_event_id, stop_receipt_order, *, delivery_settled=False)` — :276 | `wait_for_persist=True` via `_update_response_turn` (:269) | Terminal: `with_user_stop` -> `completed=True`, `response_event_id`, `user_stop_receipt_order`, `user_stop_settled_receipt_order` |
| `mark_source_redacted` | `(source_event_id)` — :402 | `wait_for_persist=True` (:424) | Redaction tombstone: `redacted_source_event_ids` += source, `pending_redaction_cleanup_event_ids` += source when cleanup context exists; treated as handled by `_has_responded_locked` (handled_turns.py:485) |
| `prepare_edit_response_source` | `(*, target, source_event_ids, response_event_id, edit_receipt_order)` — :482 | durable via `_update_response_turn` (:517) | Mutates existing record: `latest_edit_receipt_order`, `user_stop_settled_receipt_order` |
| `prepare_pending_response_source` | `(*, target, source_event_ids, terminal_source_event_ids)` — :469 | durable | Runs owed redaction cleanup (`_clear_pending_redaction_cleanup` write at :693) + Agno session surgery (:695 `_remove_redacted_event_from_scope` — separate store) |
| `load_turn` | `(*, room, thread_id, original_event_id, requester_user_id)` — :576 | async (:611) | Read-**repair** write: reconciles ledger record with Agno run metadata, writes repaired record back |
| `try_claim_turn` / `release_pending_turn_claim` / `wait_for_turn_settled` | :367 / :385 / :392 | **in-memory only** (not durable) | Pending-response claims |
| `_update_response_turn` | `(response_event_id, update, *, notify_terminal=False)` — :246 | `wait_for_persist=True` (:269) | Shared helper for response-owning-record mutation |

Ledger-level (`handled_turns.py`): `update_handled_turn` (:422), `record_handled_turn` (:415 — **no production callers**; removal candidate), `_cleanup_old_events` (:739, prune/delete write), `flush` (:410).

### `TurnRecord` fields (`src/mindroom/turn_record.py:108-134`)

- Identity: `source_event_ids`, `discovery_event_ids`, `anchor_event_id` (`indexed_event_ids` = sources+discovery).
- Redaction: `redacted_source_event_ids`, `pending_redaction_cleanup_event_ids`.
- Delivery: `response_event_id`, `visible_echo_event_id`, `visible_echo_is_fallback`.
- Outcome: `completed` (default True), `timestamp`.
- Edit facts: `source_event_prompts`, `source_event_revisions`, `suppressed_source_event_revisions`.
- Dispatch ordering: `latest_edit_receipt_order`, `user_stop_receipt_order`, `user_stop_settled_receipt_order`.
- Source metadata: `source_event_metadata` (`SourceEventMetadata{sender, timestamp_ms, discovery_event_id}`).
- Context: `response_owner`, `requester_id`, `correlation_id`, `history_scope`, `conversation_target`.
- Command journal: `command_execution_started`, `command_result_text`.

## 2. Terminal write call sites (by writing module)

### `src/mindroom/ingress_validation.py` — `IngressValidator.precheck_event`

- **:298** — unauthorized sender -> ignored-turn tombstone. `record_turn(TurnRecord.create([event.event_id]))`. Fields: `source_event_ids`, `completed=True`; no response.
- **:302** — sender fails `can_reply_to_sender` -> identical ignored-turn tombstone.

### `src/mindroom/text_ingress_dispatch.py`

- **:436** `_apply_turn_plan` (ignore plan, reason `"router"`) — router turn already has a finalized visible echo -> `record_responded_turn(router_outcome)` where outcome comes from `TurnController._router_handled_turn_outcome` (turn_controller.py:1730-1742). Outcome: **ignored router turn bound to its visible voice echo** (`response_event_id` = echo event).

### `src/mindroom/turn_controller.py`

- **:971** `_notify_command_target_not_ready` — router-only: command whose conversation can't resolve yet; after visible "not ready" text -> `record_responded_turn(pending_turn + response_event_id)`. Outcome: **failed command with visible error**.
- **:1411** `_execute_interactive_selection` — selection payload build failed; `_finalize_dispatch_failure` delivered visible error -> `record_responded_turn(selection_handled_turn + response_event_id)`. Outcome: **failed interactive selection**.
- **:1466** `_execute_interactive_selection` — generated selection response returned -> `record_responded_turn(...)`. Outcome: **completed interactive-selection answer**.
- **:1703** `_execute_router_relay` — recovery found the relay's visible response already on Matrix -> `record_responded_turn(tracked_handled_turn + recovered_response_event_id)`. Outcome: **recovered router relay**.
- **:1722** `_execute_router_relay` — relay text delivered -> `record_responded_turn(tracked_handled_turn + event_id)` (immediately after a pending bind at :1721). Outcome: **completed router relay**.
- **:1790** `_build_response_settlement_callbacks.record_deferred_outcome` (closure) — `record_responded_turn(handled_turn + response_event_id)`. Invoked by `response_runner.py:1605` when a **deferred visible outcome** (error/cancel with `mark_handled`) lands durably.
- **:1795** `_build_response_settlement_callbacks.record_user_stop` (closure) — `record_turn_durably(with_user_stop(handled_turn, response_event_id, stop_receipt_order, delivery_settled=True))`. Invoked by `response_runner.py:1251` (`_record_user_stop_handled`). Outcome: **terminal user-stop** with `user_stop_receipt_order` + `user_stop_settled_receipt_order`.
- **:1856** `_execute_response_action` (reject branch) — rejection message delivered -> `record_responded_turn(...)`. Outcome: **completed "reject" response**.
- **:1980** `_execute_response_action` (`PostLockRequestPreparationError`) — payload-prep failure finalized visibly -> `record_responded_turn(...)`. Outcome: **failed response with visible error**.
- **:1985** `_execute_response_action` — `response_event_id` returned from runner -> `record_responded_turn(...)`. Outcome: **completed model response** — also covers failed/cancelled turns whose terminal visible note reached Matrix with `mark_handled` (see response_runner.py:1589-1607).

### `src/mindroom/edit_regenerator.py` — `EditRegenerator`

- **:292** `_build_request` (no active edit left) — `record_turn(record)`. Outcome: **terminal edit-revision bookkeeping without regeneration** (edits applied/suppressed by hook or STOP). Fields: `source_event_prompts`, `source_event_revisions`, `suppressed_source_event_revisions`; no `response_event_id`.
- **:365** `_build_request` (`on_deferred_outcome_handled` lambda) — `record_responded_turn(record + response_event_id)` if `applied`. Same role as turn_controller.py:1790, for edit regeneration; invoked at response_runner.py:1605.
- **:372** `_build_request` (`on_user_stop_handled` lambda) — `record_turn_durably(with_user_stop(record, ..., delivery_settled=True))` if `applied`. Same role as turn_controller.py:1795; invoked at response_runner.py:1251.
- **:432** `_drain_claimed` — regenerated response delivered -> `record_responded_turn(record + regenerated_event_id)`. Outcome: **completed edit regeneration**.

### `src/mindroom/command_turn_executor.py` — `CommandTurnExecutor`

- **:136** `execute.record_command_turn` (closure -> `CommandHandlerContext.record_handled_turn`, wired at :153) — `record_responded_turn(active_command_turn + outcome.response_event_id)`. Invoked from `commands/handler.py:437` (config-set confirmation preview) and **`commands/handler.py:487`** (generic command result). Outcome: **completed command with visible response**.
- **:194** `_recover_visible_response` — replayed command whose response is already visible -> `record_responded_turn(command_turn + response_event_id)`. Outcome: **recovered command terminal**.
- **:238** `_deliver_checkpointed_result` — checkpointed `command_result_text` redelivered -> `record_responded_turn(...)`. Outcome: **completed command-result redelivery**.

### `src/mindroom/user_stop_reconciliation.py` — `UserStopReconciler`

- **:47** `_record` — `record_user_stopped_response(response_event_id, stop_receipt_order, delivery_settled=...)`. Called from `_finalize_under_lock` (:76, second call :85 with `delivery_settled=True`) and `finalize` (:111). Entry: 🛑 reaction -> `reaction_dispatch.py:134` `_maybe_handle_stop_reaction` -> `user_stop_reconciler.finalize`. Outcome: **terminal user-stop on the response-owning turn** (fields as in `with_user_stop`, handled_turns.py:67-92).

### `src/mindroom/redacted_turn_cleanup.py` — `RedactedTurnCleanup.handle`

- **:32** — `mark_source_redacted(event.redacts)` for every inbound `RedactionEvent`. Outcome: **redaction tombstone** (counts as handled for replay suppression; schedules pending cleanup).

### Internal repair writers in `turn_store.py`

- **:611** `load_turn` — read-repair write after reconciling ledger vs. Agno run metadata. Callers: `edit_regenerator.py:151`, `:159` (`handle_message_edit`), `:249` (`_build_request`).
- **:517** `prepare_edit_response_source` — durable receipt-order mutation. Sole caller: `edit_regenerator.py:354`.
- **:693** `_clear_pending_redaction_cleanup` — cleanup-ack write, reached via `prepare_pending_response_source` (:469) from `turn_controller.py:1455` and `:1949` (invoked under response lock at `response_runner.py:1275`).

## 3. Non-terminal durable write call sites

### `record_pending_turn` (durable, `completed=False`)

- **`text_ingress_dispatch.py:463-466`** `_apply_turn_plan` (respond plan) — pending **response intent** before generation. Fields: full `handled_turn` (source ids/prompts/metadata from coalescing) + `response_owner`, `history_scope`, `conversation_target` (via `attach_response_context` at :458). Aborts if merged record came back completed (:467) or redacted (:469).
- **`turn_controller.py:1346-1349`** `_execute_interactive_selection` — pending **interactive selection** turn. Fields: source=`question_event_id`, discovery=`source_event_id`, `requester_id`, `correlation_id`, `history_scope` (individual), `conversation_target`.
- **`visible_response_reconciliation.py:134-137`** `record_pending_visible_response` — binds a **visible `response_event_id`** to an incomplete turn (`completed=False`). Called from: `recovered_response_event_id` (:125), `deliver_recoverable_text` (:155), `turn_controller.py:1721` (router relay), `turn_controller.py:1925-1926` (`record_visible_response` closure -> `ResponseRequest.on_visible_response`, invoked at `response_runner.py:1327` for placeholders and `:1524` for streamed run messages), `turn_controller.py:1405-1408` (selection failure path).
- **`visible_response_reconciliation.py:180`** `prepare_visible_delivery_turn` — pending **non-model delivery intent** (`requester_id`, `correlation_id`, `conversation_target`, `history_scope=None`). Callers: `command_turn_executor.py:90`, `turn_controller.py:954` (`_notify_command_target_not_ready`), `turn_controller.py:1693` (`_execute_router_relay`).
- **`command_turn_executor.py:206-217`** `_persist_checkpoint` — **command journal milestones** (`command_execution_started`, `command_result_text`). Called from `execute.record_command_result` (:130, invoked by `commands/handler.py:481`), `_resume_or_start` (:251 uncertain-outcome checkpoint, :264 side-effect-started checkpoint).

### Visible-echo tracking (durable, non-completing)

- **`visible_voice_echo.py:407`** `_send_placeholder` — adopt recovered echo -> `record_visible_echo`.
- **`visible_voice_echo.py:422`** `_send_placeholder` — new placeholder sent -> `record_visible_echo`.
- **`visible_voice_echo.py:451`** `_settle` — adopt recovered echo -> `record_visible_echo`.
- **`visible_voice_echo.py:463`** `_settle` — new echo sent -> `record_visible_echo`.
- **`visible_voice_echo.py:477`** `_settle` — echo replaced by final transcript -> `record_finalized_visible_echo` (sets `response_event_id` on non-completed records + `visible_echo_is_fallback`).

### In-memory (non-durable) claim writes

- `try_claim_turn`: `text_ingress_dispatch.py:191` (`_try_claim_turn`), `turn_controller.py:2067`/`:2073` (`_claim_live_turn`), `edit_regenerator.py:398`/`:411` (`_drain`).
- `release_pending_turn_claim`: `text_ingress_dispatch.py:178` (`dispatch_text_message` finally), `:546` (`_run_claimed_response` finally); `turn_controller.py:891` (`_dispatch_prepared_text_like_ingress` command branch), `:1061` (`_enqueue_for_dispatch` claim-metadata close), `:2160` (`_handle_message_inner` finally), `:2313`/`:2364` (`_handle_media_message_inner`), `:2477`/`:2589` (`_ready_voice_event`); `edit_regenerator.py:417` (`_drain` finally).
- `wait_for_turn_settled`: `turn_controller.py:2070`, `edit_regenerator.py:400`.

### Separate durable store (not turn records, listed for completeness)

`dispatch_obligations/storage.py` (SQLite): `create_pending` (:281, obligation admission), `mark_callback_deferred` (:501), `settle_from_turn_store` (:526; via `runner.py:636-660` `_settle_from_turn_store_if_owned` at runner.py:365/373/613/656/660), `settle_pending_from_turn_store` (:622; driven by `turn_settlement_retry.py:45`/`:84`, triggered by `TurnStore.on_terminal_turn_persisted` wired at `bot.py:538`), `settle_intentionally_ignored_turn_sources` (:630; wired as `settle_ignored_sources` at `bot.py:659`, called via `settle_source_events_ignored` from `text_ingress_dispatch.py:438/440/344/350/381`, `turn_controller.py:975/1612/1929`, `visible_response_reconciliation.py:184`, and directly at `bot.py:2206`).

Modules checked with **no turn-ledger writes**: `reaction_dispatch.py` (reads at :117, delegates stop -> UserStopReconciler, selection -> TurnController), `response_terminal.py` (pure helpers), `sync_restart_retry.py` (in-memory `InterruptedTurnRooms.register` at :101 — the `record_interrupted_turn` closures at turn_controller.py:1786-1788 and edit_regenerator.py:333-337 write only there), `response_runner.py`/`response_turn.py` (invoke the callbacks above; no direct TurnStore access), `handled_turns.py` (primitive only).

## 4. Duplication candidates

1. **User-stop terminal write — two chokepoints, same outcome.** `user_stop_reconciliation.py:47` -> `record_user_stopped_response` -> `_update_response_turn`, vs. `turn_controller.py:1795` and `edit_regenerator.py:372` -> `record_turn_durably(with_user_stop(...))` -> `_record_terminal_turn`. Both write `completed=True` + `response_event_id` + `user_stop_receipt_order` + `user_stop_settled_receipt_order` (delivery_settled=True) for "user stopped a response". The two closures in `_build_response_settlement_callbacks` (turn_controller.py:1789-1802) are additionally near-verbatim copies of the lambdas at `edit_regenerator.py:364-382` — a shared callback-builder would remove 2 of 4 sites.
2. **`record_responded_turn(canonicalize_turn_record(record, response_event_id=...))` open-coded 9 times**: turn_controller.py:971, :1411, :1466, :1703, :1722, :1790, :1856, :1980, :1985; edit_regenerator.py:365, :432; command_turn_executor.py:136, :194, :238. Success/failure/recovery variants differ only in which `response_event_id` and record they carry.
3. **Pending->deliver->terminal triple open-coded per non-model flow**: `_notify_command_target_not_ready` (turn_controller.py:954-973), `_execute_router_relay` (:1693-1724), `CommandTurnExecutor.execute` (:90 + handler callbacks), `_execute_interactive_selection` (:1371-1468). Note the **double write** when a pending bind is immediately followed by a terminal record for the same `response_event_id`: turn_controller.py:1721+1722, and `command_turn_executor.py:231-240` (`deliver_recoverable_text` binds pending internally at visible_response_reconciliation.py:155, then :238 writes terminal).
4. **Identical ignored-turn tombstones**: `ingress_validation.py:298` vs `:302` (only the guard differs).
5. **Terminal write without a response** exists in exactly two shapes — ignored tombstones (`ingress_validation`, completed, no `response_event_id`) and edit bookkeeping (`edit_regenerator.py:292`, completed with revision facts, no `response_event_id`) — worth keeping distinct in any refactor since `record_responded_turn` forbids the missing response id.
6. **Dead write path**: `HandledTurnLedger.record_handled_turn` (handled_turns.py:415) has no production callers — removal candidate alongside the refactor.
