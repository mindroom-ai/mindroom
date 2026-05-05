# Summary

Top duplication candidate: worker API root and `/execute` endpoint construction is repeated across worker model helpers and backends.
Worker handle/spec/progress dataclasses are central contracts; related metadata shapes exist in backends and API response models, but they are storage/API boundary adapters rather than clear duplicated behavior.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
WorkerSpec	class	lines 14-18	related-only	WorkerSpec(, worker_key/private_agent_names, ResolvedWorkerTarget	private_agent_names: src/mindroom/tool_system/sandbox_proxy.py:339, src/mindroom/tool_system/sandbox_proxy.py:371, src/mindroom/tool_system/worker_routing.py:77
WorkerHandle	class	lines 22-38	related-only	WorkerHandle(, WorkerResponse, worker metadata, _to_handle	src/mindroom/workers/backends/local.py:43, src/mindroom/workers/backends/local.py:367, src/mindroom/workers/backends/static_runner.py:29, src/mindroom/workers/backends/static_runner.py:183, src/mindroom/workers/backends/kubernetes.py:632, src/mindroom/api/workers.py:24, src/mindroom/api/workers.py:58, src/mindroom/api/sandbox_worker_prep.py:168
WorkerReadyProgress	class	lines 42-49	none-found	WorkerReadyProgress(, ProgressSink, ready progress, cold_start/waiting/ready/failed	src/mindroom/workers/backends/kubernetes.py:55, src/mindroom/workers/backends/kubernetes.py:109, src/mindroom/tool_system/sandbox_proxy.py:720
worker_api_endpoint	function	lines 55-68	duplicate-found	api_root, /api/sandbox-runner/execute, removesuffix("/execute"), save-attachment, leases	src/mindroom/workers/backends/local.py:74, src/mindroom/workers/backends/local.py:308, src/mindroom/workers/backends/static_runner.py:17, src/mindroom/workers/backends/static_runner.py:187, src/mindroom/workers/backends/kubernetes.py:645, src/mindroom/workers/backends/kubernetes.py:649, src/mindroom/workers/backends/kubernetes.py:665, src/mindroom/api/sandbox_worker_prep.py:171, src/mindroom/api/sandbox_worker_prep.py:181
```

# Findings

## 1. Sandbox-runner API root and operation endpoint construction is duplicated

`worker_api_endpoint()` centralizes operation URL construction from a `WorkerHandle` in `src/mindroom/workers/models.py:55`.
It derives `api_root` from `debug_metadata["api_root"]` or from `handle.endpoint.removesuffix("/execute").rstrip("/")`, then returns the execute endpoint, cleanup endpoint, or a simple operation suffix.

The same root/execute relationship is repeated while handles are created:

- `src/mindroom/workers/backends/local.py:74` normalizes an API root and removes a trailing `/execute`.
- `src/mindroom/workers/backends/local.py:308` builds `endpoint=f"{self.api_root}/execute"`.
- `src/mindroom/workers/backends/local.py:383` stores `debug_metadata["api_root"]`.
- `src/mindroom/workers/backends/static_runner.py:17` normalizes a sandbox-runner API root and removes a trailing `/execute`.
- `src/mindroom/workers/backends/static_runner.py:187` builds `endpoint=f"{self.api_root}/execute"`.
- `src/mindroom/workers/backends/static_runner.py:198` stores `debug_metadata["api_root"]`.
- `src/mindroom/workers/backends/kubernetes.py:645` builds the service API root.
- `src/mindroom/workers/backends/kubernetes.py:649` builds `/api/sandbox-runner/execute`.
- `src/mindroom/workers/backends/kubernetes.py:665` stores matching `debug_metadata["api_root"]`.
- `src/mindroom/api/sandbox_worker_prep.py:171` and `src/mindroom/api/sandbox_worker_prep.py:181` repeat the dedicated runner execute/root pair.

These are functionally the same invariant: every worker handle carries both the execute endpoint and enough metadata to derive sibling sandbox-runner operation endpoints.
Differences to preserve: static runner normalization appends `/api/sandbox-runner` when a bare base URL is provided, while local runner defaults to a relative root and Kubernetes constructs a cluster service host.

## Related-only: Worker metadata adapters mirror `WorkerHandle`

`_LocalWorkerMetadata` in `src/mindroom/workers/backends/local.py:43`, `_StaticWorkerMetadata` in `src/mindroom/workers/backends/static_runner.py:29`, and `WorkerResponse` in `src/mindroom/api/workers.py:24` overlap with `WorkerHandle` fields.
The behavior is not clearly duplicated because the local/static metadata classes are persistence/backend state without auth/debug fields, and `WorkerResponse` is an API schema boundary.
The `_to_handle()` conversions in `src/mindroom/workers/backends/local.py:367`, `src/mindroom/workers/backends/static_runner.py:183`, and `src/mindroom/workers/backends/kubernetes.py:632` are related construction adapters but preserve backend-specific state sources.

# Proposed Generalization

Introduce a tiny worker URL helper in `src/mindroom/workers/models.py` or a sibling `src/mindroom/workers/urls.py`:

1. `worker_execute_endpoint(api_root: str) -> str` returns `f"{api_root.rstrip('/')}/execute"` with the existing relative-root behavior.
2. `worker_api_root_from_execute(endpoint: str) -> str` captures the current `removesuffix("/execute").rstrip("/")` fallback.
3. Optionally replace local/static/kubernetes/dedicated handle construction with the helper where they build execute endpoints.
4. Keep static runner's bare-base-url normalization separate because it has additional policy.

No refactor is recommended for `WorkerSpec`, `WorkerHandle`, or `WorkerReadyProgress` dataclasses themselves.

# Risk/tests

Main risk is changing URL normalization for relative roots, blank values, or bare static proxy base URLs.
Targeted tests should cover `worker_api_endpoint()` for execute, leases, workers, cleanup, and save-attachment; local/static normalization with and without trailing `/execute`; Kubernetes handle API root/execute pairing; and dedicated sandbox worker prep.
