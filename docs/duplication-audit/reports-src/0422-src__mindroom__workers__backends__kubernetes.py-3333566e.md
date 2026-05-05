# Duplication Audit: `src/mindroom/workers/backends/kubernetes.py`

## Summary

Top duplication candidates:

1. Worker lifecycle operations in `KubernetesWorkerBackend` repeat the same backend contract behavior implemented by `_LocalWorkerBackend` and `StaticSandboxRunnerBackend`: timestamping, touch, list/sort, evict-to-idle, cleanup timed-out ready workers, failure recording, effective status, and `WorkerHandle` assembly.
2. Per-worker lock creation duplicates the same dictionary-backed lock pattern used by local worker initialization.
3. Kubernetes startup progress generation is mostly unique to this backend, with only related downstream consumption in sandbox proxy and streaming warmup rendering.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_ProgressReporterState	class	lines 44-48	none-found	ProgressReporterState cold_start_emitted next_waiting_elapsed reporter_done WorkerReadyProgress	src/mindroom/tool_system/sandbox_proxy.py:720; src/mindroom/streaming_warmup.py:22
_noop_finalize_progress	function	lines 51-52	none-found	noop finalize_progress WorkerReadyPhase failed ready	none
_progress_event	function	lines 55-69	none-found	WorkerReadyProgress phase worker_key backend_name elapsed_seconds error	src/mindroom/workers/models.py:42; src/mindroom/streaming_warmup.py:47
_pending_progress_events	function	lines 72-100	none-found	cold_start waiting elapsed progress events interval	src/mindroom/streaming_warmup.py:67; src/mindroom/streaming_warmup.py:73
_next_progress_deadline_elapsed	function	lines 103-106	none-found	next deadline cold_start waiting progress interval	none
_report_progress	function	lines 109-111	none-found	ProgressSink list WorkerReadyProgress for event sink	src/mindroom/tool_system/sandbox_proxy.py:720
_progress_terminal_event	function	lines 114-130	none-found	terminal ready failed progress event cold_start_emitted	src/mindroom/streaming_warmup.py:59
_progress_reporter_events	function	lines 133-152	none-found	threading.Condition reporter_done wait timeout progress events	none
_finalize_progress_events	function	lines 155-179	none-found	finalize progress pending terminal notify_all reporter_done	none
_progress_reporter_loop	function	lines 182-199	none-found	progress reporter loop pending events condition	none
_build_progress_reporter	function	lines 202-243	none-found	build progress reporter thread condition finalize on_poll_tick	src/mindroom/workers/backends/kubernetes_resources.py:527; src/mindroom/tool_system/sandbox_proxy.py:720
_build_progress_reporter.<locals>.on_poll_tick	nested_function	lines 225-227	related-only	on_poll_tick elapsed_seconds wait_for_ready progress notify_all	src/mindroom/workers/backends/kubernetes_resources.py:527
_build_progress_reporter.<locals>.finalize	nested_function	lines 229-241	none-found	finalize WorkerReadyPhase error terminal progress thread join	none
KubernetesWorkerBackend	class	lines 246-682	duplicate-found	worker backend ensure get touch list evict cleanup record_failure handle effective_status	src/mindroom/workers/backends/local.py:149; src/mindroom/workers/backends/static_runner.py:42
KubernetesWorkerBackend.__init__	method	lines 251-292	related-only	worker backend init idle_timeout locks progress sinks credentials resources	src/mindroom/workers/backends/local.py:154; src/mindroom/workers/backends/static_runner.py:47
KubernetesWorkerBackend.from_runtime	method	lines 295-312	none-found	from_runtime backend config runtime_paths factory	none
KubernetesWorkerBackend._register_progress_sink	method	lines 314-319	none-found	progress sinks snapshot register worker_key	none
KubernetesWorkerBackend._unregister_progress_sink	method	lines 321-332	none-found	progress sinks unregister remove snapshot worker_key	none
KubernetesWorkerBackend._emit_progress	method	lines 334-341	none-found	emit progress snapshots ready failed sinks	src/mindroom/tool_system/sandbox_proxy.py:720
KubernetesWorkerBackend.ensure_worker	method	lines 343-468	duplicate-found	ensure_worker timestamp startup_count last_used status ready failure progress local static	src/mindroom/workers/backends/local.py:168; src/mindroom/workers/backends/static_runner.py:60; src/mindroom/workers/backends/kubernetes_resources.py:527
KubernetesWorkerBackend.get_worker	method	lines 470-476	duplicate-found	get_worker timestamp read existing to_handle none	src/mindroom/workers/backends/local.py:206; src/mindroom/workers/backends/static_runner.py:99
KubernetesWorkerBackend.touch_worker	method	lines 478-492	duplicate-found	touch_worker timestamp last_used idle ready patch metadata	src/mindroom/workers/backends/local.py:216; src/mindroom/workers/backends/static_runner.py:108
KubernetesWorkerBackend.list_workers	method	lines 494-502	duplicate-found	list_workers include_idle sort last_used reverse handles	src/mindroom/workers/backends/local.py:229; src/mindroom/workers/backends/static_runner.py:118
KubernetesWorkerBackend.evict_worker	method	lines 504-531	duplicate-found	evict_worker preserve_state idle last_used delete service secret metadata	src/mindroom/workers/backends/local.py:243; src/mindroom/workers/backends/static_runner.py:127
KubernetesWorkerBackend.cleanup_idle_workers	method	lines 533-549	duplicate-found	cleanup_idle_workers ready effective_status idle sorted cleaned	src/mindroom/workers/backends/local.py:270; src/mindroom/workers/backends/static_runner.py:147
KubernetesWorkerBackend.record_failure	method	lines 551-581	duplicate-found	record_failure status failed last_used failure_count failure_reason handle	src/mindroom/workers/backends/local.py:287; src/mindroom/workers/backends/local.py:351; src/mindroom/workers/backends/static_runner.py:158
KubernetesWorkerBackend._record_startup_failure_or_cleanup_secret	method	lines 583-604	related-only	startup failure cleanup secret record_failure destructive auth_secret	src/mindroom/workers/backends/local.py:192
KubernetesWorkerBackend._worker_lock	method	lines 606-612	duplicate-found	worker_lock dictionary threading.Lock per worker key	src/mindroom/workers/backends/local.py:140; src/mindroom/workers/backends/local.py:296
KubernetesWorkerBackend._worker_id	method	lines 614-615	related-only	worker_id worker_key prefix hash worker_dir_name	src/mindroom/workers/backends/kubernetes_resources.py:174; src/mindroom/workers/backends/local.py:304; src/mindroom/workers/backends/static_runner.py:80
KubernetesWorkerBackend._state_subpath	method	lines 617-620	related-only	state_subpath storage_subpath_prefix worker_dir_name local paths	src/mindroom/workers/backends/local.py:92; src/mindroom/workers/backends/kubernetes_resources.py:665
KubernetesWorkerBackend._deployment_ready	method	lines 622-630	none-found	deployment ready replicas observed_generation generation Kubernetes	none
KubernetesWorkerBackend._handle_from_deployment	method	lines 632-667	duplicate-found	WorkerHandle endpoint auth_token status timestamps counts debug_metadata	src/mindroom/workers/backends/local.py:367; src/mindroom/workers/backends/static_runner.py:183; src/mindroom/workers/backends/kubernetes_resources.py:211
KubernetesWorkerBackend._effective_status	method	lines 669-682	duplicate-found	effective_status failed replicas idle timeout ready last_used	src/mindroom/workers/backends/local.py:346; src/mindroom/workers/backends/static_runner.py:178
```

## Findings

### 1. Worker backend lifecycle bookkeeping is repeated across all worker backends

`KubernetesWorkerBackend` repeats the same backend contract behavior found in `_LocalWorkerBackend` and `StaticSandboxRunnerBackend`.
The repeated behavior includes resolving `now` to a timestamp, updating `last_used_at`, deriving idle status from `last_used_at` and `idle_timeout_seconds`, listing handles with optional idle filtering and `last_used_at` descending sort, marking preserved evictions as idle, marking timed-out ready workers idle during cleanup, incrementing failure counts, and returning `WorkerHandle` values.

References:

- `src/mindroom/workers/backends/kubernetes.py:343` starts or resolves workers while maintaining `created_at`, `last_started_at`, `startup_count`, `failure_count`, `failure_reason`, and status annotations.
- `src/mindroom/workers/backends/local.py:168` performs the same lifecycle transition for local metadata.
- `src/mindroom/workers/backends/static_runner.py:60` performs the same lifecycle transition for in-memory metadata.
- `src/mindroom/workers/backends/kubernetes.py:494`, `src/mindroom/workers/backends/local.py:229`, and `src/mindroom/workers/backends/static_runner.py:118` all implement the same `include_idle` filter and `last_used_at` descending sort.
- `src/mindroom/workers/backends/kubernetes.py:504`, `src/mindroom/workers/backends/local.py:243`, and `src/mindroom/workers/backends/static_runner.py:127` all use the same preserve-state eviction semantics.
- `src/mindroom/workers/backends/kubernetes.py:533`, `src/mindroom/workers/backends/local.py:270`, and `src/mindroom/workers/backends/static_runner.py:147` all mark ready workers idle when their effective status has timed out.
- `src/mindroom/workers/backends/kubernetes.py:551`, `src/mindroom/workers/backends/local.py:351`, and `src/mindroom/workers/backends/static_runner.py:158` all persist failed status, timestamp, incremented failure count, and failure reason.
- `src/mindroom/workers/backends/kubernetes.py:632`, `src/mindroom/workers/backends/local.py:367`, and `src/mindroom/workers/backends/static_runner.py:183` all assemble the same `WorkerHandle` fields from backend-specific metadata.
- `src/mindroom/workers/backends/kubernetes.py:669`, `src/mindroom/workers/backends/local.py:346`, and `src/mindroom/workers/backends/static_runner.py:178` all derive idle status from timeout, with Kubernetes adding deployment readiness and stored failed-status rules.

Differences to preserve:

- Kubernetes persists state in deployment annotations and must patch/delete Kubernetes Deployments, Services, and Secrets.
- Local persists JSON metadata and creates/removes filesystem state.
- Static is in-memory and requires configured proxy URL/token.
- Kubernetes has additional startup progress events and deployment readiness checks.

### 2. Per-worker lock registry is duplicated

`KubernetesWorkerBackend._worker_lock` uses a lock-protected dictionary of per-worker locks.
The same pattern exists in the local backend for both shared initialization locks and instance initialization locks.

References:

- `src/mindroom/workers/backends/kubernetes.py:606`
- `src/mindroom/workers/backends/local.py:140`
- `src/mindroom/workers/backends/local.py:296`

Differences to preserve:

- Kubernetes uses a dedicated guard lock separate from the locks it returns.
- Local has both module-level shared initialization locks and instance-level backend locks.

### 3. Kubernetes progress reporting has related consumers but no duplicated producer

The progress reporter helpers in `kubernetes.py` generate delayed `cold_start`, repeated `waiting`, and terminal `ready` or `failed` events.
No other backend currently produces equivalent timed worker-startup progress.
The related code in sandbox proxy only forwards `WorkerReadyProgress` into the async pump, and streaming warmup renders those events for users.

References:

- `src/mindroom/workers/backends/kubernetes.py:44`
- `src/mindroom/workers/backends/kubernetes.py:72`
- `src/mindroom/workers/backends/kubernetes.py:202`
- `src/mindroom/tool_system/sandbox_proxy.py:720`
- `src/mindroom/streaming_warmup.py:47`

Differences to preserve:

- Progress generation depends on Kubernetes readiness polling and a background condition thread.
- Consumers intentionally know only the `WorkerReadyProgress` model and should not own backend timing policy.

## Proposed generalization

A small shared worker lifecycle helper could reduce active duplication without touching Kubernetes resource management.
The minimal useful shape would live under `src/mindroom/workers/lifecycle.py` and provide pure helpers for:

- `timestamp_or_now(now)`.
- `sort_worker_handles(handles, include_idle)`.
- `is_idle_by_last_used(status, last_used_at, now, idle_timeout_seconds)`.
- A small `WorkerLifecycleRecord` dataclass plus `worker_handle_from_record(...)` only if it can preserve backend-specific endpoint, auth token, and debug metadata cleanly.

The per-worker lock duplication could be handled by a tiny `WorkerLockRegistry` class in the same module or `src/mindroom/workers/locks.py`.
That refactor is low risk but should only be done if another worker backend change touches these files.

No refactor is recommended for Kubernetes progress reporting right now.
It is backend-specific and not duplicated by another producer.

## Risk/tests

Risks:

- Lifecycle helper extraction could subtly change timestamp ordering, idle transitions, failure count increments, or whether a missing worker creates metadata during `record_failure`.
- `WorkerHandle` helper extraction could over-parameterize backend-specific fields and make code less clear.
- Lock registry extraction must preserve current lock ownership and avoid holding the registry lock longer than today.

Tests needing attention if refactored:

- Backend tests for `ensure_worker`, `touch_worker`, `list_workers(include_idle=False)`, `evict_worker(preserve_state=True/False)`, `cleanup_idle_workers`, and `record_failure` for Kubernetes, local, and static backends.
- Kubernetes-specific tests around annotation parsing, deployment readiness, failed status, idle timeout, and startup failure cleanup.
- Progress reporter tests should remain Kubernetes-specific unless another backend starts producing warmup progress.
