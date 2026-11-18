# Inline compaction marker: backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development (two coupled tasks across streaming and history; a missed edit path ships a stuck "running" marker, so each task gets its own review).

**Goal:** Record each automatic compaction on the agent's reply event (`io.mindroom.compactions`) at the spot it happened, instead of posting a separate notice.

**Architecture:** A per-reply recorder (`ReplyCompactions`) is written by a new `CompactionLifecycle` implementation and read by `StreamingResponse` on every durable write and live frame, with a refresh nudge on change, the same side channel worker warm-up progress uses. Manual compactions keep the old notice.

**Tech Stack:** Python 3.13, asyncio, pytest (`pytest_asyncio` fixtures), nix-shell + uv.

**Spec:** `docs/baspowers/specs/2026-11-17-inline-compaction-marker-design.md` (event contract), wiring evidence in `docs/baspowers/specs/2026-11-17-inline-compaction-backend-wiring.md` (section "Recommended wiring", T1-T9, with file:line refs on `origin/main` 125b6efa2).

## Global Constraints

- Content key `io.mindroom.compactions`, constant `COMPACTIONS_CONTENT_KEY` in `src/mindroom/constants.py` next to `COMPACTION_NOTICE_CONTENT_KEY`.
- Entry keys exactly as the spec's contract: `id, phase, mode, after_tool, status, started_at, duration_ms?, summary_model, before_tokens, after_tokens?, history_budget_tokens?, runs_before, compacted_run_count?, runs_remaining?, failure_reason?`. Optional keys omitted, never `null`. `runs_remaining` only while `status == "running"`.
- `after_tool` = `len(tool_trace) if show_tool_calls else 0`, read once at start (same rule as `StreamingResponse.add_reasoning`).
- `mode == "manual"` still goes through `MatrixCompactionLifecycle` (separate notice, unchanged).
- `COMPACTION_NOTICE_CONTENT_KEY` stays in `_NON_TURN_NOTICE_CONTENT_KEYS`; the plural key must not make a reply look like a notice.
- No change to what the model sees (prompt cache must stay byte-identical).
- Tests: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest <files> -x -n 0 --no-cov'`; run each new test file once with the repo's default addopts. Use `@pytest_asyncio.fixture` for async fixtures.

## Review Focus

- ~~A compaction that starts while only the pending placeholder exists (pre-turn) must produce a durable edit of the placeholder carrying the entry, even with no text yet, and with live frames on (checkpoint) and off.~~ Superseded by 6f751569e: a compaction never triggers, gates or requires a durable write; live frames show it at once, and the entries ride on the next write the stream makes anyway.
- A compaction that fails or is cancelled must leave its entry in a terminal status (`failed`/`timeout`), never `running`, on the final event.
- `show_tool_calls=False` anchors at 0; resumed tool trace (`resumed_tool_trace`) counts toward `after_tool`.
- Non-streamed (collected) delivery puts the final entries on the single final message.
- Split replies: key on the first segment only.

---

### Task 1: Recorder, lifecycle, phase, and runner wiring

**Files:**
- Modify: `src/mindroom/constants.py` (new constant)
- Modify: `src/mindroom/history/types.py` (`CompactionPhase`, `phase` on `CompactionLifecycleStart`, `start` docstring)
- Modify: `src/mindroom/history/runtime.py` (`_run_scope_compaction_with_lifecycle(..., phase)`, call sites pass `"pre_turn"` / `"mid_turn"`)
- Create: `src/mindroom/history/reply_compactions.py`
- Modify: `src/mindroom/response_runner.py` (`_build_compaction_lifecycle`, `_deliver_turn`, `_deliver_collected`, `final_extra_content`, widened type hints)
- Modify: `src/mindroom/delivery_gateway.py` (`StreamingDeliveryRequest.compactions`, pass-through to `send_streaming_response`; `send_streaming_response` gets the parameter as a no-op until Task 2)
- Test: `tests/test_reply_compactions.py` (new), `tests/test_response_runner_agent.py`, `tests/test_mid_turn_compaction.py`, `tests/test_history_prepare_lifecycle.py`

**Interfaces:**
- Produces:
  - `CompactionPhase = Literal["pre_turn", "mid_turn"]`
  - `class ReplyCompactions` with `bind(*, tool_count: Callable[[], int], changed: Callable[[], None]) -> None`, `unbind() -> None`, `start(event: CompactionLifecycleStart) -> str`, `update(entry_id: str, **fields: object) -> None`, `content() -> list[dict[str, object]] | None` (deep copy; `None` when empty), `version: int` (bumped on every change).
  - `class ReplyCompactionLifecycle(recorder: ReplyCompactions, notice: MatrixCompactionLifecycle)` implementing `CompactionLifecycle`.
  - `StreamingDeliveryRequest.compactions: ReplyCompactions | None = None`; `send_streaming_response(..., compactions: ReplyCompactions | None = None)`.

- [ ] **Step 1: Write failing tests** in `tests/test_reply_compactions.py`:
  - `test_start_progress_success_updates_one_entry_in_place`: start (phase mid_turn, before 366804, budget 351792, runs_before 24) → progress (after 9000, compacted 9, remaining 15, duration 4000) → success (after 6336, compacted 24, duration 8400); `content()` has exactly one entry with `status == "success"`, `after_tokens == 6336`, no `runs_remaining`, and the id returned by `start`.
  - `test_failure_sets_status_and_reason`: timeout failure → `status == "timeout"`, `failure_reason` set, no `runs_remaining`.
  - `test_manual_goes_to_notice_and_routes_back`: with a recording double for `MatrixCompactionLifecycle`, a `mode="manual"` start/success both reach the double and `content()` stays `None`.
  - `test_after_tool_read_once_at_start`: bound count returns 2 at start, 5 afterwards → entry keeps `after_tool == 2`.
  - `test_unbound_recorder_anchors_at_zero` and `test_changed_called_on_every_change` (start, progress, success → 3 calls) and `test_content_is_a_copy`.
  - In `tests/test_mid_turn_compaction.py`: recorded starts carry `phase == "mid_turn"`; in `tests/test_history_prepare_lifecycle.py`: one assertion that the pre-turn start has `phase == "pre_turn"`.
  - In `tests/test_response_runner_agent.py`: switch `test_generate_response_keeps_first_turn_follow_up_effects_in_new_thread` to `mode="manual"` (keeps the notice-routing assertion), and add `test_auto_compaction_records_on_reply_without_notice`: `_send_compaction_lifecycle_start` is not awaited and the final `$response` content has `io.mindroom.compactions[0]` with `status "success"`, `phase "pre_turn"`, `after_tool 0`.
- [ ] **Step 2: Run them, expect failures** (import error / missing key).
- [ ] **Step 3: Implement** per wiring doc T1-T5. `start` sets `started_at = time.time_ns() // 1_000_000`, `status = "running"`, `id = uuid4().hex`, `mode = event.mode`, `phase = event.phase`. The lifecycle remembers notice-routed ids in a `set[str]`. In `_deliver_collected`, bind the recorder to the collected presentation's tool count and a no-op `changed`.
- [ ] **Step 4: Run the touched test files** plus `tests/test_response_delivery_gateway.py`, `tests/test_partial_reply_context.py`; all pass.
- [ ] **Step 5: Commit** (`git commit --trailer "Assisted-by: ..."`).

### Task 2: StreamingResponse, live frames, segmented replies

**Files:**
- Modify: `src/mindroom/streaming.py` (`StreamingResponse.compactions` field and version tracking, `_delivery_extra_content`, `_delivery_snapshot`, `_StreamingDeliverySnapshot`/`_CommittedDeliveryState`, `_mark_delivery_committed`, `has_uncommitted_presentation`, live `_throttled_send` empty-progress rule, `live_state`, `send_streaming_response` bind/unbind)
- Modify: `src/mindroom/live_frames/frames.py` (`LiveDocs.compactions`, `to_json`, `live_delta` whole-list replace)
- Modify: `src/mindroom/matrix/segmented_messages.py` (`_continuation_content` drops the key)
- Test: `tests/live_frames/test_live_streaming.py`, `tests/test_streaming_edits.py` (or `test_streaming_finalize.py`), `tests/test_segmented_messages.py`, `tests/test_partial_reply_context.py`, `tests/live_frames/` frame unit tests

**Interfaces:**
- Consumes: Task 1's `ReplyCompactions` (`bind`, `unbind`, `content`, `version`) and the `compactions` parameter of `send_streaming_response`.

- [ ] **Step 1: Write failing tests:**
  - `test_compaction_after_two_visible_tools_anchors_after_tool_2` and `test_compaction_with_hidden_tool_calls_anchors_at_zero` (live streaming harness next to the reasoning-segment tests).
  - `test_live_frames_carry_compactions_in_snapshot_and_delta` (frame unit: snapshot has `compactions`; a changed list yields a delta with the whole list; unchanged yields no key).
  - `test_compaction_start_on_placeholder_sends_progress_edit` (no text yet: an edit of the placeholder carries the key), with live frames off; and `test_compaction_change_schedules_checkpoint_with_live_frames` (live on: `has_uncommitted_presentation` true and a checkpoint edit carries the key).
  - `test_final_event_keeps_success_entry` and `test_failed_compaction_is_terminal_on_final_event`.
  - `test_split_reply_keeps_compactions_on_first_segment_only` (next to `test_split_reply_keeps_no_reasoning`).
  - `test_reply_with_compactions_is_not_skipped_from_unseen_context` (old singular notice still skipped).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** per wiring doc T6-T8. The `changed` callback queues `_queue_delivery_request(delivery_queue, force_refresh=True, allow_empty_progress=not streaming.accumulated_text.strip())`, like `_drain_worker_progress_events`. Add the key to every durable write only when `content()` is not `None`.
- [ ] **Step 4: Run** the touched files plus `tests/test_streaming*.py tests/live_frames tests/test_large_messages_integration.py tests/test_response_runner_agent.py tests/test_reply_compactions.py`; all pass. (The controller runs the full suite afterwards on another machine.)
- [ ] **Step 5: Commit.**
