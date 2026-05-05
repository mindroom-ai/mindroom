# Duplication Audit: `src/mindroom/api/sandbox_runner_app.py`

## Summary

No meaningful duplication found.
The module is a thin FastAPI entry point for the sandbox runner sidecar, and the nearest related behavior in `src/mindroom/api/main.py` has different lifecycle and health semantics.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_lifespan	async_function	lines 20-35	related-only	lifespan asynccontextmanager initialize_sandbox_runner_app startup_runtime app_runtime_paths	src/mindroom/api/main.py:346, src/mindroom/api/sandbox_runner.py:211, src/mindroom/api/sandbox_runner.py:217, tests/api/test_sandbox_runner_api.py:342
healthz	async_function	lines 43-45	related-only	healthz /healthz health status ok readiness liveness	src/mindroom/api/main.py:461, src/mindroom/api/main.py:484, src/mindroom/workers/backends/kubernetes_resources.py:715, tests/api/test_sandbox_runner_api.py:1662
```

## Findings

No real duplication was found for `_lifespan`.
`src/mindroom/api/sandbox_runner_app.py:20` reuses an already initialized sandbox runner context when tests or callers have bound one, and otherwise loads runtime context from the sandbox startup manifest/environment.
`src/mindroom/api/main.py:346` is a related FastAPI lifespan manager, but it initializes the dashboard API, loads and watches config, syncs credentials, manages knowledge watchers, and starts cleanup loops.
Those are lifecycle entry points with different responsibilities, not duplicated behavior.
The runtime loading and initialization operations are already centralized in `src/mindroom/api/sandbox_runner.py:211` and `src/mindroom/api/sandbox_runner.py:217`.

No real duplication was found for `healthz`.
`src/mindroom/api/sandbox_runner_app.py:43` exposes a minimal unauthenticated worker probe returning `{"status": "ok"}`.
`src/mindroom/api/main.py:461` and `src/mindroom/api/main.py:484` are related API health/readiness endpoints, but they include Matrix sync health and orchestrator runtime readiness semantics that should not be copied into the sandbox runner worker probe.
`src/mindroom/workers/backends/kubernetes_resources.py:715` references `/healthz` in Kubernetes probes, but it is a consumer of this endpoint rather than a duplicate implementation.

## Proposed Generalization

No refactor recommended.
The target module already delegates substantive runtime setup to `mindroom.api.sandbox_runner`, and extracting a shared FastAPI lifespan or health helper would add indirection without removing active duplicate behavior.

## Risk/Tests

Behavior risk is low if this module stays as-is.
Any future change to `_lifespan` should preserve the existing initialized-context path covered by `tests/api/test_sandbox_runner_api.py:342`, because that avoids reparsing broken disk config during lifespan startup.
Any future change to `healthz` should preserve the minimal unauthenticated response expected by `tests/api/test_sandbox_runner_api.py:1662` and the Kubernetes probe path in `src/mindroom/workers/backends/kubernetes_resources.py:715`.
