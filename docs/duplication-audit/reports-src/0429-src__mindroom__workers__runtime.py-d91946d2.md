## Summary

Top duplication candidates for `src/mindroom/workers/runtime.py`:

1. Stable JSON serialization plus SHA-256 hashing is repeated in worker runtime, Kubernetes resource hashing, and startup manifest hashing.
2. Signature-keyed module-level `WorkerManager` singleton caching is duplicated between the primary runtime backend and local worker backend.
3. Kubernetes-aware primary worker manager resolution is repeated at API cleanup, worker API, and sandbox proxy call sites.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_stable_json_digest	function	lines 31-34	duplicate-found	stable_json_digest sha256 json.dumps sort_keys separators	src/mindroom/workers/backends/kubernetes_resources.py:262; src/mindroom/constants.py:452; src/mindroom/constants.py:464; src/mindroom/workers/backends/kubernetes_config.py:206
_worker_validation_snapshot_cache_key	function	lines 37-53	related-only	worker validation snapshot cache_key plugins mcp model_dump	src/mindroom/workers/runtime.py:75; src/mindroom/orchestrator.py:818; src/mindroom/api/runtime_reload.py:100
clear_worker_validation_snapshot_cache	function	lines 56-59	related-only	clear cache lock validation snapshot	src/mindroom/tool_approval.py:158; src/mindroom/orchestrator.py:823; src/mindroom/orchestrator.py:1143; src/mindroom/api/runtime_reload.py:100
serialized_kubernetes_worker_validation_snapshot	function	lines 62-90	related-only	serialized_kubernetes_worker_validation_snapshot resolved_tool_validation_snapshot serialize_tool_validation_snapshot deepcopy	src/mindroom/api/main.py:140; src/mindroom/api/workers.py:85; src/mindroom/tool_system/sandbox_proxy.py:418
_normalize_backend_name	function	lines 93-100	related-only	MINDROOM_WORKER_BACKEND static_runner kubernetes Unsupported worker backend	src/mindroom/workers/backends/kubernetes_config.py:33; src/mindroom/tool_system/sandbox_proxy.py:316; src/mindroom/tool_system/sandbox_proxy.py:799
primary_worker_backend_name	function	lines 103-105	related-only	primary_worker_backend_name env_value MINDROOM_WORKER_BACKEND	src/mindroom/tool_system/sandbox_proxy.py:316; src/mindroom/api/main.py:136; src/mindroom/api/workers.py:86
primary_worker_backend_available	function	lines 108-126	related-only	primary_worker_backend_available proxy_url proxy_token kubernetes_backend_config_signature	src/mindroom/api/main.py:129; src/mindroom/api/workers.py:79; src/mindroom/tool_system/sandbox_proxy.py:803
_require_kubernetes_tool_validation_snapshot	function	lines 129-135	none-found	Kubernetes worker backend requires explicit tool validation snapshot	none
_resolve_worker_grantable_credentials	function	lines 138-143	duplicate-found	DEFAULT_WORKER_GRANTABLE_CREDENTIALS worker_grantable_credentials configured None	src/mindroom/config/main.py:1167; src/mindroom/api/main.py:145; src/mindroom/api/main.py:195; src/mindroom/tool_system/sandbox_proxy.py:430
_static_runner_backend_config_signature	function	lines 146-155	related-only	normalize_static_runner_api_root proxy_token static_runner config signature	src/mindroom/workers/backends/static_runner.py:17; src/mindroom/workers/backends/local.py:398
_primary_worker_backend_config_signature	function	lines 158-192	duplicate-found	backend config signature WorkerManager cache config tuple kubernetes_backend_config_signature	src/mindroom/workers/backends/local.py:398; src/mindroom/workers/backends/kubernetes_config.py:198
_build_primary_worker_manager	function	lines 195-232	related-only	WorkerManager StaticSandboxRunnerBackend KubernetesWorkerBackend.from_runtime	src/mindroom/workers/backends/local.py:405; src/mindroom/api/workers.py:91; src/mindroom/tool_system/sandbox_proxy.py:424
get_primary_worker_manager	function	lines 235-266	duplicate-found	get primary worker manager cache signature lock WorkerManager	src/mindroom/workers/backends/local.py:394; src/mindroom/api/main.py:147; src/mindroom/api/workers.py:91; src/mindroom/tool_system/sandbox_proxy.py:424
_reset_primary_worker_manager	function	lines 269-275	related-only	reset cached manager clear cache lock tests	src/mindroom/tool_approval.py:158; src/mindroom/workers/backends/local.py:389
```

## Findings

### Stable JSON hashing is repeated

`_stable_json_digest` in `src/mindroom/workers/runtime.py:31` serializes arbitrary JSON-like payloads with `sort_keys=True` and compact separators, then returns a SHA-256 hex digest.
The same canonical JSON plus SHA-256 pattern appears in `_template_hash` at `src/mindroom/workers/backends/kubernetes_resources.py:262` and in startup manifest hashing through `startup_manifest_json` / `startup_manifest_sha256` at `src/mindroom/constants.py:452` and `src/mindroom/constants.py:464`.
`kubernetes_backend_config_signature` also repeats compact sorted JSON serialization for several dict fields at `src/mindroom/workers/backends/kubernetes_config.py:206`.

Differences to preserve:
`_stable_json_digest` uses `default=repr` for non-standard objects.
`startup_manifest_json` hashes an already structured manifest and does not use `default=repr`.
`kubernetes_backend_config_signature` needs serialized JSON strings in the returned signature, not only a digest.

### Signature-keyed worker manager caching is duplicated

`get_primary_worker_manager` in `src/mindroom/workers/runtime.py:235` computes a config signature, takes a module-level lock, rebuilds a `WorkerManager` when the cached config changes, stores the new signature, and returns the cached manager.
`get_local_worker_manager` in `src/mindroom/workers/backends/local.py:394` has the same lifecycle shape with `_local_worker_manager`, `_local_worker_manager_config`, and `_local_worker_manager_lock`.

Differences to preserve:
The primary manager supports backend switching and Kubernetes validation snapshot data.
The local manager only uses local worker root, API root, and idle timeout.
The primary cache exposes `_reset_primary_worker_manager`; the local cache currently has no reset helper.

### Primary worker-manager resolution is repeated at call sites

`serialized_kubernetes_worker_validation_snapshot` plus `get_primary_worker_manager` wiring is repeated in `_cleanup_workers_once` at `src/mindroom/api/main.py:140`, `_worker_manager` at `src/mindroom/api/workers.py:85`, and `_get_worker_manager` at `src/mindroom/tool_system/sandbox_proxy.py:418`.
Each call site checks whether the primary backend is Kubernetes, builds a validation snapshot only for Kubernetes, resolves worker-grantable credentials from runtime config or context, and passes the same proxy and storage inputs into `get_primary_worker_manager`.

Differences to preserve:
`_cleanup_workers_once` intentionally returns early when Kubernetes is configured but no runtime config is available.
`_get_worker_manager` uses context storage when available.
`_worker_manager` raises HTTP 503 when no backend is configured.

### Worker grantable credential defaulting is repeated narrowly

`_resolve_worker_grantable_credentials` in `src/mindroom/workers/runtime.py:138` maps `None` to `DEFAULT_WORKER_GRANTABLE_CREDENTIALS`.
`Config.get_worker_grantable_credentials` in `src/mindroom/config/main.py:1167` performs the same defaulting for authored config.
Call sites also repeat the decision to derive credentials from runtime config only when available, for example `src/mindroom/api/main.py:145` and `src/mindroom/tool_system/sandbox_proxy.py:430`.

Differences to preserve:
The config method converts a configured list to `frozenset`.
The runtime helper receives an already normalized `frozenset[str] | None`.

## Proposed generalization

Consider a small utility for deterministic JSON serialization / hashing, probably near existing runtime constants or a focused serialization helper, only if more code starts needing the exact `default=repr` behavior.
For current code, a refactor is optional because some call sites need strings while others need digests.

The most useful minimal refactor would be a focused helper in `mindroom.workers.runtime`, such as `resolve_primary_worker_manager_for_config(...)`, that accepts `runtime_paths`, proxy config, storage root, and optional runtime config/context-derived credentials.
It would centralize the Kubernetes-only validation snapshot and credential defaulting while preserving call-site-specific availability checks and HTTP/cleanup behavior.

For manager caching, a generic singleton-cache helper is not recommended yet.
The duplicated pattern is real, but the primary and local caches have different reset and signature requirements, and abstracting mutable module globals plus locks would likely reduce readability.

## Risk/tests

If JSON hashing is generalized, tests should cover sorted keys, compact separators, non-JSON values requiring `repr`, and unchanged Kubernetes template hash values.
If primary worker-manager resolution is centralized, tests should cover static backend behavior, Kubernetes snapshot requirement, missing runtime config during cleanup, context storage override in sandbox proxy, and worker-grantable credential propagation.
Any change to manager caching should preserve lock behavior and manager rebuild when signatures change.
