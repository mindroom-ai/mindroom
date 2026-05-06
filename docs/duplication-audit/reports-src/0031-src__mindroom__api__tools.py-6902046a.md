Summary: The only meaningful duplication candidate is the dashboard tool-availability credential context in `src/mindroom/api/tools.py`, which overlaps with the credential target resolution and loading flow in `src/mindroom/api/credentials.py`.
Most other symbols are endpoint DTOs or small annotations over exported tool dictionaries and have only related callers, not duplicated behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ToolsResponse	class	lines 44-48	not-a-behavior-symbol	ToolsResponse BaseModel tools status_authoritative	src/mindroom/api/tools.py:44; src/mindroom/api/credentials.py:103; src/mindroom/api/integrations.py:80
_ResolvedToolAvailabilityContext	class	lines 52-63	related-only	RequestCredentialsTarget DashboardAgentExecutionScopeResolution availability context credentials_manager worker_target allowed_shared_services	src/mindroom/api/tools.py:52; src/mindroom/api/credentials.py:103; src/mindroom/api/credentials.py:116
_effective_allowed_shared_services	function	lines 66-74	related-only	credential_service_policy uses_local_shared_credentials allowed_shared_services	src/mindroom/api/tools.py:66; src/mindroom/api/credentials.py:569; src/mindroom/credentials.py:375
_check_homeassistant_configured	function	lines 77-86	related-only	homeassistant instance_url access_token long_lived_token configured	src/mindroom/api/tools.py:77; src/mindroom/custom_tools/homeassistant.py:87; src/mindroom/api/homeassistant_integration.py:279
_check_standard_tool_configured	function	lines 89-99	none-found	config_fields required fields credentials status requires_config	src/mindroom/api/tools.py:89; src/mindroom/tool_system/metadata.py:700; src/mindroom/tool_system/metadata.py:1153
_check_auth_provider_configured	function	lines 102-118	related-only	oauth_credentials_usable oauth_provider_service_account_configured auth_provider configured	src/mindroom/api/tools.py:102; src/mindroom/oauth/service.py:199; src/mindroom/api/oauth.py:397
_append_config_only_presets	function	lines 121-145	none-found	TOOL_PRESETS config-only preset export tools dashboard	src/mindroom/api/tools.py:121; src/mindroom/config/main.py:1167; src/mindroom/tool_system/metadata.py:1153
_annotate_dashboard_configuration_support	function	lines 148-155	none-found	dashboard_configuration_supported annotate tools supported	src/mindroom/api/tools.py:148; src/mindroom/agent_policy.py:146; src/mindroom/agent_policy.py:173
_annotate_execution_scope_support	function	lines 158-168	related-only	unsupported_shared_only_integration_names execution_scope_supported tool names	src/mindroom/api/tools.py:158; src/mindroom/api/credentials.py:505; src/mindroom/tool_system/worker_routing.py:439
_load_shared_preview_credentials	function	lines 171-191	duplicate-found	load_worker_grantable_shared_credentials shared_manager load_credentials allowed_services Mapping	src/mindroom/api/tools.py:171; src/mindroom/api/credentials.py:554; src/mindroom/credentials.py:375
_resolve_tool_availability_context	function	lines 194-248	duplicate-found	resolve_dashboard_agent_execution_scope_request build_dashboard_execution_identity build_worker_target allowed_shared_services runtime config	src/mindroom/api/tools.py:194; src/mindroom/api/credentials.py:426; src/mindroom/api/oauth.py:387; src/mindroom/api/integrations.py:124
_read_tools_runtime_config	function	lines 251-253	related-only	read_committed_runtime_config bind_current_request_snapshot request runtime config	src/mindroom/api/tools.py:251; src/mindroom/api/credentials.py:459; src/mindroom/api/credentials.py:642
_update_tools_statuses	function	lines 256-303	duplicate-found	load scoped credentials cache auth provider status available requires_config	src/mindroom/api/tools.py:256; src/mindroom/api/credentials.py:540; src/mindroom/api/integrations.py:123
_update_tools_statuses.<locals>.get_credentials	nested_function	lines 263-279	duplicate-found	credentials cache load_scoped_credentials load shared preview allowed_shared_services	src/mindroom/api/tools.py:263; src/mindroom/api/credentials.py:540; src/mindroom/api/credentials.py:554; src/mindroom/credentials.py:412
get_registered_tools	async_function	lines 308-344	related-only	get registered tools export metadata resolve context annotate statuses response	src/mindroom/api/tools.py:308; src/mindroom/tool_system/metadata.py:1017; src/mindroom/tool_system/metadata.py:1153
```

Findings:

1. `src/mindroom/api/tools.py:194` duplicates part of dashboard credential target resolution from `src/mindroom/api/credentials.py:426`.
Both flows parse or receive the same dashboard execution-scope override, resolve it against agent config through `resolve_dashboard_agent_execution_scope_request`, build a dashboard execution identity, compute a worker target, and carry `config.get_worker_grantable_credentials()` into later credential loads.
The important difference is policy: `resolve_request_credentials_target()` rejects draft scope previews for credential writes, while `_resolve_tool_availability_context()` intentionally allows draft previews and can mark status as non-authoritative.

2. `src/mindroom/api/tools.py:171` and `src/mindroom/api/tools.py:263` partially duplicate credential-layer loading rules in `src/mindroom/api/credentials.py:540` and low-level merge helpers in `src/mindroom/credentials.py:375` / `src/mindroom/credentials.py:412`.
The tools route has its own per-service allowlist adjustment, authoritative `load_scoped_credentials()` path, non-authoritative shared-preview path, and request-local credential cache.
The credential API already centralizes target-based scoped/global/shared loading for dashboard requests, but it does not currently expose the tools route's non-authoritative preview behavior.

3. `src/mindroom/api/tools.py:77` repeats the Home Assistant runtime credential shape used by `src/mindroom/custom_tools/homeassistant.py:87`.
Both require an instance URL and either `access_token` or `long_lived_token`.
This is a small service-specific predicate, and the dashboard status check has a different purpose than runtime request execution, so it is related duplication but not worth extracting by itself.

Proposed generalization:

1. Add a small read-only dashboard credential availability helper in `src/mindroom/api/credentials.py`, next to `RequestCredentialsTarget`, that resolves a target with an `allow_draft_preview` flag and returns `{target, status_authoritative, dashboard_configuration_supported}`.
2. Keep `_ResolvedToolAvailabilityContext` only if the tools route still needs the OAuth provider maps and runtime metadata, or replace it with the new helper plus OAuth fields.
3. Move the tools route's authoritative/non-authoritative credential loading into a single helper in `api/credentials.py`, parameterized by `status_authoritative` and `allowed_shared_services`.
4. Leave Home Assistant's predicate local unless a second dashboard/service status path needs the same credential-shape check.

Risk/tests:

The main risk is collapsing draft preview behavior into the stricter credential-write path and accidentally turning non-authoritative tool status previews into credential management operations.
Tests should cover `/api/tools` with no agent, with a saved shared/user/user_agent agent scope, with a draft `execution_scope` query override, with worker-grantable shared credentials, and with OAuth provider credentials.
No refactor is recommended without those route-level tests because the current duplication encodes a real policy difference between preview status and credential writes.
