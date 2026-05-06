## Summary

Top duplication candidates for `src/mindroom/api/sandbox_worker_prep.py`:

- Lease TTL clamping is duplicated between `sandbox_worker_prep.bounded_ttl_seconds` and `sandbox_proxy._read_credential_lease_ttl`.
- The user-agent private visibility requirement is duplicated between `_explicit_private_agent_names` and `KubernetesWorkerResourceBuilder._scoped_storage_mounts`.

Most other symbols are request-preparation orchestration around worker state and only have related call sites, not meaningful duplicated behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
CredentialLease	class	lines 42-50	none-found	CredentialLease expires_at uses_remaining lease_id	src/mindroom/api/sandbox_runner.py:313; src/mindroom/tool_system/sandbox_proxy.py:266; src/mindroom/oauth/state.py:96
PreparedWorkerRequest	class	lines 60-65	none-found	PreparedWorkerRequest handle paths runtime_overrides	src/mindroom/api/sandbox_runner.py:256; src/mindroom/api/sandbox_runner.py:920; src/mindroom/api/sandbox_runner.py:1410
WorkerRequestPreparationError	class	lines 68-78	none-found	WorkerRequestPreparationError failure_kind request worker	src/mindroom/api/sandbox_runner.py:952; src/mindroom/api/sandbox_runner.py:1422; src/mindroom/api/sandbox_runner.py:1501
WorkerRequestPreparationError.__init__	method	lines 71-78	none-found	failure_kind Literal request worker custom exception	src/mindroom/tool_system/sandbox_proxy.py:470; src/mindroom/api/sandbox_runner.py:952
bounded_ttl_seconds	function	lines 81-83	duplicate-found	max min credential lease ttl seconds	src/mindroom/tool_system/sandbox_proxy.py:137
bounded_max_uses	function	lines 86-88	none-found	max uses credential lease clamp	src/mindroom/api/sandbox_runner.py:319; src/mindroom/tool_system/sandbox_proxy.py:287
cleanup_expired_leases	function	lines 91-95	related-only	expired leases expires_at pop cleanup	src/mindroom/oauth/state.py:96; src/mindroom/approval_manager.py:288
create_credential_lease	function	lines 98-119	related-only	create credential lease lease_payload token_urlsafe	src/mindroom/tool_system/sandbox_proxy.py:266; src/mindroom/api/sandbox_runner.py:1272
consume_credential_lease	function	lines 122-142	none-found	consume credential lease uses_remaining tool function match	src/mindroom/api/sandbox_runner.py:1484; src/mindroom/oauth/state.py:148
prepare_worker	function	lines 145-184	related-only	ensure_worker dedicated worker root WorkerHandle	src/mindroom/tool_system/sandbox_proxy.py:303; src/mindroom/tool_system/sandbox_proxy.py:370
normalize_request_worker_key	function	lines 187-194	none-found	normalize worker key dedicated worker omitted	src/mindroom/api/sandbox_runner.py:1386; src/mindroom/api/sandbox_runner.py:1480
resolve_worker_base_dir	function	lines 197-231	related-only	base_dir allowed roots visible_state_roots is_relative_to	src/mindroom/tool_system/output_files.py:336; src/mindroom/api/sandbox_exec.py:347; src/mindroom/workspaces.py:55
ready_runtime_overrides	function	lines 234-242	related-only	runtime_overrides base_dir mkdir parents	src/mindroom/api/sandbox_runner.py:256; src/mindroom/api/sandbox_runner.py:1149
_explicit_private_agent_names	function	lines 245-255	duplicate-found	user_agent workers require explicit private-agent visibility	src/mindroom/workers/backends/kubernetes_resources.py:984; src/mindroom/tool_system/sandbox_proxy.py:388
prepare_worker_request	function	lines 258-299	related-only	prepare worker request local_worker_state_paths base_dir WorkerRequestPreparationError	src/mindroom/api/sandbox_runner.py:1413; src/mindroom/api/sandbox_runner.py:1494; src/mindroom/api/sandbox_runner.py:944
resolve_prepared_worker_request	function	lines 302-320	none-found	reuse or prepare prepared_worker worker_key none	src/mindroom/api/sandbox_runner.py:944; src/mindroom/api/sandbox_runner.py:1117
record_worker_failure	function	lines 323-330	related-only	record worker failure runner dedicated worker manager	src/mindroom/tool_system/sandbox_proxy.py:470; src/mindroom/tool_system/sandbox_proxy.py:504; src/mindroom/tool_system/sandbox_proxy.py:541
```

## Findings

### 1. Lease TTL bounds are repeated

`src/mindroom/api/sandbox_worker_prep.py:81` clamps a requested lease TTL with `max(1, min(MAX_LEASE_TTL_SECONDS, raw_ttl_seconds))`.
`src/mindroom/tool_system/sandbox_proxy.py:137` reads the same credential lease TTL environment value and repeats the same clamp shape at line 146.

The duplicated behavior is the supported TTL range for sandbox credential leases.
The proxy side also parses and defaults the environment value, while `sandbox_worker_prep` clamps an already parsed request value.
Those differences should be preserved.

### 2. User-agent private visibility guard is repeated

`src/mindroom/api/sandbox_worker_prep.py:245` requires explicit private-agent visibility when the resolved worker key scope is `user_agent`.
`src/mindroom/workers/backends/kubernetes_resources.py:984` repeats the same `resolved_worker_key_scope(worker_key) == "user_agent" and private_agent_names is None` guard and raises a worker-backend error with the same message.
`src/mindroom/tool_system/sandbox_proxy.py:388` has related client-side validation for resolved worker targets, but it also validates that `worker_key` is present and includes tool/function context, so it is adjacent rather than a direct duplicate.

The duplicated behavior is the policy that user-agent worker storage visibility must be explicit before resolving visible state roots or mounts.
The exception types differ by layer and should stay layer-specific.

## Proposed Generalization

For the TTL clamp, a tiny shared helper or exported constant pair would be reasonable if both sides are edited together.
The lowest-friction location is `mindroom.tool_system.sandbox_proxy` only if the proxy remains the owner of the environment variable, but sharing from `sandbox_worker_prep` would create an API-to-tool-system import direction concern.
A better minimal option is a small neutral helper in `mindroom.api.sandbox_exec` or a focused `mindroom.api.sandbox_lease_policy` module containing only the max/default constants and clamp function.

For the user-agent private visibility guard, consider a helper in `mindroom.tool_system.worker_routing`, next to `resolved_worker_key_scope` and `visible_state_roots_for_worker_key`.
For example, a pure function that returns whether explicit private visibility is required, or a function returning the effective `frozenset`.
Keep exception construction at call sites so API request errors and backend errors remain distinct.

No broad refactor recommended.

## Risk/tests

The lease TTL helper would need tests covering invalid env parsing on the proxy side and request TTL clamping on the runner side.
The behavior risk is accidentally changing defaults or import layering.

The private visibility helper would need tests for `user_agent`, `user`, `shared`, and unscoped worker keys in both local request preparation and Kubernetes scoped mount construction.
The behavior risk is changing whether `None` and an empty `frozenset()` are distinct for `user_agent` workers.
