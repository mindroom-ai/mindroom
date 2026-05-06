## Summary

Top duplication candidates for `src/mindroom/workers/backends/local.py`:

1. Worker lifecycle bookkeeping is repeated across the local, static sandbox-runner, and Kubernetes backends.
2. Sandbox-runner API-root normalization is duplicated between local and static-runner backends, with different empty-input behavior that must be preserved.
3. Worker manager singleton caching in the local backend mirrors the primary worker manager cache in `src/mindroom/workers/runtime.py`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
LocalWorkerStatePaths	class	lines 31-40	related-only	local worker paths state_root metadata_file workspace venv_dir	src/mindroom/api/sandbox_worker_prep.py:161; src/mindroom/api/sandbox_worker_prep.py:197; src/mindroom/workers/backends/kubernetes.py:617
_LocalWorkerMetadata	class	lines 44-55	duplicate-found	worker metadata worker_id worker_key last_used_at startup_count failure_count status	src/mindroom/workers/backends/static_runner.py:29; src/mindroom/workers/backends/kubernetes_resources.py:233; src/mindroom/workers/backends/kubernetes.py:632
_default_worker_root	function	lines 58-59	none-found	storage_root workers default worker root	none
_read_idle_timeout_seconds	function	lines 62-71	duplicate-found	read idle timeout env float max 1 default	src/mindroom/workers/backends/kubernetes_config.py:67; src/mindroom/workers/backends/kubernetes_config.py:182
_normalize_worker_api_root	function	lines 74-79	duplicate-found	normalize sandbox runner api root removesuffix execute api/sandbox-runner	src/mindroom/workers/backends/static_runner.py:17; src/mindroom/workers/models.py:55
_read_worker_api_root	function	lines 82-84	related-only	read sandbox worker endpoint env normalize api root	src/mindroom/workers/runtime.py:146; src/mindroom/workers/runtime.py:158
_local_worker_state_paths_for_root	function	lines 87-99	related-only	metadata worker.json workspace cache venv path layout	src/mindroom/api/sandbox_worker_prep.py:161; src/mindroom/api/sandbox_worker_prep.py:197
local_worker_state_paths_for_root	function	lines 102-104	related-only	local worker paths public wrapper dedicated worker	src/mindroom/api/sandbox_worker_prep.py:161
_local_worker_state_paths	function	lines 107-110	related-only	worker_dir_name worker root path state paths	src/mindroom/workers/backends/kubernetes.py:617; src/mindroom/workers/backends/kubernetes_resources.py:174
local_worker_state_paths_from_handle	function	lines 113-119	related-only	handle debug_metadata state_root local paths	src/mindroom/workers/models.py:55; src/mindroom/api/sandbox_worker_prep.py:168
_ensure_local_worker_state	function	lines 122-131	related-only	create workspace cache metadata venv EnvBuilder	src/mindroom/api/sandbox_worker_prep.py:161; src/mindroom/api/sandbox_worker_prep.py:163
ensure_local_worker_state_locked	function	lines 134-137	related-only	shared per worker initialization lock ensure state	src/mindroom/api/sandbox_worker_prep.py:163
_shared_worker_initialization_lock	function	lines 140-146	duplicate-found	per key threading lock dictionary get create lock	src/mindroom/workers/backends/local.py:296; src/mindroom/workers/backends/kubernetes.py:606
_LocalWorkerBackend	class	lines 149-386	duplicate-found	worker backend ensure get touch list evict cleanup record failure handle	src/mindroom/workers/backends/static_runner.py:42; src/mindroom/workers/backends/kubernetes.py:246
_LocalWorkerBackend.__init__	method	lines 154-166	related-only	init api root idle timeout locks worker root	src/mindroom/workers/backends/static_runner.py:47; src/mindroom/workers/backends/kubernetes.py:251
_LocalWorkerBackend.ensure_worker	method	lines 168-204	duplicate-found	ensure worker metadata starting ready startup_count failure_reason	src/mindroom/workers/backends/static_runner.py:60; src/mindroom/workers/backends/kubernetes.py:343
_LocalWorkerBackend.get_worker	method	lines 206-214	duplicate-found	get worker load metadata return handle now timestamp	src/mindroom/workers/backends/static_runner.py:99; src/mindroom/workers/backends/kubernetes.py:470
_LocalWorkerBackend.touch_worker	method	lines 216-227	duplicate-found	touch worker last_used_at ready metadata handle	src/mindroom/workers/backends/static_runner.py:108; src/mindroom/workers/backends/kubernetes.py:478
_LocalWorkerBackend.list_workers	method	lines 229-241	duplicate-found	list workers include_idle sorted last_used_at reverse	src/mindroom/workers/backends/static_runner.py:118; src/mindroom/workers/backends/kubernetes.py:494
_LocalWorkerBackend.evict_worker	method	lines 243-268	duplicate-found	evict worker preserve_state idle delete state return handle	src/mindroom/workers/backends/static_runner.py:127; src/mindroom/workers/backends/kubernetes.py:504
_LocalWorkerBackend.cleanup_idle_workers	method	lines 270-285	duplicate-found	cleanup idle workers effective status ready idle sorted	src/mindroom/workers/backends/static_runner.py:147; src/mindroom/workers/backends/kubernetes.py:533
_LocalWorkerBackend.record_failure	method	lines 287-294	duplicate-found	record failure worker failure_count failure_reason failed handle	src/mindroom/workers/backends/static_runner.py:158; src/mindroom/workers/backends/kubernetes.py:551
_LocalWorkerBackend._worker_lock	method	lines 296-302	duplicate-found	per worker lock dict threading lock	src/mindroom/workers/backends/local.py:140; src/mindroom/workers/backends/kubernetes.py:606
_LocalWorkerBackend._default_metadata	method	lines 304-313	duplicate-found	default worker metadata worker_dir_name endpoint execute starting	src/mindroom/workers/backends/static_runner.py:80; src/mindroom/api/sandbox_worker_prep.py:168
_LocalWorkerBackend._ensure_worker_state	method	lines 315-316	related-only	wrapper ensure local worker state override seam	none
_LocalWorkerBackend._metadata_paths	method	lines 318-325	none-found	glob metadata worker.json local worker root	none
_LocalWorkerBackend._load_metadata	method	lines 327-339	related-only	json load dataclass metadata return none invalid	src/mindroom/workers/backends/kubernetes_resources.py:211; src/mindroom/workers/backends/kubernetes_resources.py:222
_LocalWorkerBackend._save_metadata	method	lines 341-344	related-only	json dump asdict metadata file sort_keys	src/mindroom/workers/backends/kubernetes_resources.py:233
_LocalWorkerBackend._effective_status	method	lines 346-349	duplicate-found	effective status ready idle timeout last_used_at	src/mindroom/workers/backends/static_runner.py:178; src/mindroom/workers/backends/kubernetes.py:669
_LocalWorkerBackend._record_failure_locked	method	lines 351-365	duplicate-found	record failure locked failed last_used failure_count failure_reason	src/mindroom/workers/backends/static_runner.py:158; src/mindroom/workers/backends/kubernetes.py:551
_LocalWorkerBackend._to_handle	method	lines 367-386	duplicate-found	WorkerHandle from backend metadata endpoint auth status debug_metadata	src/mindroom/workers/backends/static_runner.py:183; src/mindroom/workers/backends/kubernetes.py:632; src/mindroom/api/sandbox_worker_prep.py:168
get_local_worker_manager	function	lines 394-414	duplicate-found	cached worker manager config signature lock singleton	src/mindroom/workers/runtime.py:235; src/mindroom/workers/runtime.py:255
```

## Findings

### 1. Worker lifecycle bookkeeping is repeated across backends

`_LocalWorkerBackend` repeats the same backend contract implementation shape as `StaticSandboxRunnerBackend` and parts of `KubernetesWorkerBackend`: resolve `now`, load current metadata, update `last_used_at`, move ready workers to idle after `idle_timeout_seconds`, increment startup/failure counters, return handles sorted by `last_used_at`, and convert backend metadata into `WorkerHandle`.

Representative local references:

- `src/mindroom/workers/backends/local.py:168` starts or reuses a worker and updates status, startup count, failure reason, and last-used timestamps.
- `src/mindroom/workers/backends/local.py:229` lists handles and sorts by last-used descending.
- `src/mindroom/workers/backends/local.py:270` marks timed-out ready workers idle.
- `src/mindroom/workers/backends/local.py:351` records failed metadata.
- `src/mindroom/workers/backends/local.py:367` maps persisted metadata to `WorkerHandle`.

Matching behavior elsewhere:

- `src/mindroom/workers/backends/static_runner.py:60` performs the same ready/idle/startup/last-used lifecycle in memory.
- `src/mindroom/workers/backends/static_runner.py:118` applies the same include-idle filtering and descending last-used sort.
- `src/mindroom/workers/backends/static_runner.py:147` marks timed-out ready workers idle.
- `src/mindroom/workers/backends/static_runner.py:158` increments failure count and failure reason.
- `src/mindroom/workers/backends/static_runner.py:183` maps metadata to `WorkerHandle`.
- `src/mindroom/workers/backends/kubernetes.py:494` applies the same list filtering and sorting.
- `src/mindroom/workers/backends/kubernetes.py:551` records failed state with failure count/reason.
- `src/mindroom/workers/backends/kubernetes.py:632` maps persisted annotations to `WorkerHandle`.
- `src/mindroom/workers/backends/kubernetes.py:669` derives idle/ready effective status from last-used time and backend readiness.

Differences to preserve:

- Local persists JSON metadata and creates a venv on disk.
- Static backend is in-memory and requires configured API root/token before use.
- Kubernetes stores metadata in annotations, scales deployments, deletes service/secret resources, and must reflect deployment readiness.
- Kubernetes returns unsorted `cleanup_idle_workers` results at `src/mindroom/workers/backends/kubernetes.py:533`, while local/static sort cleaned workers descending by last-used.

### 2. Sandbox-runner API root normalization is duplicated

`src/mindroom/workers/backends/local.py:74` strips whitespace, trims trailing slashes, removes `/execute`, and falls back to `/api/sandbox-runner`.

`src/mindroom/workers/backends/static_runner.py:17` strips whitespace/trailing slashes, removes `/execute`, preserves a value already ending in `/api/sandbox-runner`, and otherwise appends `/api/sandbox-runner`.

The behavior is similar but not identical:

- Local treats blank input as the default relative API root.
- Static treats blank input as blank so `ensure_worker` can raise a proxy URL configuration error.
- Static accepts a host root and appends `/api/sandbox-runner`; local expects the worker API root path directly.

`src/mindroom/workers/models.py:55` also has fallback root derivation from `handle.endpoint.removesuffix("/execute").rstrip("/")`, which is the same suffix-stripping operation in a handle-centric context.

### 3. Numeric timeout parsing is duplicated

`src/mindroom/workers/backends/local.py:62` reads an environment value, parses it as `float`, falls back on invalid input, and clamps to at least `1.0`.

`src/mindroom/workers/backends/kubernetes_config.py:67` performs the same parse/fallback/clamp operation for Kubernetes timeout values, and it is used for both idle and ready timeouts at `src/mindroom/workers/backends/kubernetes_config.py:182`.

Differences to preserve:

- Local reads through `RuntimePaths.env_value`.
- Kubernetes config reads from a concrete `Mapping[str, str]` after `runtime_env_values`.
- Local and Kubernetes use different environment variable names.

### 4. Per-worker lock registry logic is repeated

`src/mindroom/workers/backends/local.py:140`, `src/mindroom/workers/backends/local.py:296`, and `src/mindroom/workers/backends/kubernetes.py:606` all implement the same pattern: guard a dictionary with a lock, fetch a worker-specific `threading.Lock`, create and store it if absent, and return it.

Differences to preserve:

- Local has both a module-level shared lock registry for venv initialization and an instance-level lock registry.
- Kubernetes has an instance-level registry only.

### 5. Worker manager singleton caching is repeated

`src/mindroom/workers/backends/local.py:394` caches a `WorkerManager` behind a module-level lock and rebuilds when a config tuple changes.

`src/mindroom/workers/runtime.py:235` does the same for the primary worker manager, using a richer config signature and backend factory.

Differences to preserve:

- Local config is only `(worker_root, api_root, idle_timeout_seconds)`.
- Primary worker manager supports multiple backend types, storage roots, auth tokens, validation snapshots, and worker credential allowlists.

## Proposed Generalization

Minimal helpers would be useful, but a broad worker-backend base class is not recommended.

Recommended small extractions:

1. Add a tiny helper in `src/mindroom/workers/models.py` or a new focused `src/mindroom/workers/lifecycle.py` for `effective_idle_status(status, last_used_at, now, idle_timeout_seconds)`.
2. Add `sorted_worker_handles(handles, include_idle=True)` in the same worker lifecycle helper to centralize include-idle filtering and descending last-used sorting.
3. Add a small `WorkerLockRegistry` dataclass in `src/mindroom/workers/lifecycle.py` for the repeated per-key lock dictionary.
4. Add `read_positive_float_env` in a worker config helper only if another worker backend starts reading timeout-like env values.
5. Leave API-root normalization separate unless the call sites are parameterized explicitly for blank-input behavior and host-root appending.

No production refactor is included because this task requested a report only and no production code edits.

## Risk/tests

Behavior risks if refactored:

- Local/static/Kubernetes idle transitions are similar but not identical around readiness, replicas, and preserved state.
- Static blank API-root behavior is a configuration validation path and should not be collapsed into local's defaulting behavior.
- Kubernetes cleanup currently returns workers in resource-list order while local/static sort; changing that would be a behavioral change.
- Lock-registry extraction must preserve the exact lock acquisition order to avoid introducing deadlocks.

Tests needing attention for any future refactor:

- Unit tests for local, static, and Kubernetes effective status at boundary times exactly below, equal to, and above idle timeout.
- Unit tests for `list_workers(include_idle=False)` and ordering across all backends.
- Unit tests for `record_failure` preserving/incrementing counts and failure reasons.
- Unit tests for API-root normalization with blank input, host root, `/api/sandbox-runner`, trailing slash, and `/execute`.
- Concurrency tests or focused lock-registry tests ensuring the same key returns the same lock and different keys can use different locks.
