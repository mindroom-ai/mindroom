# Inline compaction markers on the reply event: backend wiring

Read-only investigation of `origin/main` @ `125b6efa2`. All `file:line` refs are origin/main.

## TL;DR

- Pre-turn compaction runs **inside** the response stream generator. By then the `StreamingResponse`, its delivery task, and its live-frame task all exist, and normally so does the reply's placeholder event.
- Mid-turn compaction is an awaited `before_request` hook. It **cannot yield** into the run event stream.
- Simplest correct wiring is a side channel: one per-reply `ReplyCompactions` recorder. The new `ReplyCompactionLifecycle` writes to it. `StreamingResponse` reads it on every durable write and every live frame, and the recorder asks for a refresh when it changes. This copies the worker-warmup side channel that already exists (`_drain_worker_progress_events`, streaming.py:1745).
- **Correction to the brief:** manual compaction is not reply-less. `compact_context` only *queues* a forced compaction (`custom_tools/compact_context.py:34-50` → `request_compaction_before_next_reply`). It then runs **pre-turn in the next reply**, with `mode="manual"` (`history/types.py:151-153`). The placeholder is held back on purpose so the notice comes first (`response_runner.py:1238-1247`, `_has_queued_forced_compaction`). The recommendation below keeps the notice for `mode == "manual"` as you asked, by having the new lifecycle hand manual off to `MatrixCompactionLifecycle`.
- **Do not reuse the key `io.mindroom.compaction` on replies.** `_has_nothing_to_read` (execution_preparation.py:585-594) treats any message carrying that key as a non-turn notice and drops it from thread context. The new key `io.mindroom.compactions` is a different dict key, so it is safe.

---

## Q1. How do agent-loop events become `StreamInputChunk`s?

Chain for an agent turn, from the innermost producer out:

1. **`run_agent`.** `agent_loop/run.py`, wrapping `model_loop` (`agent_loop/model_loop.py:~190-248`). It yields `TextDelta | ReasoningDelta | ToolStarted | ToolProgress | ToolCompleted | LoopEnded` (`LoopEvent`, model_loop.py:163). Tool events come from `_run_tool_batch` (model_loop.py:314-436). `ToolProgress` is yielded from a per-call progress source inside the batch runner (model_loop.py:349-350, 411-412).
2. **`_start_agent_run`** (ai.py:519-591). It wraps `run_agent` in `drive_delegation_stream(...)` (ai.py:569-580). In minimal or `cli_shell` mode it also wraps it in `stream_cli_events(driven)` (ai.py:581-582, `agent_cli/events.py:58-84`). `stream_cli_events` uses `callback_event_stream` (`tool_system/context_bound_streams.py:70-95`). That runs the loop in a **producer task feeding an unbounded queue**, and merges in hidden CLI tool events that `emit_cli_run_event` pushes into the same sink.
3. **`_stream_agent_attempt_chunks`** (ai.py:1280-1315). It wraps `stream_with_llm_request_log_context`, then **`_process_stream_events`** (ai.py:1208-1277). That function filters and forwards `ReasoningDelta`, `ToolProgress`, `TextDelta`, `ToolStarted`, `ToolCompleted`, and `RunFinished`, and logs anything else as "Skipping stream event" (ai.py:1270).
4. **`stream_agent_response._run_attempt`** (ai.py:1348-1437). It first calls **`_prepare_agent_run_context`**, which is where pre-turn compaction happens. It then streams the attempt chunks and ends with an `AttemptResolved` sentinel.
5. **The `_agent_turn_adapter`** (ai.py:284, built at ai.py:1439-1448) is passed to **`stream_response_turn` → `_stream_response_turn`** (response_turn.py:1118-1243). That loops over attempts (`TURN_CONTINUATION_LIMIT`), wraps each in `context_bound_async_stream` (a pull-through, no queue; context_bound_streams.py:45-67), drops `AttemptResolved`, and yields the rest.
6. **`response_runner._deliver_turn`'s `response_stream()`** (response_runner.py:1938-1939) returns `turn.stream(...)`, which is the agent `stream` closure at response_runner.py:2343-2381 or the team one at :1753-1766. `_deliver_streamed` wraps it in `tool_runtime.stream_in_context` and passes it to `delivery_gateway.deliver_stream` (response_runner.py:2017-2045).
7. **`deliver_stream`** (delivery_gateway.py:~1960-2015) calls `send_streaming_response` (streaming.py:2151-2307). That function creates `StreamingResponse`, the delivery task (`_drive_stream_delivery`), the worker-progress task (`_drain_worker_progress_events`), and the live-frame task. The consumer is **`_consume_streaming_chunks`** (streaming.py:1623-1742), which dispatches on chunk type.

The non-streamed path is `_deliver_collected` (response_runner.py:2065-2125), which uses `collect_streamed_response_content` (response_turn.py:629-662).

Pull vs push matters for ordering:
- In **standard mode** the chain is pull-based. A hook runs only when the consumer asks for the next chunk, so every earlier chunk, including all `ToolStarted` events, has already been applied to `streaming.tool_trace`.
- In **minimal or cli_shell mode** a producer task sits behind a queue, so the consumer can lag behind the loop.

## Q2. Pre-turn: does the reply's stream or placeholder exist yet?

**Yes, both, for auto compaction.**

- The placeholder is sent at claim time, before preparation: `_send_placeholder` (response_runner.py:1229-1274, called at :1584-1596). It sends `AGENT_PLACEHOLDER`/`TEAM_PLACEHOLDER` with `{io.mindroom.stream_status: "pending"}` (:1255). It is skipped when:
  - the request is quiet or under participation (:1588-1590),
  - an event already exists,
  - or a **forced (manual) compaction is queued** (:1238-1247). This is deliberate, so the notice orders before the reply.
- Pre-turn compaction sits inside the stream generator. The path is `_run_attempt` → `_prepare_agent_run_context` (ai.py:1355-1363) → `_prepare_agent_and_prompt` → `prepare_agent_execution_context(..., compaction_lifecycle=ctx.compaction_lifecycle)` (ai.py:901-924) → `history/runtime.py prepare_scope_history` (:145-300) → `_run_scope_compaction_with_lifecycle` (:316-400). The response stream is a factory that `send_streaming_response` pulls (response_runner.py:2022-2025), so this all runs **during** `_consume_streaming_chunks`. `StreamingResponse` exists, `event_id` is set to the adopted placeholder (streaming.py:2216-2221), and the delivery and live-frame tasks are running. An emitted chunk would reach `StreamingResponse`, but preparation is a plain `await` with no generator to yield from, so the side channel is what works here.
- **What the user sees today:**
  - the placeholder ("Thinking...", `stream_status: pending`), live frames permitting, and the typing indicator;
  - plus a **separate `m.notice`** "Compacting history..." posted by `MatrixCompactionLifecycle.start` → `_send_compaction_lifecycle_start` (delivery_gateway.py:439-475, 1813-1852). It is threaded with `reply_to_event_id` set to the placeholder (`_build_compaction_lifecycle`, response_runner.py:858-873).
  - The notice is then edited by progress, success, or failure (delivery_gateway.py:1854-1946).
- **Pre-turn is not always at `after_tool=0`.** `_stream_response_turn` re-runs `adapter.run_attempt` for each continuation (response_turn.py:1151-1197), and each attempt re-prepares, so it can compact again **after** earlier attempts' tools. A restart replay also starts with `resumed_tool_trace` already in `tool_trace` (streaming.py:646-654). So `after_tool` must be **read from the live tool count**, never hard-coded to 0. It comes out as 0 for the first attempt of a fresh reply.

## Q3. Mid-turn: can the hook emit into the run event stream?

**No, not without changing the loop core.**

- `before_request` hooks are `Callable[[LoopState], Awaitable[RequestDecision]]` (model_loop.py:106, 116-150). They are awaited by `_before_request` inside `while` in the loop generator (model_loop.py:220-222). Only the model turn and the tool batch yield events (model_loop.py:224-247).
- `mid_turn_compaction_hook` (history/mid_turn_compaction.py:139-170) reaches `compact_scope_mid_turn(..., compaction_lifecycle=compaction.lifecycle)` (mid_turn_compaction.py:351-369 → runtime.py:498-570).

In-band precedents all come from **inside the batch** or the CLI sink, not from hooks:
- `ToolProgress` comes from the running call's progress source, yielded by the batch runner (model_loop.py:349, 411).
- Hidden CLI tool events go through the `_SINK` contextvar into `stream_cli_events`' queue (agent_cli/events.py:29-56). This only exists in minimal/cli_shell mode.
- Queued-message and job notices (`tool_jobs/notices.py`, `ai_runtime._notice_at_batch`) are `after_tool_batch` hooks that rewrite messages and post Matrix side effects. They are not stream events.
- Skill-review capture is `on_loop_end` and writes state only.

The side-channel precedent is exact: worker warm-up progress. `worker_progress_pump_scope` (`tool_system/runtime_context.py:323, 451, 724`) is set in `send_streaming_response` (streaming.py:2227-2234). Tools push into it, `_drain_worker_progress_events` (streaming.py:1745-1773) mutates `streaming._warmup_state`, and it nudges with `_queue_delivery_request(force_refresh=True, allow_empty_progress=...)`. `tool_trace_collector` (streaming.py:2168, 2303) is the shared-mutable-object precedent across the gateway boundary.

**Verdict:** use a side channel (recorder plus nudge). It is far smaller than making `before_request` able to yield, which would need a concurrent event drain in the loop and a second mechanism for pre-turn preparation.

On whether `after_tool` read at start time is exact:
- **Standard mode: yes.** The chain is pull-based (Q1), so every `ToolStarted` yielded before the hook has been applied.
- **Minimal/cli_shell mode: almost always.** The loop runs in a producer task (`callback_event_stream`). The `ToolStarted` events of the last batch were queued when each call *started*, so the whole tool runtime has passed since, plus the hook's own awaits (sizing, storage reads) before `lifecycle.start`. The only failure is a consumer blocked for that whole window. If a strict guarantee is ever needed: in CLI mode, push a `CompactionMarker` through `_SINK` and let `_process_stream_events` and `_consume_streaming_chunks` stamp `after_tool` in-band. That is about 25 LOC and not needed for v1.

## Q4. Smallest correct wiring and how it survives each case

See "Recommended wiring" below. Per concern:

- **Where `after_tool` is read.** In `ReplyCompactions.start()`, through a `tool_count` callable bound by the consumer:
  - `StreamingResponse` binds `lambda: len(streaming.tool_trace) if streaming.show_tool_calls else 0`. This is the same rule as `add_reasoning` (streaming.py:660-674).
  - The collected path binds the same expression over `CollectedStreamPresentation.tool_trace`.
  - Unbound means 0.
  - It is read once, at `start`, and never recomputed.
- **Phase.** Add `phase: Literal["pre_turn", "mid_turn"] = "pre_turn"` to `CompactionLifecycleStart` (history/types.py:202-213). `_run_scope_compaction_with_lifecycle` (runtime.py:316) gets a `phase` param. The pre-turn call site (runtime.py:~255) passes `"pre_turn"` and `compact_scope_mid_turn` (runtime.py:558) passes `"mid_turn"`. No other types change.
- **running → success/failure updates the same entry.** `start()` returns the entry `id` (uuid hex) in place of a Matrix event id. `_run_scope_compaction_with_lifecycle` already threads that return value back:
  - `progress` gets `event.notice_event_id` (runtime.py:364-368),
  - success gets `outcome.lifecycle_notice_event_id` (runtime.py:372-377),
  - failure gets `failure.notice_event_id` (runtime.py:350-362).
  - Later notices are sent only when start returned non-None (runtime.py:342-343), which still holds.
  - The recorder updates the entry with that id in place.
  - Cancellation already calls `_fail("failed", …)` (runtime.py:381-384).
- **Stream restarts and resumed attempts.**
  - *Within one `deliver_stream`*, the recorder lives for the whole `_deliver_turn`, so entries survive continuation attempts and fallback retries. `after_tool` counts the shared `tool_trace`, which already includes `resumed_tool_trace`. That matches `ReplacementStreamingResponse._update`'s `remap_visible_tool_marker_indices` offset (streaming.py:1460-1470), so marker numbering and `after_tool` agree.
  - *A restart replay* (`reply_scope.py:716-734`, a new span with `UnfinishedStreamedReply`) builds a new `_deliver_turn` and recorder. Its edits replace `m.new_content`, so it seeds its recorder with the stopped attempt's entries: each answer `Segment` of the reply's records stores the entries it showed (`SpanHandle.compactions`), and `_handle_for` hands them to the replay as `prior_compactions`. A `running` entry from the dead span stays as recorded; the client shows it as interrupted on a terminal reply.
  - *An approval resume* (`approval/continuation_runner.py`) runs without a lifecycle. The paused reply's suspension flush carries the entries (`_send_or_edit_message` → `_delivery_extra_content`, streaming.py:2263-2270), and the resume's edits drop them. Same as reasoning.
  - Carrying entries across spans would mean adding `compactions` to `reply_presentation.Segment` and its codec (reply_presentation.py:68-95, 262+). That is about 40 LOC, deferred.
- **Ledger-rendered terminal writes.** Cancel, error, interrupted, and delivery-failed writes go through `_send_rendered_reply` and carry only `stream_status` (delivery_gateway.py:1189-1206). They drop the key, so the marker disappears. **Client rule:** an entry with `status == "running"` on an event whose `io.mindroom.stream_status` is terminal (or that no longer streams) renders as "interrupted", not as ticking.
- **Large messages** (`matrix/large_messages.py`). `io.mindroom.*` keys that are not in `_SIDECAR_ONLY_MINDROOM_KEYS` (:71-79) pass through to the preview event and the edit wrapper (:98-111). The new key is small (a few hundred bytes per entry), so it **just survives, with no change**. `fit_reasoning` (:625-670) handles only reasoning, and it doesn't need to change.
- **Segmented messages** (`matrix/segmented_messages.py`). `_rich_text_content` (:142-155) copies every non-dropped key onto **every** segment. Each segment's tool trace is renumbered from 1 (`_with_tool_trace`, :207-230), so `after_tool` would be wrong on continuations. Fix: keep the key on the **first segment only**, by stripping it in `_continuation_content` (:179-197). On the first segment, `after_tool` is still exact for tools in that chunk, and a larger value clamps to the segment end on the client. Reasoning is dropped on split replies (`_INTERNAL_STREAM_KEYS`, :42), but compaction markers are cheap and worth keeping.
- **Live frames** (`live_frames/frames.py`).
  - Yes, the client gets frames for progress. `_run_live_frames` (streaming.py:2047-2085) publishes `streaming.live_state` (streaming.py:695-705) whenever `live_active` holds: event exists, `_shows_stream`, homeserver relays, and `defaults.streaming.live_frames` is on. That includes the adopted placeholder during pre-turn compaction.
  - While frames run, `force_refresh` durable edits are **suppressed to checkpoints** (`live_checkpoints_only`, streaming.py:1843-1850; `_throttled_send` live branch, :841-851).
  - So the entries must be in `LiveDocs`: add a field `compactions`, plus `to_json` and `live_delta`. Durable checkpoints must also notice compaction changes, via `has_uncommitted_presentation` (streaming.py:1252-1259) and `schedule_checkpoint` (:2060-2075). Otherwise a client that reloads mid-compaction sees only the placeholder.
  - The client ticks the duration itself from `started_at`, so no periodic writes are needed.
- **Teams.** Team turns use the same `_deliver_turn`. The team `stream` closure passes `compaction_lifecycle` into `team_response_stream` (response_runner.py:1753-1766). The leader's pre-turn and mid-turn compaction use it (`teams/envelope.py:447, 486, 246-266`). `ReplacementStreamingResponse.tool_trace` comes from snapshots (`apply_tool_trace_snapshot`, streaming.py:726-732), and `len(tool_trace)` is the right anchor. Members get no lifecycle (`HookTurn` docstring, loop_hooks.py:64-66), and team runs outside Matrix get `None` (envelope.py:292). Both are unchanged.
- **Replies that are not streamed, or not visible:**
  - **Non-streamed (`_deliver_collected`).** The recorder is bound to the collected trace count and a no-op nudge. Entries land on the single final message through `final_extra_content()` → `FinalDeliveryRequest` (response_runner.py:1941-1944, 2119-2125). There is no live "running" state, but the typing indicator still shows. If you want live feedback there, keep `MatrixCompactionLifecycle` when `not use_streaming`, a one-line choice in `_deliver_turn`. I recommend the inline final record for consistency.
  - **Quiet, contact, or participation-gated.** `_build_compaction_lifecycle` returns `None` today (response_runner.py:864-865). Keep it, so they stay silent.
  - **Voice calls** (`matrix_rtc/call_tools.py:764`), **listening** (`listening/turn.py:161`), **delegated children** (`ai.py:1082-1104`), **approval execution** (`approval/execution.py:299`), **OpenAI-compat** (`api/openai_compat.py:860`), and **CLI approval recovery** (`agent_cli/cli_approval_recovery.py:130`) all build `ResponseTurnContext` with no `compaction_lifecycle` (default `None`, response_turn.py:384). The only setters are response_runner.py:1063 and 1764/2367. They are **silent today**, so keep them silent. A delegated child's compaction could later surface on the caller's subagent live frames, but that is out of scope.
  - Run-local mid-turn compaction (`summarize_run_locally`, runtime.py:474; mid_turn_compaction.py:550) never calls the lifecycle today. Unchanged.

## Q5. Tests that cover the notice today

These must change:
- `tests/test_response_runner_agent.py::test_generate_response_keeps_first_turn_follow_up_effects_in_new_thread` (:2596-2716). It calls `ctx.compaction_lifecycle.start(CompactionLifecycleStart(mode="auto", ...))` and asserts that `DeliveryGateway._send_compaction_lifecycle_start` is awaited once, with the thread target and `reply_to_event_id`.
  - Either switch it to `mode="manual"`, which still sends the notice and keeps the thread-routing coverage,
  - or split it, and add an auto variant asserting no notice plus the entry on `$response`.

These keep passing unchanged but are worth knowing:
- `tests/test_response_runner_agent.py:495, 2091` (lifecycle `None` for no-report/quiet), `tests/test_response_runner_team.py:1402`, `tests/contacts/test_turns.py:721`. Still `None`.
- `tests/test_response_delivery_gateway.py:4177` (failure notice text) and `:4205-4264` (start/success notice, `io.mindroom.compaction` v3 metadata). The manual path still uses these. Keep them.
- `tests/test_partial_reply_context.py:425` (`content={COMPACTION_NOTICE_CONTENT_KEY: True}` is skipped). Keep it, for old events.
- `tests/test_large_messages_integration.py:185` (passthrough of `io.mindroom.compaction` on large edits). Keep, and optionally add the new key.
- History-layer tests drive the protocol through `RecordingCompactionLifecycle` (`tests/history_helpers.py:242-262`): `test_history_prepare_lifecycle.py` (59 refs), `test_mid_turn_compaction.py` (24), `test_history_compaction_rewrite.py`, `test_history_responsiveness.py`, `test_history_team_scope.py`, `test_mid_turn_compaction_resume_and_team.py`, `test_native_compaction_history.py`, `test_compact_context.py`, `test_mode_commands.py`, `test_responses_replay_estimate.py`, `test_run_hook_source_metadata.py`, `test_history_replay_planning.py`, `test_agent_turn_runtime.py`, `test_execution_preparation.py`, `conftest.py`. None of them construct or compare `CompactionLifecycleStart` by equality; the only constructors are gateway:4239 and runner:2605. A defaulted `phase` field is therefore source-compatible. Add one assertion that mid-turn starts carry `phase == "mid_turn"`, in `test_mid_turn_compaction.py`, which already records events (:747-858).

## Q6. Prompt-cache impact: none

- The notice never feeds history. `execution_preparation.py:17-18, 91-96` puts `COMPACTION_NOTICE_CONTENT_KEY` in `_NON_TURN_NOTICE_CONTENT_KEYS`. `_has_nothing_to_read` (:585-594) skips any message carrying it, and also a placeholder-only streamed reply. `_get_unseen_messages_for_sender` (:597-631) uses that filter. **Keep the key in the tuple**, both for old notices and for manual ones.
- The new key is presentation only:
  - The model-facing text of a Matrix message is `message.body`, with tool markers stripped (execution_preparation.py:205-214). Content keys never enter the prompt. The new key adds nothing to `body` or `formatted_body`.
  - `_has_nothing_to_read` matches dict keys exactly, so `io.mindroom.compactions` does **not** make a reply look like a notice.
  - A placeholder-only edit carrying the key is still `stream_status` plus the placeholder body, so it is still skipped as a placeholder.
- Compaction itself (summary rows, replay plan, `on_commit` rewriting the request) is unchanged. Only the lifecycle sink differs.
- Nothing stores the reply's content keys into runs. The run metadata collector is separate (response_turn.py:1013-1014).

---

## Recommended wiring (task list)

New constant: `COMPACTIONS_CONTENT_KEY = "io.mindroom.compactions"` in `constants.py`, next to :1076/1086.

**Entry schema.** Your proposal, with two additions:
- `mode`, which is cheap and lets manual move inline later;
- `summary_model` updated on progress and terminal events, because the fallback model can take over mid-run (runtime.py:346-349, 364-366).

```json
{"id": "hex", "phase": "pre_turn|mid_turn", "mode": "auto", "after_tool": 0,
 "status": "running|success|failed|timeout", "started_at": 1763400000000,
 "duration_ms": 1234, "summary_model": "…", "before_tokens": 0,
 "after_tokens": 0, "history_budget_tokens": 0, "runs_before": 0,
 "compacted_run_count": 0, "runs_remaining": 0, "failure_reason": "…"}
```

Optional keys are omitted when `None`. `duration_ms` is set on progress (elapsed so far) and on terminal status. `runs_remaining` appears only while running. The value is a list in start order. No version is needed yet; if one is wanted, use a wrapper `{"version": 1, "entries": [...]}`.

### Tasks

- [ ] **T1, `history/types.py`.** Add `CompactionPhase = Literal["pre_turn", "mid_turn"]` and `phase: CompactionPhase = "pre_turn"` on `CompactionLifecycleStart`. Reword the `CompactionLifecycle.start` docstring to "return a handle later events carry back". *(+4 LOC)*
- [ ] **T2, `history/runtime.py`.** Add `phase: CompactionPhase` to `_run_scope_compaction_with_lifecycle` (:316) and pass it into `CompactionLifecycleStart` (:331). Call sites: the pre-turn one (:~255, `prepare_scope_history`) passes `"pre_turn"`, and `compact_scope_mid_turn` (:558) passes `"mid_turn"`. *(+4)*
- [ ] **T3, new `src/mindroom/history/reply_compactions.py`.**
  - `class ReplyCompactions` (the per-reply recorder):
    - `_entries: list[dict]`, `_tool_count: Callable[[], int] | None`, `_changed: Callable[[], None] | None`, `version: int`.
    - `bind(*, tool_count, changed) -> None` and `unbind() -> None`.
    - `start(event: CompactionLifecycleStart) -> str`. This is the **only place `after_tool` is read**: `self._tool_count() if bound else 0`. It also sets `started_at=time.time_ns() // 1_000_000`, `status="running"`, appends, bumps the version, and calls `changed()`.
    - `update(entry_id, **fields)`: find by id, set fields, drop `runs_remaining` on terminal status, bump the version, call `changed()`.
    - `content() -> list[dict] | None`: a deep copy, or `None` when empty.
  - `class ReplyCompactionLifecycle` (implements the `CompactionLifecycle` protocol):
    - constructed as `__init__(recorder: ReplyCompactions, notice: MatrixCompactionLifecycle)`.
    - `start`: `if event.mode == "manual": return await self._notice.start(event)`. It must remember which ids belong to the notice, using a `set[str]`. Otherwise `return recorder.start(event)`.
    - `progress`, `complete_success`, `complete_failure`: route by id. Notice ids go to `self._notice`. Recorder ids get `update(...)` with:
      - progress: `status running`, `after_tokens`, `compacted_run_count`, `runs_remaining`, `duration_ms`, `summary_model`;
      - success: `status success`, `after_tokens`, `compacted_run_count`, `history_budget_tokens`, `duration_ms`, `summary_model`;
      - failure: `status=event.status` (`failed` or `timeout`), `failure_reason`, `duration_ms`, `summary_model`.
  - *(~110 LOC)*
- [ ] **T4, `response_runner.py`.**
  - `_build_compaction_lifecycle(target, request, recorder)` (:858) returns `ReplyCompactionLifecycle(recorder, MatrixCompactionLifecycle(...))` and keeps the `quiet`/`participation` → `None` rule. Widen the type hints from `MatrixCompactionLifecycle | None` to `CompactionLifecycle | None` at :355, :1015, :1756, :2345.
  - In `_deliver_turn` (:1916-1965), create `compactions = ReplyCompactions()`. Pass it to the lifecycle, to `_deliver_streamed` (put it on `StreamingDeliveryRequest(compactions=compactions)`, :2018-2043), and to `_deliver_collected`.
  - Extend `final_extra_content()` (:1941-1944) with `{COMPACTIONS_CONTENT_KEY: entries}` when there are any. This covers `finalize_streamed_response` terminal writes and `deliver_final`.
  - In `_deliver_collected`, `collect()` (:2080-2091) binds `compactions.bind(tool_count=lambda: len(presentation.tool_trace) if runtime.show_tool_calls else 0, changed=lambda: None)`. Hoist `presentation` into a local.
  - *(+20)*
- [ ] **T5, `delivery_gateway.py`.** Add `compactions: ReplyCompactions | None = None` to `StreamingDeliveryRequest` (:477-546) and pass it through in `deliver_stream` → `send_streaming_response(compactions=...)` (:~1980-2014). `MatrixCompactionLifecycle` and `_send/_edit_compaction_lifecycle_*` stay for manual. *(+4)*
- [ ] **T6, `streaming.py`.** *(+35)*
  - `StreamingResponse` gets a field `compactions: ReplyCompactions | None = None` and `_last_delivered_compactions_version: int = field(default=0, init=False)`.
  - `send_streaming_response(…, compactions=None)` (:2151): after `delivery_queue` is created (:2228), call `compactions.bind(tool_count=lambda: len(streaming.tool_trace) if streaming.show_tool_calls else 0, changed=lambda: _queue_delivery_request(delivery_queue, force_refresh=True, allow_empty_progress=not streaming.accumulated_text.strip()))`. Call `unbind()` in the outer `finally` next to the `tool_trace_collector` copy (:2302-2304). This is the same nudge as `_drain_worker_progress_events` (:1766-1772).
  - `_delivery_extra_content` (:1160-1172): add `COMPACTIONS_CONTENT_KEY: self.compactions.content()` on **every** write, progress and final. Reasoning is final-only, but compactions need live status on durable edits too.
  - `_delivery_snapshot` (:1134-1136): add `compactions_dirty` (version != last delivered) to the "may write an empty progress" condition, next to `warmup_suffix_lines`.
  - Record the version on commit: carry `compactions_version` in `_StreamingDeliverySnapshot` and `_CommittedDeliveryState` (:267-280, 351-372), and set `_last_delivered_compactions_version` in `_mark_delivery_committed` (:1206).
  - `has_uncommitted_presentation` (:1252-1259): `or compactions_dirty`. `schedule_checkpoint` (:2060-2075) then covers it.
  - Live-checkpoint `_throttled_send` (:841-849): `allow_empty_progress = needs_warmup_clear_edit or compactions_dirty`.
  - `live_state` (:695-705): pass `compactions=tuple(self.compactions.content() or ())`.
  - Leave `ReplacementStreamingResponse` alone; it inherits all of this.
- [ ] **T7, `live_frames/frames.py`.** Add `compactions: tuple[dict[str, object], ...] = ()` to `LiveDocs`, add `"compactions"` in `to_json`, and in `live_delta` add `if before.compactions != after.compactions: delta["compactions"] = [...]` (whole-list replace, like `segments`). `FRAME_VERSION` stays 1 because the change is additive. *(+6)*
- [ ] **T8, `matrix/segmented_messages.py`.** In `_continuation_content` (:179-197), pop `COMPACTIONS_CONTENT_KEY` so only the first segment carries markers. *(+3)*
- [ ] **T9, no change** to `execution_preparation.py`: keep `COMPACTION_NOTICE_CONTENT_KEY` in `_NON_TURN_NOTICE_CONTENT_KEYS`. **No change** to `large_messages.py` either.

### Tests to add or change

- **Change** `tests/test_response_runner_agent.py::test_generate_response_keeps_first_turn_follow_up_effects_in_new_thread` (:2596): use `mode="manual"` to keep the notice-routing assertion. **Add** a sibling where `mode="auto"` sends **no** notice (`_send_compaction_lifecycle_start` not awaited) and the final `$response` content has `io.mindroom.compactions[0]` with `status: "success"`, `phase: "pre_turn"`, `after_tool: 0`.
- **Add** `tests/test_reply_compactions.py` (unit tests for the recorder and lifecycle):
  - start, then progress, then success updates one entry, in place, by id;
  - failure sets `timeout` or `failed` and `failure_reason`, and drops `runs_remaining`;
  - manual goes to the notice double, and its ids route back to it;
  - `after_tool` is read once at start, so a later change to the bound count leaves it untouched;
  - an unbound recorder gives `after_tool == 0`.
- **Add** to `tests/live_frames/test_live_streaming.py`, next to the reasoning-segment tests at :233-338:
  - (a) a compaction started after two visible tools anchors `after_tool: 2`;
  - (b) with `show_tool_calls=False` it anchors at 0;
  - (c) frames carry `compactions` in the snapshot and delta;
  - (d) a start while only the placeholder shows causes a progress edit of the placeholder carrying the key (`allow_empty_progress`);
  - (e) the final event keeps the success entry.
- **Add** to `tests/test_streaming_edits.py` (or `test_streaming_finalize.py`): a compaction change with live frames active schedules a durable checkpoint (`has_uncommitted_presentation`).
- **Add** to `tests/test_mid_turn_compaction.py`: the recorded start has `phase == "mid_turn"`. Pre-turn tests in `test_history_prepare_lifecycle.py` assert `phase == "pre_turn"` once.
- **Add** to `tests/test_segmented_messages.py`, next to `test_split_reply_keeps_no_reasoning` (:181): a split reply keeps `io.mindroom.compactions` on the first segment only.
- **Add** to `tests/test_partial_reply_context.py`: a reply carrying `io.mindroom.compactions` is **not** skipped from unseen context, while the old `io.mindroom.compaction` notice still is (:425).
- **Optional:** a collected (non-streamed) delivery puts the entry on the final message (`tests/test_ai_stream_collection.py` or the runner tests).
- **Keep:** gateway notice tests (:4177, :4205), large-message passthrough (:185), and the `None`-lifecycle assertions.

### Size estimate

- Source: about **190 LOC**. T3 is about 110, T6 about 35, T4 about 20, T1/T2/T5/T7/T8 about 20 together. Nothing is deleted, since the manual notice path stays.
- Tests: about **220-280 LOC**.
- Follow-ups not in v1:
  - carry entries across spans via `reply_presentation.Segment` (approval resume or restart; about 40 LOC);
  - strict in-band `after_tool` for minimal/cli mode via the `_SINK` marker (about 25 LOC);
  - put manual inline too, which would mean dropping the placeholder hold-back in `_send_placeholder` (about 10 LOC plus test churn).
