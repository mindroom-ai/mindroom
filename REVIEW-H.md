# REVIEW-H.md (Round 3)

## Verdict: APPROVE

## Findings

None.

## Architectural review

R2 fixes addressed every Round 2 finding I raised, and the broader architectural invariants from FINAL-PLAN.md still hold.

### Round 2 findings — verification

1. **R2#1 grace-window edge case (D5)**: The reporter loop is no longer driven by `wait_for_ready` poll ticks. `_ProgressReporterState` now stores `started_at = time.monotonic()` (`kubernetes.py:45,209`); `_progress_reporter_events` computes `wait_timeout = started_at + _next_progress_deadline_elapsed(state) - time.monotonic()` and does a `condition.wait(timeout=…)` (`kubernetes.py:140-152`). `_pending_progress_events` re-derives `elapsed_seconds` from monotonic at emit time (`kubernetes.py:78`). The 1.5 s grace fires precisely regardless of whether the kubernetes resource layer polls every 0.5 s, 2 s, or never. `on_poll_tick` is now only a wakeup nudge (`kubernetes.py:225-227`). Fixed.

2. **R2#2 late-joining sink (D8)**: `_progress_snapshots` is keyed by `worker_key` and updated atomically with sink fanout under `_progress_sinks_lock` (`kubernetes.py:328-335`). `_register_progress_sink` replays the latest non-terminal snapshot to a newly-attached sink under the same lock (`kubernetes.py:308-313`). Terminal phases (`ready` / `failed`) clear the snapshot so a sink that registers AFTER terminal does not see a stale notice; `_unregister_progress_sink` of the last sink also pops the snapshot, which is correct because the next emission from the still-running reporter will rebuild it. Fixed.

3. **R2#3 workloop scope creep**: `tests/test_workloop_thread_scope.py` is byte-identical to `origin/main` (`git diff origin/main..ed94c34a4 -- tests/test_workloop_thread_scope.py` is empty). Fixed.

### No new bugs introduced

- **finalize ↔ reporter race**: `_finalize_progress_events` computes pending+terminal events under the condition lock and sets `reporter_done=True` atomically (`kubernetes.py:164-179`). The reporter loop checks `reporter_done` immediately on re-entering the lock, so it cannot double-emit the same events that finalize consumed. `thread.join(timeout=1.0)` (`kubernetes.py:238`) protects finalize from a stuck sink without affecting the in-the-clear case.
- **finalize ↔ reporter sink ordering**: Both call paths use `self._emit_progress` as the sink; that method serializes on `_progress_sinks_lock`, so terminal events never interleave with mid-flight reporter emissions.
- **`_emit_progress` holds lock across sink callbacks**: Acceptable because the only sink in production is the asyncio bridge in `sandbox_proxy._make_progress_sink` (`sandbox_proxy.py:434-449`), which does a non-blocking `loop.call_soon_threadsafe` — no risk of deadlock or unbounded hold time.
- **Snapshot replay correctness across overlapping requests**: When two `ensure_worker` calls share a `worker_key`, both register sinks before the inner `_worker_lock` serializes the second behind the first. Snapshot replay covers the registration-during-cold-start window; terminal cleanup prevents stale `ready`/`failed` replay. Verified end-to-end above.

### Plan invariants still satisfied

- **D1/D12**: WorkerBackend owns the reporter and `ProgressSink`; `local.py` and `static_runner.py` accept-and-ignore.
- **D2 side-band**: `_active_warmups` stays out of `accumulated_text`/`tool_trace`/`extra_content`; suffix is composed only inside `_send_or_edit_message` (`streaming.py:359-394`); `finalize` clears warmup state before composing terminal body (`streaming.py:312-313`).
- **D8 worker_key coalescing**: same-key entries accumulate `tool_labels`; distinct keys produce distinct lines (`streaming.py:451-461`).
- **D9 single-direction pump**: `worker_progress_pump_scope` always sets `shutdown` in `finally` (`runtime_context.py:594-607`); drain checks `pump.shutdown` after every queue read; `_shutdown_worker_progress_drain` runs before finalize in every cancel/error/success branch (`streaming.py:776-820`).
- **D10 ContextVar install**: streaming entry point only.
- **D11 finalize discipline**: warmups cleared before terminal body composition.

`tach.toml` updates are present in the same PR.

## Final summary

APPROVE — R2 fixes correctly resolve the grace-window decoupling, snapshot replay, and scope-creep issues without introducing new ownership or invariant violations.
