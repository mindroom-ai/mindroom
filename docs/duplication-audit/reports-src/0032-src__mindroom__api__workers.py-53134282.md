Summary: The primary-runtime worker observability API in `src/mindroom/api/workers.py` duplicates the sandbox-runner worker observability API in `src/mindroom/api/sandbox_runner.py`.
The duplicated surface includes the worker response model shape, list/cleanup wrapper models, `WorkerHandle` serialization, and thin list/cleanup endpoint flow.
The primary-runtime `_worker_manager` helper also repeats most of the primary worker-manager construction flow used by background cleanup and sandbox proxy dispatch, but those call sites have enough contextual differences that this is related duplication rather than a direct extraction target.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
WorkerResponse	class	lines 24-39	duplicate-found	WorkerResponse SandboxWorkerResponse WorkerHandle fields worker_id worker_key endpoint status backend_name last_used_at created_at	src/mindroom/api/sandbox_runner.py:368, src/mindroom/workers/models.py:21
WorkerListResponse	class	lines 42-45	duplicate-found	WorkerListResponse SandboxWorkerListResponse workers list response_model	src/mindroom/api/sandbox_runner.py:386
WorkerCleanupResponse	class	lines 48-52	duplicate-found	WorkerCleanupResponse SandboxWorkerCleanupResponse idle_timeout_seconds cleaned_workers	src/mindroom/api/sandbox_runner.py:392
_serialize_worker	function	lines 58-73	duplicate-found	_serialize_worker WorkerHandle WorkerResponse SandboxWorkerResponse debug_metadata failure_reason startup_count	src/mindroom/api/sandbox_runner.py:578, src/mindroom/workers/models.py:21
_worker_manager	function	lines 76-98	related-only	get_primary_worker_manager primary_worker_backend_available serialized_kubernetes_worker_validation_snapshot sandbox_proxy_config	src/mindroom/api/main.py:121, src/mindroom/tool_system/sandbox_proxy.py:410
list_workers	async_function	lines 102-106	duplicate-found	list_workers include_idle worker_manager.list_workers response_model workers endpoint	src/mindroom/api/sandbox_runner.py:1291, src/mindroom/workers/manager.py:47
cleanup_idle_workers	async_function	lines 110-117	duplicate-found	cleanup_idle_workers idle_timeout_seconds worker_manager.cleanup_idle_workers response_model cleaned_workers	src/mindroom/api/sandbox_runner.py:1302, src/mindroom/api/main.py:121, src/mindroom/workers/manager.py:61
```

Findings:

1. Worker API response schemas are duplicated between primary-runtime and sandbox-runner endpoints.
   `src/mindroom/api/workers.py:24` defines `WorkerResponse` with the same public fields and defaults as `SandboxWorkerResponse` at `src/mindroom/api/sandbox_runner.py:368`.
   `WorkerListResponse` at `src/mindroom/api/workers.py:42` mirrors `SandboxWorkerListResponse` at `src/mindroom/api/sandbox_runner.py:386`.
   `WorkerCleanupResponse` at `src/mindroom/api/workers.py:48` mirrors `SandboxWorkerCleanupResponse` at `src/mindroom/api/sandbox_runner.py:392`.
   The only meaningful difference is nominal typing and docstrings, because the sandbox-runner models point at `SandboxWorkerResponse` while the primary-runtime models point at `WorkerResponse`.

2. Worker handle serialization is duplicated.
   `_serialize_worker` in `src/mindroom/api/workers.py:58` copies every non-secret observability field from `WorkerHandle` into `WorkerResponse`.
   `_serialize_worker` in `src/mindroom/api/sandbox_runner.py:578` performs the same mapping into `SandboxWorkerResponse`.
   Both intentionally omit `WorkerHandle.auth_token` from `src/mindroom/workers/models.py:28`, and both expose `debug_metadata`, so a shared serializer would need to preserve that exact omission.

3. Worker list and cleanup endpoint bodies are duplicated with different manager lookup.
   `list_workers` in `src/mindroom/api/workers.py:102` resolves a manager, calls `list_workers(include_idle=include_idle)`, serializes each handle, and wraps the list.
   `list_workers` in `src/mindroom/api/sandbox_runner.py:1292` performs the same endpoint flow after resolving the local sandbox-runner manager.
   `cleanup_idle_workers` in `src/mindroom/api/workers.py:110` and `src/mindroom/api/sandbox_runner.py:1303` both resolve a manager, call `cleanup_idle_workers()`, serialize the handles, and return `idle_timeout_seconds`.
   The differences to preserve are route path, response class names if public OpenAPI compatibility matters, docstrings, and manager resolution.

4. Primary worker-manager construction is similar but not identical across runtime paths.
   `_worker_manager` in `src/mindroom/api/workers.py:76` reads committed runtime config from a request, checks backend availability, builds Kubernetes validation metadata when needed, and returns `get_primary_worker_manager(...)`.
   `_cleanup_workers_once` in `src/mindroom/api/main.py:121` repeats the proxy config, backend availability, Kubernetes snapshot, grantable credentials, and manager construction flow, but returns `0` rather than raising `HTTPException` when the backend is unavailable and deliberately skips Kubernetes cleanup without runtime config.
   `_get_worker_manager` in `src/mindroom/tool_system/sandbox_proxy.py:410` repeats Kubernetes snapshot and grantable credentials construction from tool runtime context, but it uses a context-dependent storage root and does not perform availability checks.
   This is related duplication, but a shared helper would need careful parameters for error policy, config availability, and storage-root source.

Proposed generalization:

1. Add a small shared API model/serializer module such as `src/mindroom/api/worker_responses.py`.
2. Move the common response schemas there as `ApiWorkerResponse`, `ApiWorkerListResponse`, and `ApiWorkerCleanupResponse`, plus `serialize_worker_handle(worker: WorkerHandle) -> ApiWorkerResponse`.
3. Have both primary-runtime and sandbox-runner worker endpoints import the shared models and serializer, or subclass/alias the shared models only if preserving distinct OpenAPI schema names is required.
4. Optionally add a narrowly scoped helper for endpoint bodies, for example `worker_list_response(manager, include_idle)` and `worker_cleanup_response(manager)`, only if the response model names are unified.
5. Leave primary worker-manager construction alone unless a later change touches those paths; the related duplication has different error and context semantics.

Risk/tests:

Changing response model class names can alter generated OpenAPI schema names, so tests should verify `/api/workers`, `/api/workers/cleanup`, `/workers`, and `/workers/cleanup` response bodies and OpenAPI output if schema names are treated as API.
The serializer must continue omitting `auth_token` while preserving all current observability fields, especially `debug_metadata`, `failure_reason`, and optional lifecycle timestamps.
Manager construction refactoring would carry higher behavioral risk because backend-unavailable behavior differs between API endpoints, background cleanup, and tool execution.
