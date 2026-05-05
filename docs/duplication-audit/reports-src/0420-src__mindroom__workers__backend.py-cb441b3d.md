## Summary

Top duplication candidates for `src/mindroom/workers/backend.py` are the worker lifecycle contract repeated as pass-through facade methods in `src/mindroom/workers/manager.py` and as concrete implementations in the static, local, and Kubernetes worker backends.
The most concrete duplicated behavior is lifecycle bookkeeping across backends: timestamp resolution, idle filtering/sorting, idle eviction, idle cleanup, and failure recording all share the same user-visible semantics while differing in storage/resource effects.
No production refactor is required from this audit, but a small worker-state helper could reduce repeated lifecycle transitions if these backends continue to grow.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
WorkerBackendError	class	lines 11-12	related-only	WorkerBackendError RuntimeError backend unavailable error	src/mindroom/workers/backends/static_runner.py:10; src/mindroom/workers/backends/local.py:15; src/mindroom/workers/backends/kubernetes.py:273; src/mindroom/matrix/cache/event_cache.py:23
WorkerBackend	class	lines 15-52	duplicate-found	WorkerBackend Protocol backend contract lifecycle methods	src/mindroom/workers/manager.py:13; src/mindroom/workers/backends/static_runner.py:42; src/mindroom/workers/backends/local.py:149; src/mindroom/workers/backends/kubernetes.py:246; src/mindroom/matrix/cache/event_cache.py:27
WorkerBackend.ensure_worker	method	lines 21-28	duplicate-found	def ensure_worker WorkerSpec progress_sink now WorkerHandle	src/mindroom/workers/manager.py:29; src/mindroom/workers/backends/static_runner.py:60; src/mindroom/workers/backends/local.py:168; src/mindroom/workers/backends/kubernetes.py:343
WorkerBackend.get_worker	method	lines 30-31	duplicate-found	def get_worker worker_key now WorkerHandle None	src/mindroom/workers/manager.py:39; src/mindroom/workers/backends/static_runner.py:99; src/mindroom/workers/backends/local.py:206; src/mindroom/workers/backends/kubernetes.py:470
WorkerBackend.touch_worker	method	lines 33-34	duplicate-found	def touch_worker last_used_at now idle ready	src/mindroom/workers/manager.py:43; src/mindroom/workers/backends/static_runner.py:108; src/mindroom/workers/backends/local.py:216; src/mindroom/workers/backends/kubernetes.py:478
WorkerBackend.list_workers	method	lines 36-37	duplicate-found	def list_workers include_idle sorted last_used_at	src/mindroom/workers/manager.py:47; src/mindroom/workers/backends/static_runner.py:118; src/mindroom/workers/backends/local.py:229; src/mindroom/workers/backends/kubernetes.py:494
WorkerBackend.evict_worker	method	lines 39-46	duplicate-found	def evict_worker preserve_state idle delete service secret state	src/mindroom/workers/manager.py:51; src/mindroom/workers/backends/static_runner.py:127; src/mindroom/workers/backends/local.py:243; src/mindroom/workers/backends/kubernetes.py:504
WorkerBackend.cleanup_idle_workers	method	lines 48-49	duplicate-found	def cleanup_idle_workers idle_timeout ready idle sorted	src/mindroom/workers/manager.py:61; src/mindroom/workers/backends/static_runner.py:147; src/mindroom/workers/backends/local.py:270; src/mindroom/workers/backends/kubernetes.py:533
WorkerBackend.record_failure	method	lines 51-52	duplicate-found	def record_failure failure_reason failure_count status failed	src/mindroom/workers/manager.py:65; src/mindroom/workers/backends/static_runner.py:158; src/mindroom/workers/backends/local.py:287; src/mindroom/workers/backends/kubernetes.py:551
```

## Findings

### Worker lifecycle API is repeated in the protocol, facade, and all backends

`WorkerBackend` defines the backend-neutral lifecycle in `src/mindroom/workers/backend.py:15`.
`WorkerManager` mirrors the same public methods and delegates directly to the backend in `src/mindroom/workers/manager.py:13`.
The same lifecycle is implemented by `StaticSandboxRunnerBackend` in `src/mindroom/workers/backends/static_runner.py:42`, `_LocalWorkerBackend` in `src/mindroom/workers/backends/local.py:149`, and `KubernetesWorkerBackend` in `src/mindroom/workers/backends/kubernetes.py:246`.

This is intentional structural duplication from a protocol/facade/backend pattern rather than accidental logic cloning.
The facade does not currently add policy, validation, logging, or cross-backend behavior beyond forwarding.
Differences to preserve are backend storage effects: in-memory metadata for static workers, JSON metadata and filesystem state for local workers, and Kubernetes deployments/services/secrets for dedicated workers.

### Worker list and cleanup semantics are near-duplicated across backends

`list_workers` in `src/mindroom/workers/backends/static_runner.py:118`, `src/mindroom/workers/backends/local.py:229`, and `src/mindroom/workers/backends/kubernetes.py:494` all resolve `now`, convert backend records to `WorkerHandle`, filter out idle workers when requested, and sort by `last_used_at` descending in the static and local backends.
The Kubernetes backend performs the same filtering and sorting for `list_workers`.

`cleanup_idle_workers` in `src/mindroom/workers/backends/static_runner.py:147`, `src/mindroom/workers/backends/local.py:270`, and `src/mindroom/workers/backends/kubernetes.py:533` also share the same high-level transition: find ready workers whose effective status is idle, mark them idle, return cleaned handles.
Kubernetes additionally scales deployments to zero and deletes service/secret resources, so only the selection/filter/sort shape is shared.

### Eviction and failure transitions repeat the same state model

`evict_worker` in `src/mindroom/workers/backends/static_runner.py:127`, `src/mindroom/workers/backends/local.py:243`, and `src/mindroom/workers/backends/kubernetes.py:504` all return `None` for unknown workers, mark preserved workers idle with updated `last_used_at`, and remove runtime resources when `preserve_state=False`.
Local deletes the worker root, Kubernetes deletes deployment/service/secret resources, and static removes the in-memory record.

`record_failure` in `src/mindroom/workers/backends/static_runner.py:158`, `src/mindroom/workers/backends/local.py:287`, and `src/mindroom/workers/backends/kubernetes.py:551` all set status to `failed`, refresh `last_used_at`, increment `failure_count`, and persist `failure_reason`.
Static and local can create metadata for unknown workers, while Kubernetes requires an existing deployment and raises `WorkerBackendError`.

### WorkerBackendError is a shared domain exception, not duplicated behavior

`WorkerBackendError` in `src/mindroom/workers/backend.py:11` is used by worker backends for domain failures, including static configuration errors in `src/mindroom/workers/backends/static_runner.py:70`, local initialization failures in `src/mindroom/workers/backends/local.py:198`, and Kubernetes validation/resource failures in `src/mindroom/workers/backends/kubernetes.py:273`.
`EventCacheBackendUnavailableError` in `src/mindroom/matrix/cache/event_cache.py:23` is a related storage-contract exception pattern, but it serves a different subsystem and should not be generalized with worker errors.

## Proposed Generalization

No immediate refactor recommended for `WorkerBackend` itself because it is a protocol and the repeated method list is the point of the contract.
If the worker backends continue to change together, consider a small helper in `src/mindroom/workers/state.py` for pure lifecycle transitions over metadata-like objects or handles:

1. Add pure helpers for `effective_worker_status(status, last_used_at, idle_timeout_seconds, now)`, `filter_and_sort_worker_handles(handles, include_idle)`, and possibly transition helpers for idle/failure metadata.
2. Use the filtering/sorting helper in all three backend `list_workers` methods.
3. Use the effective-status helper in static and local backends, and only in Kubernetes if annotation parsing stays outside the helper.
4. Keep resource effects, filesystem writes, and Kubernetes patch/delete operations inside each backend.

Do not merge `WorkerManager` into the protocol unless it remains a pure dependency-injection facade with no planned policy, because removing it would be an API shape decision rather than a duplication fix.

## Risk/tests

The main risk in generalizing lifecycle helpers is hiding backend-specific persistence side effects.
Tests should pin visible lifecycle behavior for all backends: unknown worker lookup/eviction, `include_idle=False`, sorting by `last_used_at`, preserved eviction returning an idle handle, destructive eviction returning `None`, cleanup of timed-out workers, and failure count/reason updates.
Kubernetes tests also need to verify resource side effects: scale-to-zero, service deletion, secret deletion, and unknown-worker failure behavior.
