## Summary

Top duplication candidate: worker observability response models, worker serialization, list endpoint, and cleanup endpoint are duplicated between `src/mindroom/api/sandbox_runner.py` and `src/mindroom/api/workers.py`.
Other inspected similarities are mostly protocol counterparts or intentionally separate sides of a client/server boundary.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_startup_manifest_path_from_env	function	lines 80-85	related-only	startup manifest env path; sandbox startup manifest	src/mindroom/constants.py:479, src/mindroom/workers/backends/kubernetes_resources.py:829, src/mindroom/api/sandbox_runner_app.py:25
_startup_manifest_from_env	function	lines 88-93	related-only	startup manifest json load deserialize	src/mindroom/constants.py:431, src/mindroom/constants.py:548, src/mindroom/api/sandbox_runner.py:134
_startup_runtime_paths_from_env	function	lines 96-125	related-only	deserialize startup manifest resolve runtime paths dedicated worker	src/mindroom/constants.py:548, src/mindroom/constants.py:454, src/mindroom/workers/backends/kubernetes_resources.py:829
_startup_runner_token_from_env	function	lines 128-131	related-only	runner token env pop sandbox proxy token	src/mindroom/api/sandbox_runner_app.py:25, src/mindroom/tool_system/sandbox_proxy.py:194, src/mindroom/api/sandbox_worker_prep.py:149
_upstream_tool_validation_snapshot	function	lines 134-144	related-only	tool validation snapshot startup manifest storage root	src/mindroom/workers/runtime.py:120, src/mindroom/workers/backends/kubernetes_resources.py:829, src/mindroom/constants.py:548
_runtime_config_or_empty	function	lines 147-153	related-only	load config or empty runtime config	validate	src/mindroom/api/config_lifecycle.py:89, src/mindroom/config/main.py:453
_dedicated_worker_runtime_config_or_empty	function	lines 156-173	related-only	dedicated worker config available plugins yaml safe_load	src/mindroom/config/main.py:443, src/mindroom/workers/runtime.py:112
_config_with_available_plugins	function	lines 176-208	related-only	plugins resolve plugin root unavailable plugins	src/mindroom/tool_system/plugin_imports.py:166, src/mindroom/tool_system/plugins.py:52
_load_config_from_startup_runtime	function	lines 211-214	related-only	startup runtime config lifespan	load config	src/mindroom/api/sandbox_runner_app.py:22, src/mindroom/api/config_lifecycle.py:89
initialize_sandbox_runner_app	function	lines 217-232	related-only	fastapi app state context initialize runtime_paths config token	src/mindroom/api/main.py:123, src/mindroom/api/sandbox_runner_app.py:29
ensure_registry_loaded_with_config	function	lines 235-241	related-only	ensure tool registry loaded config wrapper	src/mindroom/tool_system/catalog.py:1021, src/mindroom/model_loading.py:0
_runner_credentials_manager	function	lines 244-246	related-only	runtime credentials manager wrapper	src/mindroom/credentials.py:652, src/mindroom/tool_system/sandbox_proxy.py:887
_request_private_agent_names	function	lines 249-253	related-only	private agent names frozenset request	src/mindroom/api/sandbox_worker_prep.py:230, src/mindroom/tool_system/sandbox_proxy.py:341
_request_runtime_overrides	function	lines 256-283	related-only	runtime overrides extra env passthrough shell resolved keys	src/mindroom/api/sandbox_worker_prep.py:220, src/mindroom/api/sandbox_exec.py:148
SandboxRunnerExecuteRequest	class	lines 286-310	related-only	sandbox execute payload lease worker execution env	src/mindroom/tool_system/sandbox_proxy.py:820, src/mindroom/api/sandbox_protocol.py:12
SandboxRunnerLeaseRequest	class	lines 313-320	related-only	credential lease request ttl max uses	src/mindroom/tool_system/sandbox_proxy.py:283, src/mindroom/api/sandbox_worker_prep.py:98
SandboxRunnerLeaseResponse	class	lines 323-328	related-only	lease_id expires_at max_uses	src/mindroom/tool_system/sandbox_proxy.py:299, src/mindroom/api/sandbox_worker_prep.py:42
SandboxRunnerExecuteResponse	class	lines 331-337	related-only	ok result error failure_kind sandbox execution response	src/mindroom/tool_system/sandbox_proxy.py:924, src/mindroom/api/sandbox_protocol.py:30
SandboxRunnerSaveAttachmentRequest	class	lines 340-354	related-only	save attachment bytes payload base64 sha256	src/mindroom/tool_system/sandbox_proxy.py:654, src/mindroom/custom_tools/attachments.py:560
SandboxRunnerSaveAttachmentResponse	class	lines 357-365	related-only	save attachment response ok worker path sha256	src/mindroom/tool_system/sandbox_proxy.py:565, src/mindroom/tool_system/sandbox_proxy.py:604
SandboxWorkerResponse	class	lines 368-383	duplicate-found	worker response model fields debug metadata idle worker	src/mindroom/api/workers.py:24, src/mindroom/workers/models.py:22
SandboxWorkerListResponse	class	lines 386-389	duplicate-found	worker list response workers list	src/mindroom/api/workers.py:42
SandboxWorkerCleanupResponse	class	lines 392-396	duplicate-found	worker cleanup response idle timeout cleaned workers	src/mindroom/api/workers.py:48
_SandboxRunnerContext	class	lines 400-404	none-found	sandbox runner context dataclass app state	none
_app_context	function	lines 407-415	related-only	fastapi app state context initialized type check	src/mindroom/api/sandbox_runner_app.py:18, src/mindroom/api/main.py:123
_app_runtime_paths	function	lines 418-419	related-only	app runtime paths accessor context	src/mindroom/api/config_lifecycle.py:89
_app_runtime_config	function	lines 422-423	related-only	app runtime config accessor context	src/mindroom/api/config_lifecycle.py:89
_app_tool_metadata	function	lines 426-427	none-found	app tool metadata accessor	none
_app_runner_token	function	lines 430-437	related-only	app runner token accessor type check	src/mindroom/api/sandbox_runner_app.py:28, src/mindroom/api/auth.py:580
sandbox_runner_runtime_paths	function	lines 440-442	related-only	fastapi dependency runtime paths request	app src/mindroom/api/config_lifecycle.py:89
sandbox_runner_runtime_config	function	lines 445-447	related-only	fastapi dependency runtime config request app	src/mindroom/api/config_lifecycle.py:89
sandbox_runner_tool_metadata	function	lines 450-452	none-found	fastapi dependency tool metadata snapshot	none
_validate_runner_token	async_function	lines 455-463	related-only	compare digest token header unauthorized src/mindroom/api/auth.py:400, src/mindroom/api/auth.py:580, src/mindroom/api/auth.py:613
_maybe_await	async_function	lines 473-476	related-only	inspect isawaitable maybe await	src/mindroom/interactive.py:133, src/mindroom/background_tasks.py:0
_run_toolkit_entrypoint	async_function	lines 479-492	related-only	toolkit requires_connect connect close entrypoint	src/mindroom/tool_system/metadata.py:0, src/mindroom/tool_system/sandbox_proxy.py:0
_runtime_paths_for_runner_agent_paths	function	lines 495-500	related-only	runner storage root replace runtime paths	src/mindroom/api/sandbox_exec.py:136, src/mindroom/runtime_resolution.py:0
_runner_tool_output_workspace_root	function	lines 503-527	related-only	resolve agent runtime tool base dir runtime_overrides base_dir	src/mindroom/agents.py:389, src/mindroom/custom_tools/attachments.py:499, src/mindroom/tool_system/metadata.py:549
_resolve_entrypoint	function	lines 530-575	related-only	get tool by name function entrypoint overrides	http exceptions	src/mindroom/tool_system/sandbox_proxy.py:875, src/mindroom/tool_system/metadata.py:590
_serialize_worker	function	lines 578-593	duplicate-found	serialize worker handle response fields	src/mindroom/api/workers.py:58, src/mindroom/workers/models.py:22
_workspace_env_overlay_for_request	function	lines 596-651	related-only	workspace env hook source overlay request	src/mindroom/api/sandbox_exec.py:344, src/mindroom/api/sandbox_exec.py:389
_request_workspace_home_root	function	lines 654-669	related-only	workspace home root execution tools request	src/mindroom/api/sandbox_runner.py:826, src/mindroom/tools/shell.py:130
_workspace_home_contract_env	function	lines 672-686	related-only	home mindroom agent workspace xdg config data state	src/mindroom/tools/shell.py:130, src/mindroom/api/sandbox_exec.py:246
_worker_owned_env	function	lines 689-699	related-only	worker owned env cache pip uv pycache venv	src/mindroom/api/sandbox_exec.py:246, src/mindroom/tools/shell.py:130
_existing_worker_runtime_env	function	lines 702-714	related-only	worker runtime env names preserve subprocess execution env	src/mindroom/constants.py:135, src/mindroom/tools/shell.py:130
_protected_execution_env_names	function	lines 717-727	related-only	protected env names workspace home worker runtime	src/mindroom/constants.py:121, src/mindroom/api/sandbox_exec.py:475
_trusted_workspace_overlay_for_runtime_paths	function	lines 730-737	related-only	filter overlay protected names runtime path reconstruction	src/mindroom/api/sandbox_exec.py:172, src/mindroom/api/sandbox_exec.py:475
_apply_workspace_home_contract_for_request	function	lines 740-759	related-only	apply workspace home contract execution env	src/mindroom/tools/shell.py:130
_protected_execution_env	function	lines 762-775	related-only	protected execution env worker owned existing runtime	src/mindroom/api/sandbox_exec.py:246, src/mindroom/tools/shell.py:130
_build_request_execution_env	function	lines 778-823	related-only	canonical order execution env workspace hook protected overlay	src/mindroom/api/sandbox_exec.py:172, src/mindroom/api/sandbox_exec.py:475, src/mindroom/tools/shell.py:147
_workspace_env_hook_workspace_for_request	function	lines 826-865	related-only	workspace env hook workspace routing agent base_dir	src/mindroom/api/sandbox_worker_prep.py:184, src/mindroom/api/sandbox_runner.py:503
_workspace_env_overlay_base_env	function	lines 868-886	related-only	base env subprocess execution env generic defaults	src/mindroom/api/sandbox_exec.py:232, src/mindroom/api/sandbox_exec.py:284
_uses_trusted_child_execution_env	function	lines 889-899	none-found	trusted child execution env apply workspace hook	none
_prepared_shell_execution_env	function	lines 902-917	related-only	prepared shell worker subprocess env extra env passthrough	src/mindroom/api/sandbox_exec.py:148, src/mindroom/api/sandbox_exec.py:246
_execute_request_inprocess	async_function	lines 920-1055	related-only	execute request inprocess resolve prepared worker entrypoint response	src/mindroom/tool_system/sandbox_proxy.py:875, src/mindroom/api/sandbox_runner.py:1101
_oauth_connection_required_result	function	lines 1058-1065	related-only	oauth connection required structured result provider connect url	src/mindroom/oauth/providers.py:26, src/mindroom/tool_system/sandbox_proxy.py:924
_subprocess_failure_response	function	lines 1068-1074	related-only	record worker failure response failure_kind worker	src/mindroom/api/sandbox_worker_prep.py:308, src/mindroom/tool_system/sandbox_proxy.py:700
_parse_subprocess_response	function	lines 1077-1098	related-only	extract response json completed process invalid response	src/mindroom/api/sandbox_protocol.py:30
_execute_request_subprocess_sync	function	lines 1101-1180	related-only	subprocess run envelope timeout execution env	src/mindroom/api/sandbox_exec.py:304, src/mindroom/api/sandbox_protocol.py:20, src/mindroom/api/sandbox_runner.py:920
_execute_request_subprocess	async_function	lines 1183-1200	related-only	asyncio to_thread subprocess sync wrapper	src/mindroom/background_tasks.py:0
_run_subprocess_worker	function	lines 1203-1269	related-only	stdin envelope response marker redirect stdout stderr	src/mindroom/api/sandbox_protocol.py:25
create_credential_lease	async_function	lines 1273-1288	related-only	credential lease endpoint create response	src/mindroom/api/sandbox_worker_prep.py:98, src/mindroom/tool_system/sandbox_proxy.py:266
list_workers	async_function	lines 1292-1299	duplicate-found	list workers endpoint serialize local manager	include idle	src/mindroom/api/workers.py:102
cleanup_idle_workers	async_function	lines 1303-1311	duplicate-found	cleanup idle workers endpoint serialize idle timeout	src/mindroom/api/workers.py:110
_validate_execute_request_payload	function	lines 1314-1347	related-only	validate credential overrides tool init config overrides execution env	src/mindroom/tool_system/catalog.py:908, src/mindroom/tool_system/sandbox_proxy.py:830
_save_attachment_output_path	function	lines 1350-1358	related-only	mindroom_output_path save_to_disk alias	src/mindroom/custom_tools/attachments.py:478
_decode_attachment_save_bytes	function	lines 1361-1374	related-only	base64 decode size sha256 compare digest attachment bytes	src/mindroom/tool_system/sandbox_proxy.py:654, src/mindroom/custom_tools/attachments.py:560
save_attachment_to_worker	async_function	lines 1378-1467	related-only	save attachment endpoint output path validate write bytes	src/mindroom/tool_system/sandbox_proxy.py:615, src/mindroom/custom_tools/attachments.py:527
execute_tool_call	async_function	lines 1471-1545	related-only	execute tool call endpoint lease prepare worker subprocess dispatch	src/mindroom/tool_system/sandbox_proxy.py:875, src/mindroom/api/sandbox_worker_prep.py:282
```

## Findings

### 1. Worker API response and endpoint behavior is duplicated

`src/mindroom/api/sandbox_runner.py:368` defines `SandboxWorkerResponse`, `SandboxWorkerListResponse`, and `SandboxWorkerCleanupResponse`.
`src/mindroom/api/workers.py:24` defines the same response shapes as `WorkerResponse`, `WorkerListResponse`, and `WorkerCleanupResponse`.
Both modules also contain an identical `WorkerHandle` field-by-field serializer at `src/mindroom/api/sandbox_runner.py:578` and `src/mindroom/api/workers.py:58`.
The list and cleanup endpoint bodies repeat the same list-comprehension serialization and `idle_timeout_seconds` response construction at `src/mindroom/api/sandbox_runner.py:1292` and `src/mindroom/api/workers.py:102`.

This is functionally the same observability behavior over different worker-manager sources.
The only meaningful difference to preserve is the route context: sandbox runner uses `get_local_worker_manager(runtime_paths)` from the runner app context, while primary API uses `_worker_manager(request)` to select and validate the configured primary backend.

## Proposed Generalization

Extract shared worker API DTOs and serialization into a small module such as `src/mindroom/api/worker_responses.py`.
That module could define `WorkerResponse`, `WorkerListResponse`, `WorkerCleanupResponse`, and `serialize_worker(worker: WorkerHandle)`.
Then `api/workers.py` and `api/sandbox_runner.py` can keep their own route functions and manager selection while sharing the response classes and serializer.

No broader refactor is recommended for the runner execution path.
The remaining related cases are paired client/server protocol logic, security-sensitive execution environment composition, or intentionally local validation at an API boundary.

## Risk/tests

Risk is low if the shared response models preserve field names and defaults exactly.
Tests should cover both `/api/workers` and `/api/sandbox-runner/workers` response schemas, plus cleanup responses.
Because these are FastAPI response models, schema snapshot or endpoint tests should confirm the exported JSON remains unchanged.
