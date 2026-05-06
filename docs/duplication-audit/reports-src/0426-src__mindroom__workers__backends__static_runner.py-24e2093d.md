## Summary

Top duplication candidates for `src/mindroom/workers/backends/static_runner.py`:

1. Static and local worker backends duplicate in-memory/metadata lifecycle bookkeeping for timestamps, idle detection, sorting, failure counters, and `WorkerHandle` projection.
2. Static and local API-root normalization both trim `/execute`, while static additionally appends `/api/sandbox-runner` for externally configured proxy roots.
3. Worker backend contract methods are intentionally mirrored across static, local, and Kubernetes backends; most of this is protocol-required similarity, not a refactor target by itself.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
normalize_static_runner_api_root	function	lines 17-26	duplicate-found	normalize sandbox runner api root; /execute; /api/sandbox-runner	src/mindroom/workers/backends/local.py:74; src/mindroom/workers/models.py:55; src/mindroom/workers/runtime.py:146
_StaticWorkerMetadata	class	lines 30-39	duplicate-found	worker metadata dataclass worker_id worker_key timestamps status failure_count	src/mindroom/workers/backends/local.py:43; src/mindroom/workers/models.py:21; src/mindroom/workers/backends/kubernetes_resources.py:ANNOTATION searches via src/mindroom/workers/backends/kubernetes.py:371
StaticSandboxRunnerBackend	class	lines 42-199	duplicate-found	WorkerBackend implementation ensure get touch list evict cleanup record_failure to_handle	src/mindroom/workers/backends/local.py:149; src/mindroom/workers/backends/kubernetes.py:246; src/mindroom/workers/backend.py:15
StaticSandboxRunnerBackend.__init__	method	lines 47-58	related-only	backend init api_root auth_token idle_timeout lock worker map	src/mindroom/workers/backends/local.py:154; src/mindroom/workers/backends/kubernetes.py:251
StaticSandboxRunnerBackend.ensure_worker	method	lines 60-97	duplicate-found	ensure_worker create metadata last_used status ready startup_count failure_reason	src/mindroom/workers/backends/local.py:168; src/mindroom/workers/backends/kubernetes.py:343; src/mindroom/workers/manager.py:29
StaticSandboxRunnerBackend.get_worker	method	lines 99-106	duplicate-found	get_worker timestamp lookup metadata return handle none	src/mindroom/workers/backends/local.py:206; src/mindroom/workers/backends/kubernetes.py:470; src/mindroom/workers/manager.py:39
StaticSandboxRunnerBackend.touch_worker	method	lines 108-116	duplicate-found	touch_worker update last_used_at return handle none	src/mindroom/workers/backends/local.py:216; src/mindroom/workers/backends/kubernetes.py:478; src/mindroom/tool_system/sandbox_proxy.py:480
StaticSandboxRunnerBackend.list_workers	method	lines 118-125	duplicate-found	list_workers include_idle filter status sort last_used_at	src/mindroom/workers/backends/local.py:229; src/mindroom/workers/backends/kubernetes.py:494; src/mindroom/api/workers.py:102
StaticSandboxRunnerBackend.evict_worker	method	lines 127-145	duplicate-found	evict_worker preserve_state idle last_used delete/pop return handle	src/mindroom/workers/backends/local.py:243; src/mindroom/workers/backends/kubernetes.py:504
StaticSandboxRunnerBackend.cleanup_idle_workers	method	lines 147-156	duplicate-found	cleanup_idle_workers ready effective idle mark idle sorted cleaned workers	src/mindroom/workers/backends/local.py:270; src/mindroom/workers/backends/kubernetes.py:533; src/mindroom/api/sandbox_runner.py:1302
StaticSandboxRunnerBackend.record_failure	method	lines 158-176	duplicate-found	record_failure failed last_used failure_count failure_reason default metadata	src/mindroom/workers/backends/local.py:287; src/mindroom/workers/backends/local.py:351; src/mindroom/workers/backends/kubernetes.py:551
StaticSandboxRunnerBackend._effective_status	method	lines 178-181	duplicate-found	effective_status ready idle_timeout last_used_at WorkerStatus	src/mindroom/workers/backends/local.py:346; src/mindroom/workers/backends/kubernetes.py:669
StaticSandboxRunnerBackend._to_handle	method	lines 183-199	duplicate-found	WorkerHandle construction metadata endpoint auth_token status debug_metadata api_root	src/mindroom/workers/backends/local.py:367; src/mindroom/workers/backends/kubernetes.py:632; src/mindroom/api/sandbox_worker_prep.py:168
```

## Findings

### 1. Static and local backends duplicate worker lifecycle bookkeeping

`StaticSandboxRunnerBackend` stores `_StaticWorkerMetadata` in memory and implements the same lifecycle operations that `_LocalWorkerBackend` stores in JSON metadata:

- `ensure_worker`: creates/defaults metadata, updates `last_used_at`, transitions to a ready/starting state, clears `failure_reason`, and increments startup count when reactivating an idle/non-ready worker (`src/mindroom/workers/backends/static_runner.py:60`, `src/mindroom/workers/backends/local.py:168`).
- `get_worker` and `touch_worker`: resolve one worker by key, return `None` if absent, and project metadata into `WorkerHandle` (`src/mindroom/workers/backends/static_runner.py:99`, `src/mindroom/workers/backends/local.py:206`, `src/mindroom/workers/backends/static_runner.py:108`, `src/mindroom/workers/backends/local.py:216`).
- `list_workers`: builds handles, optionally filters idle workers, and sorts by `last_used_at` descending (`src/mindroom/workers/backends/static_runner.py:118`, `src/mindroom/workers/backends/local.py:229`).
- `evict_worker`: with `preserve_state=True`, marks the worker idle, updates `last_used_at`, and returns a handle; with `False`, removes state and returns `None` (`src/mindroom/workers/backends/static_runner.py:127`, `src/mindroom/workers/backends/local.py:243`).
- `cleanup_idle_workers`: marks ready workers idle once `now - last_used_at >= idle_timeout_seconds` and returns cleaned handles (`src/mindroom/workers/backends/static_runner.py:147`, `src/mindroom/workers/backends/local.py:270`).
- `record_failure`: marks status failed, updates `last_used_at`, increments `failure_count`, stores `failure_reason`, and returns a handle (`src/mindroom/workers/backends/static_runner.py:158`, `src/mindroom/workers/backends/local.py:287`, `src/mindroom/workers/backends/local.py:351`).
- `_effective_status`: identical timeout condition for ready workers becoming idle (`src/mindroom/workers/backends/static_runner.py:178`, `src/mindroom/workers/backends/local.py:346`).

Differences to preserve:

- Static is an in-memory registry for a shared remote runner and validates `api_root`/`auth_token`.
- Local persists metadata to disk, creates worker directories/venvs, and uses per-worker locks.
- Static uses `status="ready"` on first creation; local uses `status="starting"` before provisioning, then `ready`.
- Local stores `endpoint` and `backend_name` in metadata; static derives endpoint/auth/backend from current backend config.

### 2. API-root normalization is repeated but not identical

`normalize_static_runner_api_root` trims whitespace/trailing slash, strips a trailing `/execute`, preserves an existing `/api/sandbox-runner`, and appends `/api/sandbox-runner` otherwise (`src/mindroom/workers/backends/static_runner.py:17`).
`_normalize_worker_api_root` in the local backend also trims and strips `/execute`, but defaults empty input to `/api/sandbox-runner` and does not append the API path to arbitrary roots (`src/mindroom/workers/backends/local.py:74`).
`worker_api_endpoint` separately derives API roots from `WorkerHandle.debug_metadata["api_root"]` or by removing `/execute` from the handle endpoint (`src/mindroom/workers/models.py:55`).

Differences to preserve:

- Static normalization treats empty input as empty so availability checks can reject missing proxy configuration.
- Local normalization treats empty input as the local app-relative default.
- Endpoint derivation in `worker_api_endpoint` is handle-operation routing, not configuration normalization.

### 3. WorkerHandle projection is repeated across backend-specific metadata sources

Static, local, Kubernetes, and dedicated-runner preparation all construct `WorkerHandle` with the same conceptual fields: worker identity, endpoint, auth token, status, backend name, timestamps, startup/failure counters, and `debug_metadata` with an API root (`src/mindroom/workers/backends/static_runner.py:183`, `src/mindroom/workers/backends/local.py:367`, `src/mindroom/workers/backends/kubernetes.py:632`, `src/mindroom/api/sandbox_worker_prep.py:168`).

Differences to preserve:

- Kubernetes must parse annotations, compute service host, derive worker auth token, and include Kubernetes debug metadata.
- Local includes `state_root`; static only includes `api_root`.
- Dedicated runner preparation emits an immediate ready handle for the in-process sandbox runner path.

## Proposed Generalization

1. Add a small backend-neutral helper in `src/mindroom/workers/models.py` or a focused `src/mindroom/workers/lifecycle.py` module for shared pure operations: `worker_effective_status(status, last_used_at, idle_timeout_seconds, now)`, `sort_worker_handles(handles)`, and optionally `filter_worker_handles(handles, include_idle)`.
2. Keep backend storage and IO in each backend; do not try to force static, local, and Kubernetes into one base class.
3. Consider a small API-root helper with explicit mode/default parameters only if another backend needs normalization, because static and local currently preserve different empty-input behavior.
4. Leave `WorkerHandle` construction backend-local unless future changes add a shared metadata dataclass; current differences make a generic factory likely to obscure backend-specific fields.

## Risk/tests

Behavior risks:

- Idle handling is externally visible through `/api/workers`, `/api/sandbox-runner/workers`, and sandbox proxy failure/touch paths.
- Static empty URL handling is configuration validation behavior; sharing normalization with local must not convert missing static proxy URLs into the app-relative default.
- Startup/failure counters differ subtly between static, local, and Kubernetes and should not be unified without preserving each backend's lifecycle semantics.

Tests that would need attention for any future refactor:

- Static runner tests around idle cleanup and failure recording in `tests/test_sandbox_proxy.py`.
- Local worker backend tests in `tests/api/test_sandbox_runner_api.py`.
- Kubernetes lifecycle tests in `tests/test_kubernetes_worker_backend.py`, especially cleanup/list/failure cases.
- API worker serialization tests in `tests/api/test_api.py` and `tests/api/test_sandbox_runner_api.py`.
