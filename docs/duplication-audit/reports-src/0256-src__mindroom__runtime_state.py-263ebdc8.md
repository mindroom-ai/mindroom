## Summary

No meaningful duplication found.
`src/mindroom/runtime_state.py` is a small process-level readiness state holder used by orchestrator startup and API health/readiness endpoints.
Nearby modules such as `src/mindroom/matrix/health.py` and `src/mindroom/workers/models.py` use similar status vocabulary and lock-protected snapshots, but they track different domains and are not duplicated behavior worth generalizing.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_RuntimeState	class	lines 10-14	related-only	_RuntimeState dataclass phase detail status ready failed Lock snapshot	src/mindroom/matrix/health.py:19; src/mindroom/workers/models.py:41; src/mindroom/workers/backends/local.py:45; src/mindroom/runtime_support.py:64
get_runtime_state	function	lines 21-24	related-only	get_runtime_state runtime state snapshot ready endpoint health endpoint lock copy	src/mindroom/api/main.py:461; src/mindroom/api/main.py:484; src/mindroom/matrix/health.py:98
set_runtime_starting	function	lines 27-31	related-only	set_runtime_starting starting startup retry readiness detail	src/mindroom/orchestrator.py:928; src/mindroom/orchestrator.py:971; src/mindroom/orchestration/runtime.py:340; src/mindroom/orchestration/runtime.py:358; src/mindroom/workers/backends/local.py:181; src/mindroom/workers/backends/kubernetes.py:371
set_runtime_ready	function	lines 34-38	related-only	set_runtime_ready phase ready detail None worker ready matrix health ready	src/mindroom/orchestrator.py:1130; src/mindroom/api/main.py:487; src/mindroom/workers/backends/local.py:200; src/mindroom/workers/backends/kubernetes.py:441
set_runtime_failed	function	lines 41-45	related-only	set_runtime_failed phase failed detail failure_reason record failure	src/mindroom/orchestrator.py:950; src/mindroom/workers/backends/kubernetes.py:444; src/mindroom/workers/backends/kubernetes.py:559; src/mindroom/workers/backends/local.py:192
reset_runtime_state	function	lines 48-52	related-only	reset_runtime_state clear shared health reset lock idle	src/mindroom/orchestrator.py:2023; src/mindroom/matrix/health.py:155
```

## Findings

No real duplication found.

Related candidate: `src/mindroom/matrix/health.py:19` and `src/mindroom/matrix/health.py:98` also maintain module-level mutable state behind a `threading.Lock` and return an immutable-ish snapshot to API callers.
The behavior differs materially: Matrix sync health tracks per-entity loop state, stale timing, active entity aggregation, and an `is_healthy` derived property.
`runtime_state.py` tracks one global process readiness phase and optional detail for `/api/ready`.
Combining them would add abstraction without removing repeated domain logic.

Related candidate: `src/mindroom/workers/models.py:9`, `src/mindroom/workers/models.py:41`, `src/mindroom/workers/backends/local.py:181`, and `src/mindroom/workers/backends/kubernetes.py:441` use `starting`, `ready`, and `failed` states for sandbox workers.
This is shared lifecycle vocabulary, not duplicated implementation.
Worker status has per-worker handles, persistence, startup counts, failure counts, backend names, and progress events, while runtime readiness has only one process-level phase and detail string.

Related candidate: `src/mindroom/orchestration/runtime.py:340` and `src/mindroom/orchestration/runtime.py:358` update runtime startup detail during retries and Matrix homeserver waiting.
Those are call sites of `set_runtime_starting`, not duplicate setters.

## Proposed Generalization

No refactor recommended.
The current module is already the minimal shared helper for process readiness.
The only repeated local shape is the four setter functions assigning `_state.phase` and `_state.detail`, but abstracting them into a private generic setter would reduce clarity without reducing cross-module duplication.

## Risk/Tests

No production-code change is recommended.
If this module is changed later, tests should cover `/api/ready` responses for `idle`, `starting`, `ready`, and `failed`, plus `/api/health` behavior when runtime is ready and Matrix sync health is stale.
Existing relevant coverage appears in `tests/api/test_api.py:1014`, `tests/api/test_api.py:1024`, `tests/api/test_api.py:1036`, and stale Matrix sync health tests around `tests/api/test_api.py:879`.
