# REVIEW-H

## Verdict: APPROVE

## Findings

1. [MINOR] tests/test_workloop_thread_scope.py:87-101 — the new `_plugin_checkout_available()` guard is unrelated to ISSUE-183 worker progress and tightens the workloop-plugin skip predicate to require a fixed list of files (`commands.py`, `formatting.py`, `hooks.py`, `poke.py`, `state.py`, `todos.py`, `types.py`) — a list that will silently drift from the ecosystem checkout. Fix: drop this hunk from the branch (or move it to its own scope-named PR) so ISSUE-183 stays focused on cold-start surfacing.

## Architectural review

The implementation faithfully realizes the plan's ownership boundaries and the 12 design decisions:

- **WorkerBackend layer (D1, D12)** owns the reporter daemon thread and emits `WorkerReadyProgress` through a caller-supplied `ProgressSink` (`src/mindroom/workers/backends/kubernetes.py:148-182`, `351-394`). Backend-neutral types live in `workers/models.py:42-52`. `local.py:174` and `static_runner.py:66` accept-and-ignore as required by D12. Protocol updated in `workers/backend.py:20-27`; `WorkerManager.ensure_worker` is a one-line forward (`workers/manager.py:29-37`).
- **Pump bridge (D9)** is a single-direction conduit: sync sink → `loop.call_soon_threadsafe(queue.put_nowait, ...)` → async drain → `apply_worker_progress_event`. The `pump.shutdown.is_set()` short-circuit + `loop.is_closed()` guard + `suppress(RuntimeError)` defends the closed-loop race exactly as specified (`src/mindroom/tool_system/sandbox_proxy.py:433-450`, `runtime_context.py:595-607`). `worker_progress_pump_scope` is a contextmanager that always sets `shutdown` in `finally`, so an unhandled stream exception cannot leak the daemon-side bridge.
- **StreamingResponse (D2 side-band)** keeps `_active_warmups` strictly out of `accumulated_text`, `tool_trace`, and `extra_content`. The suffix is composed only inside `_send_or_edit_message` from `_render_warmup_suffix_lines` (`src/mindroom/streaming.py:359-368`, `389-394`). `finalize()` clears `_active_warmups` and `_needs_warmup_clear_edit` before composing the cancelled/error/complete body (`streaming.py:312-313`), satisfying D11. `update_content` calls `_clear_terminal_warmups` first, so a failed warmup notice is dropped as soon as real text resumes (`streaming.py:298`, `407-413`).
- **Worker_key coalescing (D8)** is implemented correctly in `apply_worker_progress_event` (`streaming.py:435-461`): same `worker_key` appends a tool label to one entry; distinct keys produce one warmup line each. Backend fanout (`kubernetes.py:217-237`) snapshots the sink list under the lock before dispatch — no callback runs while the registry is mutated.
- **ContextVar install location (D10)** is the streaming entry point (`streaming.py:776-820`), naturally skipping the OpenAI-compat endpoint and propagating to nested delegates via PEP 567.
- **Cold-start detection (D1+D5)** correctly skips reporting when an existing deployment is already ready (`kubernetes.py:262`); warm-but-restarting paths emit a single terminal `ready` event that is a no-op when no `_active_warmups` entry exists (`streaming.py:439-444`). The reporter thread's wait/notify discipline plus `_emit_pending_progress_events` backlog drain after `thread.join()` keeps phase ordering correct under bursty `on_poll_tick` calls without lock contention on reporter-owned fields.
- **Tach boundaries** are updated in the same PR (`tach.toml`): `mindroom.streaming` now depends on `tool_system.runtime_context`; new exports `WorkerProgressEvent`, `WorkerProgressPump`, `worker_progress_pump_scope`, `get_worker_progress_pump`.

Lock-free reads of `cold_start_emitted` / `next_waiting_elapsed` / `latest_elapsed` (`kubernetes.py:46-50`) are safe by ownership: only the reporter thread writes them while alive, and finalize touches them only after `thread.join()`. `on_poll_tick` mutates `latest_elapsed` exclusively under the condition lock.

The `_drain_worker_progress_events` → `_throttled_send(progress_hint=True)` interaction can theoretically race with `finalize`'s in-flight edit, but the practical outcome is bounded: the drain is shut down via `_shutdown_worker_progress_drain` *before* finalize composes its body in every cancel/error/success branch (`streaming.py:776-820`), the daemon-side bridge checks `pump.shutdown` after every queue read, and `_active_warmups` is cleared before finalize composes its body. Not a blocker.

## Final summary

Implementation respects the plan's chosen invariants — side-band rendering, worker_key coalescing, 1.5 s grace, clean cancel/error finalize paths, no `ToolTraceEntry` extension, no `_append_queued_notice_if_needed` reuse, backend-neutral `progress_sink` contract. Ownership boundaries between worker backend, pump, drain task, and StreamingResponse are well-drawn and non-leaky. No refactor proposal — diff growth is justified by the new cross-thread plumbing and the 660 LOC of tests cover the contracted phases (cold_start / waiting / ready / failed), warm silence, coalescing, finalize-clears-suffix, and shutdown races. The only flag is the unrelated workloop-plugin guard tightening that should not ride on this branch.
