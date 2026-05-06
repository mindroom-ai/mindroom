## Summary

Top duplication candidates:

1. `to_json_compatible` duplicates a narrower version of the tool-output JSON normalization in `src/mindroom/tool_system/output_files.py`.
2. The primary-side attachment save client and runner-side save route duplicate the same base64, size, SHA256, and receipt protocol in opposite directions.
3. Worker-request preparation and user-agent private visibility checks appear on both the proxy request builder and sandbox runner request preparation path.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
WorkerAttachmentSaveReceipt	class	lines 64-69	duplicate-found	worker attachment receipt worker_path size_bytes sha256 save attachment response	src/mindroom/api/sandbox_runner.py:357, src/mindroom/api/sandbox_runner.py:1454
SandboxProxyConfig	class	lines 73-83	none-found	SandboxProxyConfig sandbox proxy config env settings runner_mode proxy_tools credential_policy	none
_read_proxy_url	function	lines 86-90	related-only	MINDROOM_SANDBOX_PROXY_URL rstrip normalize sandbox runner URL	src/mindroom/workers/backends/static_runner.py:17, src/mindroom/workers/backends/local.py:74
_read_proxy_token	function	lines 93-97	related-only	MINDROOM_SANDBOX_PROXY_TOKEN token env strip runner token	src/mindroom/api/sandbox_runner.py:460, src/mindroom/workers/backends/static_runner.py:72
_read_proxy_timeout	function	lines 100-108	related-only	MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS env float timeout default ValueError	src/mindroom/workers/backends/local.py:62, src/mindroom/api/sandbox_exec.py:79, src/mindroom/orchestration/runtime.py:81
inline_attachment_byte_limit	function	lines 111-124	none-found	MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES inline attachment limit DEFAULT_INLINE_ATTACHMENT_BYTES	none
_read_execution_mode	function	lines 127-134	related-only	MINDROOM_SANDBOX_EXECUTION_MODE strip lower empty none	src/mindroom/api/sandbox_exec.py:69
_read_credential_lease_ttl	function	lines 137-146	related-only	MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS ttl clamp seconds default	src/mindroom/api/sandbox_worker_prep.py:313, src/mindroom/workers/backends/local.py:62
_read_proxy_tools	function	lines 149-156	related-only	MINDROOM_SANDBOX_PROXY_TOOLS split comma proxy tools selective	src/mindroom/oauth/providers.py:118, src/mindroom/tool_system/metadata.py:1173
_read_credential_policy	function	lines 159-182	none-found	MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON credential policy json selector services	none
sandbox_proxy_config	function	lines 185-197	none-found	sandbox_proxy_config SandboxProxyConfig aggregate env readers	none
to_json_compatible	function	lines 200-210	duplicate-found	JSON compatible normalize Path Mapping list tuple set tool payload	src/mindroom/tool_system/output_files.py:341
_credential_services_for_call	function	lines 213-226	none-found	credential policy selector star tool function services order	none
_filter_internal_credential_keys	function	lines 229-230	related-only	filter credential keys underscore internal credentials mapping	src/mindroom/credentials.py:486, src/mindroom/tool_system/metadata.py:516
_collect_credential_overrides	function	lines 233-263	related-only	load_scoped_credentials worker_target allowed_shared_services credential overrides	src/mindroom/tool_system/metadata.py:516, src/mindroom/api/tools.py:263, src/mindroom/api/sandbox_runner.py:543
_create_credential_lease	function	lines 266-300	related-only	credential lease request lease_id ttl max_uses proxy runner	src/mindroom/api/sandbox_runner.py:313, src/mindroom/api/sandbox_worker_prep.py:302
_build_worker_routing_payload	function	lines 303-385	related-only	worker_key worker_scope execution_identity private_agent_names ensure_worker routing payload	src/mindroom/api/sandbox_worker_prep.py:258, src/mindroom/api/sandbox_runner.py:1491
_resolve_user_agent_worker_payload	function	lines 388-407	duplicate-found	user_agent private_agent_names explicit visibility worker_key required	src/mindroom/api/sandbox_worker_prep.py:245
_get_worker_manager	function	lines 410-433	related-only	get_primary_worker_manager storage_root kubernetes validation snapshot grantable credentials	src/mindroom/api/main.py:146, src/mindroom/api/workers.py:97
_execution_env_payload	function	lines 436-453	related-only	execution_env shell python runtime env extra_env_passthrough	src/mindroom/api/sandbox_runner.py:1320, src/mindroom/api/sandbox_exec.py:69
_request_headers_for_handle	function	lines 456-465	related-only	sandbox token header worker_handle auth_token proxy_token	src/mindroom/api/sandbox_runner.py:460, src/mindroom/workers/backends/static_runner.py:72
_record_proxy_exception_for_worker	function	lines 468-482	related-only	record worker failure touch worker HTTP request-level failure	src/mindroom/api/sandbox_worker_prep.py:323, src/mindroom/workers/manager.py:43
_is_request_level_proxy_http_error	function	lines 485-501	none-found	HTTPStatusError status 400 404 422 detail Not Found request-level	none
_record_proxy_response_failure_for_worker	function	lines 504-519	related-only	failure_kind tool worker touch_worker record_failure response failure	src/mindroom/api/sandbox_runner.py:337, src/mindroom/api/sandbox_runner.py:365
attachment_save_uses_worker	function	lines 522-538	none-found	attachment save uses worker workspace consumer tools sandbox proxy enabled	none
_record_worker_save_failure	function	lines 541-550	related-only	record worker save failure record_failure worker_key	src/mindroom/api/sandbox_worker_prep.py:323
_validated_worker_save_receipt	function	lines 553-612	duplicate-found	save attachment response receipt validate worker_path size sha256 compare_digest	src/mindroom/api/sandbox_runner.py:357, src/mindroom/api/sandbox_runner.py:1454
save_attachment_to_worker	function	lines 615-717	duplicate-found	save attachment bytes base64 sha256 size worker workspace HTTP response failure_kind	src/mindroom/api/sandbox_runner.py:1361, src/mindroom/api/sandbox_runner.py:1377
_make_progress_sink	function	lines 720-737	none-found	WorkerProgressPump ProgressSink WorkerProgressEvent call_soon_threadsafe progress	none
_make_progress_sink.<locals>.sink	nested_function	lines 726-735	none-found	progress sink shutdown loop queue put_nowait worker progress event	none
_portable_tool_init_overrides	function	lines 740-767	none-found	portable tool init overrides base_dir relative shared storage root worker_key	none
_sandbox_proxy_enabled_for_tool	function	lines 770-812	related-only	sandbox proxy enabled tool execution_mode proxy_tools worker_tools_override backend available	src/mindroom/tool_system/metadata.py:563, src/mindroom/workspaces.py:305
_call_proxy_sync	function	lines 815-927	related-only	httpx client post execute lease payload ok result error failure_kind	src/mindroom/api/sandbox_runner.py:1478, src/mindroom/api/sandbox_runner.py:331
_wrap_sync_function	function	lines 930-965	related-only	model_copy entrypoint functools.wraps wrapper sync tool function	src/mindroom/tool_system/output_files.py:474
_wrap_sync_function.<locals>.proxy_entrypoint	nested_function	lines 948-962	related-only	proxy entrypoint args kwargs _call_proxy_sync wrapped entrypoint	src/mindroom/tool_system/output_files.py:486
_wrap_async_function	function	lines 968-1004	related-only	model_copy async entrypoint asyncio.to_thread wrapper tool function	src/mindroom/tool_system/output_files.py:504
_wrap_async_function.<locals>.proxy_entrypoint	nested_async_function	lines 986-1001	related-only	async proxy entrypoint asyncio.to_thread args kwargs	src/mindroom/tool_system/output_files.py:516
maybe_wrap_toolkit_for_sandbox_proxy	function	lines 1007-1072	related-only	wrap toolkit functions async_functions sandbox proxy output files wrapper	src/mindroom/tool_system/metadata.py:563, src/mindroom/tool_system/output_files.py:532
```

## Findings

### 1. JSON normalization is duplicated, with different strictness

`src/mindroom/tool_system/sandbox_proxy.py:200` defines `to_json_compatible` for proxy request payloads.
It recursively preserves JSON primitives, converts `Path` to `str`, converts mappings with string keys, converts lists, tuples, and sets to lists, and falls back to `str(value)`.

`src/mindroom/tool_system/output_files.py:341` defines `_normalize_json_value` for tool-output receipts.
It performs the same recursive JSON-normalization core, but also supports `Enum`, Pydantic `BaseModel`, dataclasses, and deterministic sorted sets, and raises `TypeError` for unsupported values instead of stringifying them.

The behavior is functionally duplicated around "make arbitrary tool-facing values JSON-safe", but the fallback semantics differ and must be preserved.

### 2. Attachment save protocol is duplicated across client and runner

`src/mindroom/tool_system/sandbox_proxy.py:553` validates a successful save-attachment response by checking `worker_path`, `size_bytes`, and `sha256`, then comparing the returned path, byte count, and digest.
`src/mindroom/tool_system/sandbox_proxy.py:615` computes SHA256, base64-encodes bytes, sends `size_bytes`, `mime_type`, `filename`, and `bytes_b64`, then interprets `ok`, `error`, and `failure_kind`.

The runner side defines the same contract in `src/mindroom/api/sandbox_runner.py:340` and `src/mindroom/api/sandbox_runner.py:357`.
It decodes and validates the mirrored byte fields in `src/mindroom/api/sandbox_runner.py:1361`, writes the output in `src/mindroom/api/sandbox_runner.py:1377`, and returns the same receipt fields at `src/mindroom/api/sandbox_runner.py:1454`.

This is real protocol duplication rather than accidental similarity.
The two sides must remain symmetric, including base64 validation, size checks, SHA256 comparison, path validation, and failure classification.

### 3. User-agent worker visibility validation appears in two places

`src/mindroom/tool_system/sandbox_proxy.py:388` requires `private_agent_names` and `worker_key` for `user_agent` worker routing before building the outbound payload.
`src/mindroom/api/sandbox_worker_prep.py:245` independently requires explicit private-agent visibility when resolving a `user_agent` worker key on the runner side.

The checks are intentionally on both sides of the boundary, but the rule is duplicated: a user-agent worker cannot be resolved without explicit private visibility.
The difference is error surface.
The proxy raises `RuntimeError` with a tool/function-oriented message, while the runner helper raises `ValueError` that becomes a request preparation error.

### 4. Worker request preparation flow is repeated between execute and save routes

`src/mindroom/api/sandbox_runner.py:1410` prepares a worker for save-attachment requests.
`src/mindroom/api/sandbox_runner.py:1491` prepares a worker for execute requests.
Both normalize/consume request worker context, call `sandbox_worker_prep.prepare_worker_request`, map `WorkerRequestPreparationError(failure_kind="worker")` to a structured response, and map request failures to HTTP 400.

This duplication is outside the primary file but directly pairs with `_build_worker_routing_payload` in `src/mindroom/tool_system/sandbox_proxy.py:303`, because both flows maintain the same worker-key, private-agent-name, and runner-token contract.
It is a smaller candidate than the save-attachment protocol because the response model differs between routes.

## Proposed Generalization

1. Extract a shared JSON-normalization helper with a strictness option, likely in `src/mindroom/tool_system/json_values.py`.
   Keep `to_json_compatible` as the permissive proxy-facing wrapper and make output receipts use strict mode.
2. Extract save-attachment protocol helpers or dataclasses near the runner API boundary, for example `src/mindroom/api/sandbox_attachment_protocol.py`.
   Candidate helpers: build request payload from bytes, decode and verify payload bytes, and validate receipt fields.
3. Extract a tiny user-agent worker visibility helper that accepts `worker_key`, `private_agent_names`, and message context, or leave this duplicated if preserving distinct error messages is clearer.
4. Consider a route-local helper in `sandbox_runner.py` for "prepare optional worker and map preparation errors to this response model" only if more worker-backed runner routes are added.

No broad architecture refactor is recommended.

## Risk/Tests

JSON normalization refactoring risks changing permissive stringification, set ordering, or support for dataclasses/Pydantic models.
Tests should cover primitives, `Path`, mappings, list/tuple/set, unknown objects, and output-file strict failures.

Save-attachment protocol refactoring risks breaking primary-to-worker compatibility.
Tests should cover valid payload round trip, invalid base64, size mismatch, digest mismatch, non-object responses, missing receipt fields, path mismatch, size mismatch, SHA mismatch, and `failure_kind` handling.

Worker visibility extraction risks changing user-facing error text and failure classification.
Tests should cover `user_agent` with missing private names on both proxy payload construction and runner request preparation.
