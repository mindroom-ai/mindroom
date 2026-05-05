# Summary

`src/mindroom/workers/manager.py` is a pure facade over `WorkerBackend`.
No meaningful duplication was found inside the manager's own behavior beyond intentional one-line delegation to the protocol-defined backend surface.
The closest related duplication is the repeated worker lifecycle method family implemented by the local, static-runner, and Kubernetes backends; that duplication belongs to backend implementations rather than this facade.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
WorkerManager	class	lines 14-67	related-only	WorkerManager WorkerBackend facade backend delegation	src/mindroom/workers/backend.py:15; src/mindroom/workers/runtime.py:203; src/mindroom/workers/backends/local.py:394
WorkerManager.backend_name	method	lines 20-22	related-only	backend_name property backend.backend_name	src/mindroom/workers/backend.py:18; src/mindroom/workers/backends/static_runner.py:45; src/mindroom/workers/backends/local.py:152; src/mindroom/workers/backends/kubernetes.py:249
WorkerManager.idle_timeout_seconds	method	lines 25-27	related-only	idle_timeout_seconds property backend.idle_timeout_seconds	src/mindroom/workers/backend.py:19; src/mindroom/workers/backends/static_runner.py:56; src/mindroom/workers/backends/local.py:163; src/mindroom/workers/backends/kubernetes.py:279
WorkerManager.ensure_worker	method	lines 29-37	related-only	ensure_worker WorkerSpec progress_sink backend.ensure_worker	src/mindroom/workers/backend.py:21; src/mindroom/workers/backends/static_runner.py:60; src/mindroom/workers/backends/local.py:168; src/mindroom/workers/backends/kubernetes.py:343
WorkerManager.get_worker	method	lines 39-41	related-only	get_worker worker_key backend.get_worker	src/mindroom/workers/backend.py:30; src/mindroom/workers/backends/static_runner.py:99; src/mindroom/workers/backends/local.py:206; src/mindroom/workers/backends/kubernetes.py:470
WorkerManager.touch_worker	method	lines 43-45	related-only	touch_worker last_used backend.touch_worker	src/mindroom/workers/backend.py:33; src/mindroom/workers/backends/static_runner.py:108; src/mindroom/workers/backends/local.py:216; src/mindroom/workers/backends/kubernetes.py:478
WorkerManager.list_workers	method	lines 47-49	related-only	list_workers include_idle sort last_used_at	src/mindroom/workers/backend.py:36; src/mindroom/workers/backends/static_runner.py:118; src/mindroom/workers/backends/local.py:229; src/mindroom/workers/backends/kubernetes.py:494
WorkerManager.evict_worker	method	lines 51-59	related-only	evict_worker preserve_state idle backend.evict_worker	src/mindroom/workers/backend.py:39; src/mindroom/workers/backends/static_runner.py:127; src/mindroom/workers/backends/local.py:243; src/mindroom/workers/backends/kubernetes.py:504
WorkerManager.cleanup_idle_workers	method	lines 61-63	related-only	cleanup_idle_workers idle timeout backend.cleanup_idle_workers	src/mindroom/workers/backend.py:48; src/mindroom/workers/backends/static_runner.py:147; src/mindroom/workers/backends/local.py:270; src/mindroom/workers/backends/kubernetes.py:533
WorkerManager.record_failure	method	lines 65-67	related-only	record_failure failure_reason backend.record_failure	src/mindroom/workers/backend.py:51; src/mindroom/workers/backends/static_runner.py:158; src/mindroom/workers/backends/local.py:287; src/mindroom/workers/backends/kubernetes.py:551
```

# Findings

No real duplication in `WorkerManager` itself.
Every symbol in `manager.py` forwards directly to the corresponding `WorkerBackend` member without adding parsing, validation, IO, lifecycle branching, or error handling.
The matching method names and signatures in `src/mindroom/workers/backend.py:15` are an intentional typed protocol contract, not duplicated behavior to consolidate.

Related backend implementation repetition exists across `src/mindroom/workers/backends/static_runner.py:60`, `src/mindroom/workers/backends/local.py:168`, and `src/mindroom/workers/backends/kubernetes.py:343`.
They all implement the same worker lifecycle vocabulary: ensure, get, touch, list, evict, cleanup idle workers, and record failure.
The behavior is not safe to generalize from `WorkerManager` because each backend has different persistence and side effects: in-memory metadata for static runner, filesystem metadata and state directories for local workers, and Kubernetes deployments, services, secrets, annotations, replicas, and readiness polling for Kubernetes workers.

The `list_workers` methods have similar filtering and sorting in static and local backends at `src/mindroom/workers/backends/static_runner.py:118` and `src/mindroom/workers/backends/local.py:229`, with Kubernetes following the same filter and sort pattern at `src/mindroom/workers/backends/kubernetes.py:494`.
This is duplicated backend-side presentation logic, but it is outside the primary file and not caused by the facade.

# Proposed Generalization

No refactor recommended for `WorkerManager`.
If backend duplication is audited separately, the smallest candidate would be a private helper near the backend implementations for filtering and sorting `WorkerHandle` lists by idle status and `last_used_at`.
Do not move lifecycle operations into `WorkerManager`; doing so would blur the current protocol boundary and would not remove meaningful manager-level code.

# Risk/Tests

Changing `WorkerManager` risks breaking the simple facade contract used by runtime construction and API routes.
A refactor of backend list filtering would need focused tests covering `include_idle=False`, descending `last_used_at` ordering, and backend-specific cleanup side effects for static, local, and Kubernetes workers.
No tests were run because this audit required report generation only and no production code edits.
