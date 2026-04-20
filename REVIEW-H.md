# REVIEW-H.md (Round 4)

## Verdict: APPROVE

## Findings

None.

## Architectural review

R3 is a single test-only delta on top of R2 (`tests/test_kubernetes_worker_backend.py`). Production code (`kubernetes.py`, `kubernetes_resources.py`, `streaming.py`, `runtime_context.py`, `sandbox_proxy.py`, backend interfaces) is byte-identical to ed94c34a4. All architectural invariants verified in R3 still hold:

- **Reporter ownership (D1/D12)**: WorkerBackend still owns the reporter thread, condition, and ProgressSink fanout (`kubernetes.py:135-238`).
- **Time-driven, not poll-driven (R2#1 fix)**: `_progress_reporter_events` continues to compute `wait_timeout` from `state.started_at + _next_progress_deadline_elapsed(state) - time.monotonic()` (`kubernetes.py:144`); R3 explicitly proves this with a controllable clock.
- **Snapshot-replay & terminal cleanup (R2#2 fix)**: Unchanged.
- **D2/D8/D9/D10/D11 streaming-side invariants**: Unchanged.

### Round-3 test fix is architecturally sound

1. **Tests production cadence, not toy cadence** — `_install_real_elapsed_wait_for_ready` now defaults to `kubernetes_resources_module._READY_POLL_INTERVAL_SECONDS` (1.0 s), matching `kubernetes_resources.py:33,428`. The R2 bug (cold-start only fired on poll ticks) is now actually catchable.
2. **Clock injection is total** — `monkeypatch.setattr(kubernetes_backend_module, "time", SimpleNamespace(monotonic=clock.monotonic, time=time.time))` and `threading` SimpleNamespace cover every `time.X` / `threading.X` symbol used in `kubernetes.py` (verified: `monotonic`, `time`, `Condition`, `Thread`, `Lock`). No production reference leaks to wall-clock.
3. **`_ControlledCondition` honours the reporter contract** — `wait(timeout=…)` returns when the simulated clock crosses the deadline OR a real `notify_all` arrives (clock-listener bridge), which is exactly the semantics the reporter loop assumes. `notify_all` increments a wakeup counter so a notify that races ahead of `wait` is not lost — this matches real `threading.Condition` reasonably and avoids spurious flakes.
4. **Assertions pin the timing-fix invariant**: cold_start fires between 1.5 s and 1.7 s (grace deadline, before next 1.0 s poll), and the silent-finish path is rejected. This is the exact regression R2#1 introduced and R3 prevents.
5. **No production drift to accommodate the test** — all the seams (`monotonic`, `sleep`, `on_iteration` on `_install_real_elapsed_wait_for_ready`) are test-helper extensions only; production helpers untouched.

`tach.toml` unchanged in this round (no boundary changes).

## Final summary

APPROVE — R3 fix tightens the regression test to genuinely exercise the production timing model without touching production code or invariants. Ready to merge.
