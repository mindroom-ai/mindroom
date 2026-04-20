# REVIEW-H

## Verdict: APPROVE

## Findings

None at the architectural / invariant level.

The implementation faithfully realizes the plan's ownership boundaries:

- **WorkerBackend layer (D1)** owns the reporter daemon thread and emits `WorkerReadyProgress` through a caller-supplied `ProgressSink` (`src/mindroom/workers/backends/kubernetes.py:129-182`). Backend-neutral types live in `workers/models.py:42-52`. `local.py` and `static_runner.py` accept-and-ignore as required by D12.
- **Pump bridge (D9)** is a single-direction conduit: sync sink → `loop.call_soon_threadsafe(queue.put_nowait, ...)` → async drain → `apply_worker_progress_event`. The shutdown flag + `loop.is_closed()` guard + `suppress(RuntimeError)` defends the closed-loop race exactly as specified (`src/mindroom/tool_system/sandbox_proxy.py:433-450`, `runtime_context.py:595-607`).
- **StreamingResponse (D2 side-band)** keeps `_active_warmups` strictly out of `accumulated_text`, `tool_trace`, and `extra_content`. The suffix is composed only inside `_send_or_edit_message` from `_render_warmup_suffix` (`src/mindroom/streaming.py:355-368`, `408-429`). `finalize()` clears warmups before composing the cancelled/error/complete body (`streaming.py:309`), satisfying D11.
- **Worker_key coalescing (D8)** is implemented correctly in `apply_worker_progress_event` (`streaming.py:431-454`): same `worker_key` appends a tool label to one entry; distinct keys produce one warmup line each.
- **ContextVar install location (D10)** is the streaming entry point, naturally skipping the OpenAI-compat endpoint and propagating to nested delegates via PEP 567.
- **Cold-start detection (D1+D5)** correctly skips reporting when an existing deployment is already ready (`kubernetes.py:260`); warm-but-restarting paths emit a single terminal `ready` event that is a no-op when no `_active_warmups` entry exists (`streaming.py:435-436`). The reporter thread's wait/notify discipline plus `_emit_pending_progress_events` backlog drain after `thread.join()` keeps phase ordering correct under bursty `on_poll_tick` calls without lock contention on the daemon-owned fields.

Lock-free reads of `cold_start_emitted` / `next_waiting_elapsed` (`kubernetes.py:74-98`) are safe by ownership: only the reporter thread writes them while alive, and finalize touches them only after `thread.join()`.

The `_drain_worker_progress_events` → `_throttled_send(progress_hint=True)` interaction can theoretically race with `finalize`'s in-flight edit (drain processes a queued event during finalize's `await`), but the practical outcome is bounded: `_active_warmups` is cleared before finalize composes its body, lingering `cold_start`/`waiting` events would have been emitted before the worker became ready (reporter thread joined inside the synchronous tool call), and `progress_hint`'s 1.0 s throttle suppresses immediate follow-up edits. Not a blocker.

`tach.toml` boundary updates are present and correct (`mindroom.streaming` depends on `tool_system.runtime_context`; new exports `WorkerProgressEvent`, `WorkerProgressPump`, `worker_progress_pump_scope`, `get_worker_progress_pump`).

## Final summary

Implementation respects the plan's chosen invariants — side-band rendering, worker_key coalescing, 1.5 s grace, clean cancel/error finalize paths, no `ToolTraceEntry` extension, no `_append_queued_notice_if_needed` reuse, backend-neutral `progress_sink` contract. Ownership boundaries between worker backend, pump, drain task, and StreamingResponse are well-drawn and non-leaky. No refactor proposal: diff growth is justified by the new cross-thread plumbing and the 660 LOC of tests cover the contracted phases (cold_start / waiting / ready / failed), warm silence, coalescing, finalize-clears-suffix, and shutdown races.
