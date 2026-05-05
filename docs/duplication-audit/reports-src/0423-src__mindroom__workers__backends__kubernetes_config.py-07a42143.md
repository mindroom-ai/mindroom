Summary: The strongest duplication candidates are the runtime env readers in `src/mindroom/workers/backends/kubernetes_config.py`, which repeat float/int/bool/env-string parsing patterns already present in worker and runtime-support modules.
The Kubernetes config signature also repeats the stable JSON serialization used by worker-manager cache keys, though the tuple shape is backend-specific.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_read_env	function	lines 63-64	related-only	"env_value strip default runtime_env_values"	src/mindroom/constants.py:222, src/mindroom/api/sandbox_exec.py:67, src/mindroom/workers/backends/local.py:82
_read_float_env	function	lines 67-73	duplicate-found	"float env_value ValueError max(1.0 timeout)"	src/mindroom/workers/backends/local.py:62, src/mindroom/api/sandbox_exec.py:77, src/mindroom/api/main.py:111
_read_int_env	function	lines 76-82	duplicate-found	"int env_value ValueError max(1 ttl)"	src/mindroom/tool_system/sandbox_proxy.py:137, src/mindroom/api/sandbox_worker_prep.py:81
_read_bool_env	function	lines 85-89	duplicate-found	"env_flag true yes on lower strip"	src/mindroom/constants.py:234, src/mindroom/constants.py:787
_read_json_mapping_env	function	lines 92-110	related-only	"json loads env JSON dict clean strings"	src/mindroom/tool_system/sandbox_proxy.py:159, src/mindroom/credentials_sync.py:204
_KubernetesWorkerBackendConfig	class	lines 114-195	related-only	"worker backend config dataclass from_runtime env defaults"	src/mindroom/workers/backends/local.py:149, src/mindroom/workers/runtime.py:195, src/mindroom/workers/backends/kubernetes.py:24
_KubernetesWorkerBackendConfig.from_runtime	method	lines 143-195	related-only	"from_runtime backend config required env WorkerBackendError"	src/mindroom/workers/backends/kubernetes.py:223, src/mindroom/workers/runtime.py:212, src/mindroom/workers/backends/local.py:82
kubernetes_backend_config_signature	function	lines 198-239	duplicate-found	"json dumps sort_keys separators config signature tuple stable json digest"	src/mindroom/workers/runtime.py:31, src/mindroom/workers/runtime.py:146, src/mindroom/workers/runtime.py:158
```

## Findings

1. Runtime numeric env parsing is duplicated across worker/runtime modules.
`_read_float_env` parses an env value, falls back on `ValueError`, and clamps to `>= 1.0` in `src/mindroom/workers/backends/kubernetes_config.py:67`.
The same behavior appears in local worker idle-timeout parsing at `src/mindroom/workers/backends/local.py:62` and sandbox subprocess timeout parsing at `src/mindroom/api/sandbox_exec.py:77`.
`_read_int_env` similarly parses an integer with fallback and lower-bound clamp at `src/mindroom/workers/backends/kubernetes_config.py:76`; related bounded integer env parsing exists in `src/mindroom/tool_system/sandbox_proxy.py:137`, with an extra upper bound.
The differences to preserve are the source object (`Mapping[str, str]` versus `RuntimePaths`), the lower bound, and whether an upper bound is applied.

2. Boolean env flag parsing is duplicated with the central runtime helper.
`_read_bool_env` in `src/mindroom/workers/backends/kubernetes_config.py:85` uses the exact truthy set and default behavior used by `RuntimePaths.env_flag` at `src/mindroom/constants.py:234` and `runtime_env_flag` at `src/mindroom/constants.py:787`.
The Kubernetes helper works on a premerged `Mapping[str, str]`, while the central helpers work through `RuntimePaths.env_value`.

3. Stable JSON serialization for cache signatures is repeated.
`kubernetes_backend_config_signature` serializes config dict fields with `json.dumps(..., sort_keys=True, separators=(",", ":"))` in `src/mindroom/workers/backends/kubernetes_config.py:206`.
`_stable_json_digest` in `src/mindroom/workers/runtime.py:31` and `_primary_worker_backend_config_signature` in `src/mindroom/workers/runtime.py:158` use the same canonical JSON options for cache identity.
The difference to preserve is output form: Kubernetes needs raw serialized dict strings inside a public tuple signature, while `_stable_json_digest` hashes arbitrary JSON-like payloads.

## Proposed Generalization

Introduce a tiny runtime env parsing helper module only if another backend needs the same parsing soon.
A minimal shape would be `src/mindroom/runtime_env_parsing.py` with pure functions for stripped string lookup, bounded float, bounded int, bool flag, and canonical JSON serialization.
Keep adapters separate so callers can pass either `Mapping[str, str]` or `RuntimePaths.env_value`.
No refactor is recommended immediately for `_read_json_mapping_env` or `_KubernetesWorkerBackendConfig.from_runtime` because their current behavior is Kubernetes-specific and only loosely related to other JSON/env readers.

## Risk/tests

Refactoring numeric and boolean env parsing would need focused tests for missing values, blank values, malformed numbers, lower-bound clamping, and truthy/falsey strings.
Signature refactoring would need tests asserting exact tuple contents and canonical JSON ordering, because worker-manager cache invalidation depends on byte-for-byte stable identity.
No production code was edited for this audit.
