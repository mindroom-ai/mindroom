# Cross-Room Thread Forks — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task using `baspowers:subagent-driven-development`. Steps use checkbox (`- [ ]`) syntax.

**Execution:** `baspowers:subagent-driven-development` — independent backend & client worktrees run in parallel on `gemini4_neon@xhigh` against a shared protocol spec, followed by Opus + Astra dual review.

**Goal:** Support cross-room `io.mindroom.fork: { room_id?, thread_id, event_id }` in the MindRoom backend with fail-closed access control, destination-local model selection and routing, true-room attachment provenance, provisional titles, and prompt context.

**Architecture:** Extend `matrix/thread_forks.py` with room-qualified references and `(room_id, thread_id)` chain walks; add one shared fail-closed cross-room authorization gate (`matrix/thread_fork_access.py`) reused by `ConversationResolver`, `matrix_message(action="read")`, and `thread_summary/automatic.py`; stamp `room_id` on cross-room `ResolvedVisibleMessage` rows so attachments, routing, display names, and enrichment stay cleanly separated by room.

**Tech Stack:** Python 3.12+, `matrix-nio`, `pytest`, `ruff`, `mypy`, `tach`.

**Spec:** `docs/baspowers/specs/2026-11-23-cross-room-thread-forks-design.md`

## Global Constraints

- Wire format: `io.mindroom.fork: { "room_id"?: "!...", "thread_id": "$...", "event_id": "$..." }`. Omit `room_id` for same-room; reject entire reference if `room_id` is present and invalid (`None`, empty, whitespace, not starting with `!`).
- Never degrade (`is_full_history=False` / `degraded=True`) a child thread when a cross-room hop is denied by authorization or missing agent membership; only `DEFERRED` (incomplete cache in `NONBLOCKING` mode) triggers `STRICT` fallback.
- Never overwrite on-disk attachment records' `room_id` with the destination room ID.
- Keep `THREAD_FORK_CONTEXT_TEMPLATE` and existing same-room fork behavior intact.
- Soft size target: ~350–500 prod lines across touched/new modules, ~450–600 test lines (~1:1–1.2:1 test:prod).

## Review Focus

1. Transitive chain `A -> B -> C` where `B -> A` omits `room_id` (same-room in A) or `C -> B` crosses rooms: omitted `room_id` must resolve relative to its owning hop room, and encryption + audience checks must validate every ancestor against final output room `C`.
2. Unauthorized cross-room fork (outsider in target room, unjoined requester, unjoined fork author, encrypted source -> plaintext target, contact room) must yield zero parent messages, zero copied parent title, zero parent attachments, and still allow normal replies in the child room (no degradation).
3. Strict-refresh denial or failure in `reply_turn/locked_turn.py` must never fall back to a previously expanded snapshot.
4. Multi-human parent forked into a 1-human target room, or source-only agent in parent forked into a room with a different sole agent / mentions-only mode, must route cleanly using target-room participation.
5. Parent-room attachment viewed via `get_attachment(..., view=True)` and inline `[attachments: ...]` annotations must work for authorized cross-room forks while preserving the original `room_id` on disk.

---

### Task 1: Protocol parsing, `ResolvedVisibleMessage.room_id`, and room-aware `with_fork_prefix`

**Files:**
- Modify: `src/mindroom/matrix/client_visible_messages.py` (add `room_id: str | None = None` to `ResolvedVisibleMessage`)
- Modify: `src/mindroom/matrix/thread_forks.py`
- Test: `tests/test_thread_forks.py`

**Interfaces:**
- Produces in `mindroom.matrix.thread_forks`:
  - `ThreadForkReference(room_id: str | None, thread_id: str, event_id: str)`
  - `CrossRoomForkDecision(StrEnum)`: `ALLOWED`, `DENIED`, `DEFERRED`
  - `CrossRoomForkHop(target_room_id: str, child_room_id: str, parent_room_id: str, fork_root: ResolvedVisibleMessage, fork_point_event_id: str)`
  - `fork_reference(history, thread_id) -> ThreadForkReference | None` (or keep `fork_reference` returning `tuple[str, str] | None` and add `fork_target_reference(history, thread_id, *, default_room_id: str) -> ThreadForkReference | None` — choose cleanest API and update all callers!)
  - `fork_parent_scopes(history, thread_id, *, room_id: str) -> frozenset[tuple[str, str]]`
  - `with_fork_prefix(history, *, room_id, thread_id, read_thread: Callable[[str, str], Awaitable[ThreadHistoryResult]], authorize_cross_room: Callable[[CrossRoomForkHop], Awaitable[CrossRoomForkDecision]] | None = None, logger)`

- [ ] **Step 1:** Write failing tests in `tests/test_thread_forks.py` for valid/invalid `room_id` parsing (including serverless `!room` IDs, `None`, `""`, `#alias`, whitespace), cross-room `A -> B -> C` chain with an omitted-room hop in `A`, `(room_id, thread_id)` cycle detection, `ALLOWED`/`DENIED` (non-degrading)/`DEFERRED` (degrading) decisions, `room_id` stamping on prefix messages, and `fork_parent_scopes`.
- [ ] **Step 2:** Implement `ResolvedVisibleMessage.room_id` and `thread_forks.py` updates.
- [ ] **Step 3:** Run `pytest tests/test_thread_forks.py -x -n 0 --no-cov` and verify PASS. Commit.

### Task 2: Shared fail-closed cross-room authorization gate

**Files:**
- Create: `src/mindroom/matrix/thread_fork_access.py`
- Modify: `tach.toml` (declare `mindroom.matrix.thread_fork_access` dependencies)
- Test: `tests/test_thread_forks.py` (or `tests/test_thread_fork_access.py`)

**Interfaces:**
- Produces:
  - `authorize_cross_room_fork_hop(*, client, config, runtime_paths, hop: CrossRoomForkHop, requester_id: str | None, nonblocking: bool, target_room: nio.MatrixRoom | None = None) -> CrossRoomForkDecision`

- [ ] **Step 1:** Write failing tests covering every gate condition:
  - Authorized 1-human same-encryption / unencrypted->encrypted / shared-humans subset -> `ALLOWED`
  - Contact room as source or target -> `DENIED`
  - Agent not joined to parent room -> `DENIED`
  - Missing or non-human `requester_id`, or requester not joined to parent room -> `DENIED`
  - Fork root sender not joined to parent room (and untrusted `ORIGINAL_SENDER_KEY` ignored, trusted managed relay honored) -> `DENIED`
  - Target room has another joined or invited human not in parent room, or `world_readable` history visibility -> `DENIED`
  - Encrypted (or unknown) parent -> unencrypted (or unknown) target -> `DENIED`
  - Incomplete cached membership/encryption in `nonblocking=True` -> `DEFERRED`
- [ ] **Step 2:** Implement `thread_fork_access.py` reusing `cached_room`, `get_room_members`, `ensure_room_membership_synced`, `room_membership_is_complete`, `cached_joined_member_ids`, `resolve_room_encryption_outcome`, `is_contact_room`, `is_human_requester_id`, `equivalent_requester_ids`, and `entity_identity_registry`.
- [ ] **Step 3:** Run pytest and `tach check --dependencies --interfaces`. Commit.

### Task 3: Wire authorization, routing, and model non-inheritance into `ConversationResolver`, `turns/dispatch.py`, `reply_turn/locked_turn.py`, and `turn_policy.py`

**Files:**
- Modify: `src/mindroom/conversation_resolver.py`
- Modify: `src/mindroom/turns/dispatch.py`
- Modify: `src/mindroom/turns/ingress.py`
- Modify: `src/mindroom/edit_regenerator.py`
- Modify: `src/mindroom/turns/interactive_selection.py`
- Modify: `src/mindroom/reply_turn/locked_turn.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/thread_utils.py`
- Modify: `src/mindroom/turn_policy.py`
- Test: `tests/test_conversation_resolver.py`, `tests/test_thread_models.py`, `tests/test_turn_dispatch_pipeline.py`

- [ ] **Step 1:** Write failing tests for:
  - Cross-room fork history loaded when authorized, omitted without degrading when denied, `DEFERRED` -> `STRICT` fallback.
  - Cross-room fork skips parent thread model inheritance (both `_apply_root_thread_model` and `_inherit_fork_thread_models`) while keeping explicit opening pick in target room and same-room inheritance.
  - Multi-human parent forked into 1-human target room still routes to sole agent / router; source-room-only agent in parent does not bypass `mentions` mode in target room.
  - `locked_turn.py` does not fall back to an earlier expanded snapshot when strict refresh denies/fails cross-room prefix.
- [ ] **Step 2:** Implement all wiring and routing fixes cleanly.
- [ ] **Step 3:** Run targeted pytest files and verify PASS. Commit.

### Task 4: Cross-room attachments, provisional titles, prompt context, display names, `matrix_message read`, and docs

**Files:**
- Modify: `src/mindroom/attachments/storage.py`
- Modify: `src/mindroom/attachments/media.py`
- Modify: `src/mindroom/inbound_turn_normalizer.py`
- Modify: `src/mindroom/execution_preparation.py`
- Modify: `src/mindroom/custom_tools/attachments.py`
- Modify: `src/mindroom/thread_summary/automatic.py`
- Modify: `src/mindroom/prompts.py`
- Modify: `src/mindroom/reply_turn/enrichment.py`
- Modify: `src/mindroom/response_runner.py`
- Modify: `src/mindroom/custom_tools/matrix_conversation_operations.py`
- Modify: `docs/configuration/threads.md`, `docs/attachments.md`
- Test: `tests/test_bot_media_dispatch.py`, `tests/test_attachments_tool.py`, `tests/test_thread_summary.py`, `tests/test_response_runner_agent.py`, `tests/test_matrix_message_tool.py`, `tests/test_config_prompts.py`

- [ ] **Step 1:** Write failing tests for:
  - Cross-room parent attachment registration preserves `(parent_room_id, parent_thread_id)` on disk (never overwrites `room_id`), passes `filter_attachments_for_context`, renders inline `[attachments: ...]`, and works with `get_attachment(..., view=True)`.
  - Cross-room provisional title copies parent summary from `parent_room_id` when authorized, skips & falls back to prompt title when denied (including coalesced first turn).
  - `CROSS_ROOM_THREAD_FORK_CONTEXT_TEMPLATE` renders with `parent_room_id`, `parent_room_label`, `parent_thread_id`, `fork_point_event_id`; omitted when prefix was denied/absent; existing `THREAD_FORK_CONTEXT_TEMPLATE` unchanged.
  - `execution_preparation.py` merges fallback `member_display_names` from authorized parent rooms.
  - `matrix_message(action="read")` enforces cross-room auth gate, tags cross-room prefix rows with `room_id`, and omits `edit_options` for cross-room rows.
- [ ] **Step 2:** Implement changes and update docs (`docs/configuration/threads.md`, `docs/attachments.md`).
- [ ] **Step 3:** Run all touched test files (including once with default addopts), `ruff check`, `ruff format --check` on changed files, `mypy`, and `tach check --dependencies --interfaces`. Commit.
