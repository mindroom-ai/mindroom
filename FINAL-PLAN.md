# ISSUE-194 — FINAL-PLAN.md (synthesis of PLAN.md + PLAN-B.md + cross-critiques)

**Status:** Phase 0+1 complete. This is the single source of truth for implementation.
**Source plans:**
- Codex `PLAN.md` (1220 lines) at `/srv/mindroom-worktrees/issue-194-plan-codex/PLAN.md` (`95624dedf`)
- Claude `PLAN-B.md` (956 lines) at `/srv/mindroom-worktrees/issue-194-plan-claude/PLAN-B.md` (`da22e7492`)
- Codex critique `CRITIQUE-A.md` at `/srv/mindroom-worktrees/issue-194-plan-codex/CRITIQUE-A.md` (`afdd5ea67`)
- Claude critique `CRITIQUE-B.md` at `/srv/mindroom-worktrees/issue-194-plan-claude/CRITIQUE-B.md` (`dbdbd93f8`)

**Synthesis principle:** The two critiques converged on the SAME 14-row decision table (claude's CRITIQUE-B lines 455–471 and codex's CRITIQUE-A "Synthesis recommendation" section). Where the planners disagreed, this FINAL-PLAN.md adopts the convergent answer per the table below and explains why.

---

## Decision matrix (binding)

| ID | Topic | Source | Why |
|---|---|---|---|
| D1 | Patch `_edit_cache_row` to accept `io.mindroom.tool_approval` | **Codex** | Required correctness — without it `get_latest_edit` is blind to approval edits, breaking resolve detection AND auto-deny idempotency |
| D2 | `resolve_approval(card_event_id, room_id, status, reason)` keyword-only | **PLAN-B** | Drops the need for new `get_event_by_id` cache helper; every caller already has `room_id` |
| D3 | Keep reactions + reply-text resolution paths | **PLAN-B** | Element-vanilla operators rely on these per PR #568 docs; removing breaks wire contract |
| D4 | Add `approver_user_id` to card content payload | **Codex** | Schema delta required; PR #568 only has `requester_id` today; addition is Cinny-backwards-compatible |
| D5 | Single waiter map keyed by `card_event_id` | **PLAN-B** | The "fast-response-before-bind" race PLAN.md tried to defend doesn't exist (Cinny learns event_id from /sync, which lags `room_send`) |
| D6 | Same-sender skip in auto-deny scan | **Codex** | Matrix `m.replace` rejects cross-sender edits anyway; skip is O(my-cards) not O(all) |
| D7 | New cache helper `get_recent_room_events(room_id, *, event_type, since_ts_ms, limit)` | **Codex** | ~25 LOC, uses existing `idx_events_room_origin_ts`, warm-path speedup for auto-deny |
| D8 | Keep `_send_approval_notice` (truncated-arg UX) | **PLAN-B** (decisive) | Truncated-approve-converts-to-deny needs explanation; without it users see silent flip |
| D9 | Omit both `script_path` and `matched_rule` from card content | **PLAN-B** | Wire minimalism; `script_path` is filesystem-path infoleak |
| B1 | `request_approval` cancel safety: `try/finally` with `m.replace` cleanup on `CancelledError` | **PLAN-B** | Closes the cancel-after-send-before-bind window |
| B3 | Auto-deny iterates only original cards via `get_pending_approval` | **PLAN-B** | One filter, one place; avoids `_cached_approval_cards` filter-skew |
| B6 | Hook startup auto-deny into `_handle_bot_ready` (after first /sync) | **PLAN-B** | Warms cache for free before the scan |
| B7 | Wrong-clicker UX: `handle_response_event` returns `False`, accept brief stale optimistic tick | **PLAN-B** | Matches deletion of `restate_pending_anchored_request`; next /sync delivers unchanged card |
| B8 | Outbound cache write: `event_cache.store_event(event_id, room_id, payload)` directly, no hand-rolled `origin_server_ts` | **PLAN-B** | Avoid clock-skew misorder; let cache canonicalize |
| W1–W7 | Codex's 7 wins that claude flagged | **Codex** | Per-method deletion list, implementation order, all included below |

**Open question deferred to implementer:** notify_outbound_event extension vs direct `store_event` call from `_send_approval_event_now`. Both critiques lean direct `store_event` (avoids coupling generic cache pipeline to a single product feature). **Decision: direct `store_event`.** If implementer hits a problem, surface to DevAgent.

---

## Inventory: what to delete

### `src/mindroom/tool_approval.py` (1888 → ~390 LOC)

Codex's per-method deletion list (verbatim from CRITIQUE-A; verified against current source):

**Delete (state + persistence):**
- `_persist_request`
- `_delete_request_file`
- `_request_path`
- `_load_existing`
- `_store_request`
- `_pending_request`
- `_pending_ids_snapshot`
- `_pending_requests_snapshot`
- `runtime_storage_root` (property)
- `storage_dir` (property)
- `_runtime_storage_root` field
- `_storage_dir` field

**Delete (in-flight send tracking):**
- `_pending_send_events` field
- `_start_pending_send`
- `_complete_pending_send`
- `_pending_send_events_for_rooms`
- `pending_send_room_ids`
- `_wait_for_all_pending_sends`
- `_set_event_delivery`

**Delete (reconciliation/retry machinery):**
- `_has_unsynced_resolved`
- `_has_unconfirmed_deliveries`
- `_has_unsynced_resolution_work`
- `_has_unsynced_resolution_work_in_rooms`
- `_room_ids_with_unsynced_resolution_work`
- `list_unsynced_resolved`
- `list_unsynced_resolved_in_rooms`
- `list_unconfirmed_deliveries`
- `_claim_unsynced_resolved_replay`
- `_finish_unsynced_resolved_replay`
- `_ensure_unsynced_resolution_retry_task`
- `_run_unsynced_resolution_retry_loop`
- `cancel_unsynced_resolution_retry_task`
- `reconcile_unsynced_approvals`
- `replay_resolved_card_for_room`
- `restate_pending_anchored_request`
- `recover_unconfirmed_deliveries`
- `deny_anchored_request_for_lost_authorization`

**Delete (room-drained machinery):**
- `_check_and_notify_room_drained`
- `_room_drained_callback_tasks` field
- `_pending_room_leaves` field
- `_drain_approval_state_for_rooms`
- All R23/R25 callback orchestration

**Delete (anchored resolution branches):**
- `_handle_anchored_resolution`
- `_anchored_request`
- `anchored_request_for_event`

**Delete (in-memory dicts):**
- `_pending_by_id` dict (replaced by single `_pending_by_card_event`)
- `_requests_by_id` dict
- `_state_lock` (single lock OK; will be replaced by narrower `_live_lock`)

**Keep (redesigned):**
- `PendingApproval` dataclass — but redesigned per §1 below (frozen Matrix-card view, no `Future`, no full `arguments`)
- `is_authorized_sender` from `mindroom.authorization` (existing function)
- Authorization rule evaluation (`evaluate_tool_approval`, `_load_script_module`, `_check_callable_from_module`, OpenAI compat helper) — these stay in tool_approval.py for ISSUE-194 scope
- `request_approval` — rewritten per §2 below
- `handle_response_event` — rewritten per §4 below
- `_send_approval_notice` (D8 — kept for truncated-arg UX)
- `configure_transport`
- ISSUE-177 cross-loop `concurrent.futures.Future` bridge for sync-tool approval gating

### `tests/test_tool_approval.py` (4275 → ~600 LOC)

**Per-method tests to delete** (codex's exact list, claude approved):

`test_persist_request_atomic_replace`, `test_load_existing_skips_corrupted`, `test_load_existing_recovers_pending`, `test_load_existing_recovers_resolved`, `test_pending_send_event_lifecycle`, `test_complete_pending_send_completes_event`, `test_wait_for_all_pending_sends_*` (all variants), `test_pending_send_room_ids_*`, `test_set_event_delivery_*`, `test_has_unsynced_resolved_*`, `test_has_unsynced_resolution_work_in_rooms_*`, `test_room_ids_with_unsynced_resolution_work_*`, `test_unsynced_resolution_retry_*` (all), `test_run_unsynced_resolution_retry_loop_*` (all), `test_reconcile_unsynced_approvals_*` (all), `test_replay_resolved_card_*`, `test_restate_pending_anchored_request_*`, `test_recover_unconfirmed_deliveries_*`, `test_deny_anchored_request_for_lost_authorization_*`, `test_check_and_notify_room_drained_*`, `test_room_drained_callback_*` (incl. R25 GC test), `test_drain_approval_state_for_rooms_*`, `test_handle_anchored_resolution_*`, `test_anchored_request_for_event_*`, `test_finalize_pending_approvals_for_sender_*`, `test_force_finalize_*`, `test_in_flight_send_*`, plus all R12-R26 marathon-era regression tests for the deleted machinery.

**Tests to keep + minor adjust** (~25 tests):
- `test_request_approval_basic_flow`
- `test_request_approval_denies_on_unauthorized_sender`
- `test_request_approval_truncated_arguments_*`
- `test_resolve_approval_emits_replace_edit`
- `test_resolve_approval_idempotent_on_resolved_card`
- `test_evaluate_tool_approval_*` (rule engine, unchanged)
- `test_load_script_module_*` (rule engine, unchanged)
- `test_handle_response_event_via_custom_event`
- `test_handle_response_event_via_reaction` (D3 — kept)
- `test_handle_response_event_via_reply_text` (D3 — kept)
- `test_authorization_drift_*` (denies + logs, no recovery)
- ISSUE-177 cross-loop sync-tool approval test (preserved)

**Tests to add** (~10 new):
- `test_auto_deny_pending_on_startup_emits_replace_for_each_unresolved_card`
- `test_auto_deny_pending_on_startup_skips_other_routers_cards` (D6)
- `test_auto_deny_pending_on_startup_idempotent_on_rerun`
- `test_auto_deny_pending_on_startup_respects_lookback_window`
- `test_get_pending_approval_returns_none_for_resolved_card`
- `test_get_pending_approval_cache_miss_falls_back_to_room_get_event`
- `test_get_pending_approval_room_history_scan_when_event_missing`
- `test_request_approval_cleans_up_on_cancellation_after_send` (B1)
- `test_edit_cache_row_indexes_io_mindroom_tool_approval_edits` (in test_event_cache.py — D1)
- `test_get_recent_room_events_warm_path` (in test_event_cache.py — D7)

### Other files

- `src/mindroom/orchestrator.py`: -50 to -100 LOC (drain-before-leave, deferred-leave callbacks, `_pending_room_leaves` orchestration all gone)
- `src/mindroom/bot.py`: -30 to -60 LOC (replay-on-ready hook gone; `_handle_bot_ready` gains the `auto_deny_pending_on_startup` call)
- `src/mindroom/bot_room_lifecycle.py`: minor adjustments to remove drain check before leave
- `tests/test_multi_agent_bot.py`: clean up the R10 F2 sender-removal-finalization tests (~50 LOC)
- `tests/test_tool_hooks.py`: ~5-10 line adjustments
- `~/.mindroom-chat/mindroom_data/approvals/` directory: one-time idempotent purge at startup (logs deleted count)

### Diff scope estimate (binding)

- Source: ~-1500 LOC (tool_approval.py 1888→390, plus orchestrator/bot reductions)
- Tests: ~-3500 LOC (test_tool_approval.py 4275→~600 + ancillary cleanup)
- New code: ~+300 LOC (cache helper D7, `_edit_cache_row` patch D1, restart auto-deny, `approver_user_id` schema delta D4)
- **Net: ~-4700 LOC**

---

## §1. The redesigned `PendingApproval` dataclass

**Codex/claude synthesis:** `PendingApproval` is a **frozen typed view of a Matrix card event payload**, NOT a runtime request object. Live waiters live separately.

```python
@dataclass(frozen=True, slots=True)
class PendingApproval:
    """Typed projection of a `io.mindroom.tool_approval` Matrix card event.

    This is a READ-ONLY view derived from Matrix card content. It carries no
    Future, no mutable status, no full tool arguments. Mutable runtime state
    (live waiters, pending sends) lives in private maps on ApprovalManager.
    """
    approval_id: str
    card_event_id: str
    room_id: str
    card_sender_id: str  # the router user ID that sent the card; needed for same-sender check
    requester_id: str
    approver_user_id: str  # NEW per D4 — must be in card content
    tool_name: str
    arguments_preview: str
    arguments_preview_truncated: bool
    timeout_seconds: int
    created_at_ms: int

    @classmethod
    def from_card_event(cls, event: dict[str, Any], *, room_id: str) -> "PendingApproval":
        """Parse a card event payload into a typed view. Raises ValueError on schema mismatch."""
        ...

    def latest_status(self, latest_edit: dict[str, Any] | None) -> Literal["pending", "approved", "denied", "expired"]:
        """Project current status by checking for a terminal m.replace edit."""
        if latest_edit is None:
            return "pending"
        return latest_edit.get("content", {}).get("m.new_content", {}).get("status", "pending")
```

**Live waiter** (private; not part of the public dataclass):

```python
@dataclass(slots=True)
class _LiveApprovalWaiter:
    approval_id: str
    card_event_id: str | None  # None until room_send returns
    room_id: str
    future: concurrent.futures.Future[ApprovalDecision]  # cross-loop bridge per ISSUE-177
    cancel_at_unbind: bool = False  # set by request_approval finally clause on cancel
```

`ApprovalManager` holds `self._pending_by_card_event: dict[str, _LiveApprovalWaiter]` (single map per D5).

---

## §2. The new core API (5 public methods)

```python
class ApprovalManager:
    async def request_approval(
        self,
        *,
        room_id: str,
        requester_id: str,
        approver_user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> ApprovalDecision:
        """Send approval card, await resolution from Matrix, return decision.

        Lifecycle:
        1. Build approval_id, build card content (incl. NEW approver_user_id field per D4).
        2. Send card via _send_card_on_runtime_loop. If transport fails → return ExpiredDecision.
        3. After send returns event_id: register _LiveApprovalWaiter in _pending_by_card_event.
        4. Write card event through to event_cache via store_event (no hand-rolled origin_server_ts per B8).
        5. Await waiter.future with timeout.
        6. On CancelledError (B1): emit m.replace status=expired, reason=cancelled, then re-raise.
        """

    async def resolve_approval(
        self,
        *,
        card_event_id: str,
        room_id: str,
        status: Literal["approved", "denied"],
        reason: str | None = None,
        resolved_by: str | None = None,
    ) -> AnchoredApprovalActionResult:
        """Emit m.replace edit on the card. Complete the live waiter (if any) AFTER the edit succeeds.

        Per CRITIQUE-A §5: for approvals, the gated tool MUST NOT execute until Matrix has the
        terminal edit. If the edit send fails, fail closed (resolve waiter as denied locally).
        """

    async def get_pending_approval(
        self,
        room_id: str,
        approval_id: str,
    ) -> PendingApproval | None:
        """Look up an approval by ID. Returns None if not found, resolved, or expired.

        Path:
        1. Try `event_cache.get_event(room_id, card_event_id)` if we already know card_event_id.
        2. If not in cache: `room_get_event(room_id, card_event_id)` (CRITIQUE-A §1 — direct event lookup).
        3. If we don't know card_event_id (e.g. user typed approval_id at CLI): bounded room_messages
           scan filtered by event type, last 24h.
        4. Once card found: `event_cache.get_latest_edit(room_id, card_event_id)` to check resolution.
        5. If terminal edit exists → return None (only PENDING approvals returned per CRITIQUE-A §4).
        """

    async def auto_deny_pending_on_startup(
        self,
        *,
        lookback_hours: int = 24,
    ) -> int:
        """Scan recent room history for unresolved approval cards and auto-deny.

        Called from bot._handle_bot_ready (per B6 — after first /sync warms cache).
        Iterates configured approval rooms only (per CRITIQUE-A §6 — no ad-hoc invited rooms).
        Skips cards whose sender != self transport sender (per D6 — Matrix m.replace would reject).
        Routes through get_pending_approval per card (per B3 — single filter location).
        Emits m.replace with EXACT text: "Bot restarted before approval — original request was cancelled."
        (per CRITIQUE-A §7 — exact issue-brief reason).
        Idempotent: rerun finds zero unresolved cards.
        Returns: count denied.
        """

    async def handle_response_event(
        self,
        *,
        room_id: str,
        sender_id: str,
        card_event_id: str,
        status: Literal["approved", "denied"],
        reason: str | None,
    ) -> bool:
        """Receive a typed approval response from bot.py event dispatch.

        Per CRITIQUE-A §4: the manager takes a TYPED command, not raw nio events.
        bot.py parses io.mindroom.tool_approval_response, m.reaction (D3), and reply-text (D3)
        into this canonical shape before dispatching.

        Verifies sender_id == card.approver_user_id. If not (per B7): return False.
        bot.py logs and the next /sync delivers unchanged card to Cinny.
        If sender matches: call resolve_approval and return True.
        """
```

---

## §3. Cache layer (no new tables, no new SQLite)

### D1 — Mandatory `_edit_cache_row` patch

In `src/mindroom/matrix/cache/event_cache_events.py`:

```python
# BEFORE (current):
def _edit_cache_row(event: dict[str, Any]) -> tuple[str, str, str] | None:
    if event.get("type") != "m.room.message":
        return None
    ...

# AFTER (this PR):
_EDITABLE_EVENT_TYPES = frozenset({"m.room.message", "io.mindroom.tool_approval"})

def _edit_cache_row(event: dict[str, Any]) -> tuple[str, str, str] | None:
    if event.get("type") not in _EDITABLE_EVENT_TYPES:
        return None
    ...
```

This is THE single most important line of this PR. Without it `get_latest_edit` is blind to approval edits and the entire design fails.

### D7 — New cache helper

In `src/mindroom/matrix/cache/event_cache.py`:

```python
async def get_recent_room_events(
    self,
    room_id: str,
    *,
    event_type: str,
    since_ts_ms: int,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return recent cached room events of `event_type` since `since_ts_ms`, newest first.

    Uses the existing `idx_events_room_origin_ts` index. No new schema.
    Used by ApprovalManager.auto_deny_pending_on_startup for the warm-cache path.
    """
```

~25 LOC including SQL prepared statement + simple test.

### Cache miss path (synthesis of D2 + CRITIQUE-A §1)

When `get_pending_approval(room_id, approval_id)` is called and `card_event_id` is unknown:

1. Walk `_pending_by_card_event` for any waiter with matching `approval_id` (in-memory hint).
2. If still unknown: bounded `room_messages` scan filtered by `{"types": ["io.mindroom.tool_approval"]}`, last 24h, look for `content.approval_id == approval_id`.
3. Once `card_event_id` discovered, follow the standard cache-then-room_get_event path.

Cards too old to be in lookback are NOT findable by approval_id — accepted UX wart, documented.

---

## §4. Restart auto-deny

### Hook point (B6)

In `bot.py`, extend `_handle_bot_ready` (fires after first /sync completes):

```python
async def _handle_bot_ready(self) -> None:
    ...existing cache warmup...
    denied = await self._approval_manager.auto_deny_pending_on_startup(lookback_hours=24)
    if denied > 0:
        log.info("approval.auto_deny.startup", denied_count=denied)
```

### Algorithm

```python
async def auto_deny_pending_on_startup(self, *, lookback_hours: int = 24) -> int:
    cutoff_ts_ms = int((time.time() - lookback_hours * 3600) * 1000)
    transport_sender = self._transport_sender_id()
    denied = 0
    for room_id in self._configured_approval_room_ids():
        # Warm path: cache lookup
        cached = await self._event_cache.get_recent_room_events(
            room_id, event_type="io.mindroom.tool_approval", since_ts_ms=cutoff_ts_ms,
        )
        # Build candidate set: original cards (no m.relates_to) only
        candidates = [e for e in cached if "m.relates_to" not in e.get("content", {})]
        # Cold path fallback if cache empty for this type+room
        if not candidates:
            candidates = await self._scan_room_messages_for_cards(room_id, since_ts_ms=cutoff_ts_ms)
        for card_event in candidates:
            try:
                pending = PendingApproval.from_card_event(card_event, room_id=room_id)
            except ValueError:
                continue  # malformed card; skip
            # D6: skip cards from other routers
            if pending.card_sender_id != transport_sender:
                continue
            # B3: route through get_pending_approval to apply the same resolution filter
            still_pending = await self.get_pending_approval(room_id, pending.approval_id)
            if still_pending is None:
                continue  # already resolved (idempotency)
            await self.resolve_approval(
                card_event_id=pending.card_event_id,
                room_id=room_id,
                status="denied",
                reason="Bot restarted before approval — original request was cancelled.",
                resolved_by=transport_sender,
            )
            denied += 1
    return denied
```

### Properties

- **Idempotent**: rerun finds zero unresolved cards (because resolve_approval emits an `m.replace` that the next get_pending_approval sees via the patched `_edit_cache_row`)
- **Same-sender safe**: doesn't try cross-sender edits Matrix would reject
- **Bounded**: 24h lookback default, configurable
- **No persistence dependencies**: all state from Matrix room history + event cache

---

## §5. One-time legacy persistence cleanup

In `ApprovalManager.__init__` (or `initialize_approval_store`):

```python
def _purge_legacy_approval_files(storage_root: Path) -> int:
    legacy_dir = storage_root / "approvals"
    if not legacy_dir.exists():
        return 0
    purged = 0
    for json_file in legacy_dir.glob("*.json"):
        try:
            json_file.unlink()
            purged += 1
        except OSError as exc:
            log.warning("approval.legacy_purge.failed", path=str(json_file), error=str(exc))
    try:
        legacy_dir.rmdir()  # only succeeds if empty
    except OSError:
        pass  # leftover non-json files; leave alone
    if purged:
        log.info("approval.legacy_purge", purged_count=purged)
    return purged
```

**Phrasing per CRITIQUE-A §6 ("one-time wording is misleading"):** runs every startup; normally deletes files only the first time after deploy.

---

## §6. Wire contract changes (single addition)

**Card content (`io.mindroom.tool_approval`):**
- ADD field: `approver_user_id: str` (per D4)
- All other fields unchanged

**Response event (`io.mindroom.tool_approval_response`):**
- No change

**Edit event (`m.replace` on card):**
- No change

**Cinny client:**
- Zero changes required. Unknown fields (`approver_user_id`) are silently ignored by current Cinny code.

---

## §7. Implementation order (binding)

Per CRITIQUE-A §"Suggested implementation order" (W7) AND claude's PR-ladder concession in CRITIQUE-B:

**Single PR is acceptable** for ISSUE-194 because the deletion-heavy nature means partial PRs would leave the codebase in an inconsistent half-deleted state. But within the single PR, commit in this order for reviewability:

1. **Commit 1**: `feat(matrix): index io.mindroom.tool_approval edits in event cache (ISSUE-194)` — the `_edit_cache_row` patch (D1) + its unit test. Standalone correctness fix.
2. **Commit 2**: `feat(matrix): add get_recent_room_events cache helper (ISSUE-194)` — D7 helper + unit test.
3. **Commit 3**: `feat(approval): add approver_user_id to card content (ISSUE-194)` — D4 schema delta on the existing PR-568 code. This commit alone PASSES tests with the old machinery still present.
4. **Commit 4**: `refactor(approval): replace local persistence with Matrix-as-source-of-truth (ISSUE-194)` — the BIG one. Deletes ~1500 LOC, adds ~300 LOC, swaps machinery for the 5 new methods.
5. **Commit 5**: `refactor(approval): clean up startup ordering and orchestrator drain machinery (ISSUE-194)` — bot.py + orchestrator.py simplifications, `_handle_bot_ready` hook for auto_deny.
6. **Commit 6**: `test(approval): rewrite test_tool_approval.py for Matrix-as-source-of-truth (ISSUE-194)` — delete 3500 LOC of reconciliation tests, add ~10 new tests for the new API.

Each commit MUST pass `nix-shell --run 'uv run pytest tests/test_tool_approval.py tests/test_tool_hooks.py tests/test_multi_agent_bot.py tests/test_openai_compat.py tests/test_room_invites.py -x -n 0 --no-cov -v'` and pre-commit before the next commit lands.

---

## §8. Risk register

| Risk | Mitigation |
|---|---|
| Lookback window too short → stale "pending" UI past 24h | Configurable; document as known UX wart; file future ticket if it becomes real |
| Two routers in same room → other router's cards visible but un-deny-able | D6 same-sender skip in auto-deny; correctness preserved by Matrix's `m.replace` rule |
| Cache disabled (`event_cache.disable(reason)`) | Both `get_pending_approval` AND `auto_deny_pending_on_startup` fall back through `room_get_event` and bounded `room_messages` scan |
| Restart between `room_send` and `_pending_by_card_event` registration | The bot crashed before binding the waiter, so the original tool call already died (CancelledError propagated). On restart auto-deny finds the orphan card and denies it. THIS IS THE WHOLE POINT of the design. |
| `m.replace` send fails during approval click | Fail closed (per CRITIQUE-A §5): resolve waiter as denied locally; gated tool does NOT execute. Log Matrix transport failure. |
| Wrong-clicker UX (B7) | `handle_response_event` returns `False`; next /sync delivers unchanged card to Cinny; brief stale optimistic tick acceptable |
| Authorization drift (PR #568 had recovery for this) | Documented limitation: drift causes silent card-stays-pending; bounded by lookback. CRITIQUE-A B5 + claude's risk register agree. |
| Rollback after this PR ships | Old PR #568 code cannot recover Matrix-only cards (no JSON files exist). Documented: pending approvals issued under ISSUE-194 are abandoned and should be auto-denied or manually ignored before rollback. |

---

## §9. Acceptance criteria (binding)

1. `tool_approval.py` reduced from 1888 to ≤400 LOC.
2. Zero local persistence of approval state (no JSON files written or read).
3. `_edit_cache_row` accepts `io.mindroom.tool_approval` event type.
4. New `event_cache.get_recent_room_events(room_id, *, event_type, since_ts_ms, limit)` helper exists with unit test.
5. `ApprovalManager` exposes exactly 5 public methods per §2.
6. `PendingApproval` is a frozen dataclass derived from card content; carries no `Future` and no full `arguments` dict.
7. Card content includes `approver_user_id` field.
8. `_handle_bot_ready` calls `auto_deny_pending_on_startup(lookback_hours=24)` after first /sync.
9. Auto-deny emits exact reason text: `"Bot restarted before approval — original request was cancelled."`
10. ISSUE-177 cross-loop `concurrent.futures.Future` bridge for sync-tool approval gating is preserved (no regression on `test_sync_tool_approval_resumes_after_cross_loop_resolution`).
11. Cinny wire contract preserved: response events, reactions, and reply-text resolution paths all work end-to-end.
12. `_send_approval_notice` truncated-arg UX preserved.
13. All R12-R26 marathon-era reconciliation/retry/drain machinery is GONE.
14. Net diff ~-4700 LOC across source + tests (allowable range: -4000 to -5500).
15. Live test in lab Cinny (DevAgent verifies):
    - Tool call → card appears → approve → tool runs → card edits to "approved"
    - Tool call → card appears → restart bot → card auto-denies with the exact reason text
    - Tool call → card appears → cancel mid-send → no orphan card OR card cleanly resolved
    - Tool call → card appears → second user (non-approver) clicks → silently no-ops, original approver can still resolve

---

## §10. Out of scope

- Audit log persistence (file ticket if needed; Matrix room history is the only durable record)
- Migration of existing on-disk approval JSON files (one-time idempotent purge per §5; production has no real data to preserve since PR #568 just shipped)
- Cinny client changes
- Extending `notify_outbound_event` for non-thread-affecting custom event types (deferred per "Open question" above)
- Refactoring `evaluate_tool_approval` rule engine into a separate module
- Performance profiling beyond ensuring `get_recent_room_events` uses the existing index

---

## End

Synthesis author: DevAgent (Claude Opus 4.7) drawing on convergent critiques from Codex (gpt-5.5 xhigh) and Claude (Opus 4.7) planners. All decisions reference critique findings by ID; nothing improvised.
