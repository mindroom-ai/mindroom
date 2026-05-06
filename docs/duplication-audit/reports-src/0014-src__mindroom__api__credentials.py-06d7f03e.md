# Duplication Audit: `src/mindroom/api/credentials.py`

## Summary

Top duplication candidates:

1. Dashboard scoped credential load/save/delete in `api/credentials.py` partially reimplements the policy dispatch already centralized in `credentials.py`.
2. Dashboard OAuth pending state handling mirrors conversation OAuth connect-token handling in `oauth/service.py`.
3. OAuth API target binding repeatedly converts `RequestCredentialsTarget` into a resolved worker target/payload in `api/oauth.py`, overlapping with `api/credentials.py` worker-target helpers.

Most endpoint request/response models, OAuth field guards, and FastAPI route functions are route-local policy and do not have meaningful duplicates under `src`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_PendingOAuthState	class	lines 64-74	related-only	OAuth state dataclasses pending/connect payload	src/mindroom/oauth/service.py:50
_filter_internal_keys	function	lines 77-79	related-only	internal credential metadata filtering _source _oauth	src/mindroom/credential_policy.py:111; src/mindroom/api/oauth.py:179
_filter_credentials_for_response	function	lines 82-87	related-only	OAuth credential response filtering looks_like_oauth_credentials filter_oauth_credential_fields	src/mindroom/credential_policy.py:101; src/mindroom/credential_policy.py:111
_filter_oauth_client_config_for_response	function	lines 90-93	none-found	OAuth client config response client_secret filtering	none
_validated_service	function	lines 96-100	related-only	validate_service_name HTTPException wrappers	src/mindroom/oauth/providers.py:275; src/mindroom/config/models.py:451
RequestCredentialsTarget	class	lines 104-113	related-only	credential target dataclass worker target runtime paths	src/mindroom/tool_system/worker_routing.py:240; src/mindroom/api/oauth.py:179
DashboardAgentExecutionScopeResolution	class	lines 117-125	related-only	dashboard execution scope resolution dataclass	src/mindroom/api/tools.py:194
OAuthCredentialServiceMatch	class	lines 129-135	related-only	OAuth provider credential service role matching	src/mindroom/oauth/providers.py:270
OAuthCredentialServices	class	lines 139-174	related-only	OAuth provider service classifier dashboard editable services	src/mindroom/oauth/providers.py:270; src/mindroom/credential_policy.py:72
OAuthCredentialServices.match	method	lines 144-157	related-only	provider credential_service tool_config_service client_config_services	src/mindroom/oauth/providers.py:270; src/mindroom/api/oauth.py:456
OAuthCredentialServices.reject_non_editable_services	method	lines 159-162	related-only	reject OAuth token service dashboard direct access	src/mindroom/api/oauth.py:485
OAuthCredentialServices.allows_private_scope_for	method	lines 164-169	related-only	OAuth client config private scope tool config editable	src/mindroom/api/oauth.py:390; src/mindroom/credential_policy.py:72
OAuthCredentialServices.dashboard_may_show_service	method	lines 171-174	related-only	dashboard may show OAuth service	src/mindroom/credential_policy.py:72
_request_auth_user	function	lines 177-179	related-only	request scope auth_user extraction	src/mindroom/api/auth.py:561; src/mindroom/api/auth.py:601
_require_auth_user_id	function	lines 182-187	related-only	require auth_user user_id 401	src/mindroom/api/auth.py:579; src/mindroom/api/auth.py:592
dashboard_requester_id_for_request	function	lines 190-200	related-only	trusted_upstream matrix_user_id owner env requester	src/mindroom/api/auth.py:274; src/mindroom/constants.py:1002
_reject_unbound_private_dashboard_requester	function	lines 203-217	none-found	private dashboard requester Matrix identity	none
issue_pending_oauth_state	function	lines 220-245	duplicate-found	issue_opaque_oauth_state OAuth state ttl kind payload	src/mindroom/oauth/service.py:61
_consume_pending_oauth_request	function	lines 248-277	duplicate-found	read consume opaque OAuth state validate payload	src/mindroom/oauth/service.py:87; src/mindroom/oauth/service.py:109; src/mindroom/oauth/service.py:119
consume_pending_oauth_request	function	lines 280-286	duplicate-found	consume pending oauth wrapper	src/mindroom/oauth/service.py:119
build_dashboard_execution_identity	function	lines 289-314	related-only	build ToolExecutionIdentity dashboard requester tenant account	src/mindroom/api/tools.py:216; src/mindroom/api/oauth.py:209
_dashboard_scope_label	function	lines 317-328	none-found	execution_scope label unscoped config label	none
resolve_dashboard_execution_scope_override	function	lines 331-345	related-only	parse execution_scope query shared user user_agent unscoped	src/mindroom/api/tools.py:324; src/mindroom/oauth/service.py:280
resolve_dashboard_agent_execution_scope_request	function	lines 348-411	related-only	resolve dashboard agent scope persisted draft override	src/mindroom/api/tools.py:204
_reject_raw_worker_targeting	function	lines 414-423	none-found	reject worker_key source_worker_key dashboard credentials	none
resolve_request_credentials_target	function	lines 426-537	duplicate-found	resolve dashboard credential target worker scope worker key manager	src/mindroom/api/tools.py:194; src/mindroom/api/oauth.py:117; src/mindroom/api/oauth.py:440
load_credentials_for_target	function	lines 540-566	duplicate-found	load scoped credentials shared worker merge policy	src/mindroom/credentials.py:486; src/mindroom/api/tools.py:263
_service_uses_primary_runtime_store	function	lines 569-575	related-only	credential_service_policy primary runtime local shared	src/mindroom/credentials.py:505; src/mindroom/credentials.py:558
_service_uses_primary_runtime_global_store	function	lines 578-579	related-only	credential_service_policy primary runtime global	src/mindroom/api/credentials.py:908
_worker_target_for_credentials_target	function	lines 582-589	duplicate-found	resolve_worker_target from credential target	src/mindroom/api/oauth.py:179; src/mindroom/api/oauth.py:194
_save_credentials_for_target	function	lines 592-604	duplicate-found	save scoped credentials policy dispatch	src/mindroom/credentials.py:544; src/mindroom/api/oauth.py:414
_primary_runtime_scoped_services_for_target	function	lines 607-621	related-only	list primary runtime scoped services for requester	src/mindroom/credentials.py:486; src/mindroom/api/credentials.py:908
_delete_credentials_for_target	function	lines 624-635	duplicate-found	delete scoped credentials policy dispatch	src/mindroom/credentials.py:579; src/mindroom/api/oauth.py:497
_request_may_target_scoped_credentials	function	lines 638-639	none-found	agent_name or execution_scope request may target scoped	none
_oauth_providers_for_request	function	lines 642-646	related-only	load oauth providers for request snapshot	src/mindroom/api/oauth.py:76
_oauth_services_for_request	function	lines 649-650	related-only	OAuthCredentialServices wrapper request providers	src/mindroom/api/oauth.py:76
_oauth_service_match	function	lines 653-654	related-only	OAuth service match request service	src/mindroom/api/oauth.py:456
_reject_oauth_token_service	function	lines 657-662	related-only	reject token credential service dashboard direct access	src/mindroom/api/oauth.py:485
_dashboard_may_edit_oauth_match	function	lines 665-673	related-only	dashboard_may_edit_oauth_service wrapper	src/mindroom/credential_policy.py:72
_is_oauth_client_config_match	function	lines 676-677	related-only	client_config_service match predicate	src/mindroom/oauth/providers.py:292
_is_oauth_client_config_service	function	lines 680-684	related-only	is_oauth_client_config_service wrapper	src/mindroom/credential_policy.py:88
_reject_oauth_credentials_document	function	lines 687-690	related-only	reject looks_like_oauth_credentials	src/mindroom/credential_policy.py:101; src/mindroom/oauth/providers.py:568
_reject_oauth_api_key_read_field	function	lines 693-718	none-found	OAuth API key route read field policy	none
_reject_oauth_api_key_write_field	function	lines 721-732	none-found	OAuth API key route write field policy	none
_reject_oauth_client_config_copy	function	lines 735-746	none-found	OAuth client config copy rejection	none
_dashboard_credentials_for_save	function	lines 749-758	related-only	UI source credential save marker strip OAuth fields	src/mindroom/credentials_sync.py:111; src/mindroom/api/oauth.py:404
_reject_non_client_config_fields	function	lines 761-769	none-found	OAuth client config allowed fields validation	none
_reject_invalid_client_config_field_values	function	lines 772-775	none-found	OAuth redirect_uri string validation	none
_require_or_preserve_oauth_client_config_field	function	lines 778-790	related-only	require or preserve OAuth client config field	src/mindroom/oauth/providers.py:348
_require_or_preserve_oauth_client_config_secret	function	lines 793-811	related-only	preserve client_secret require on client_id change	src/mindroom/oauth/providers.py:348
DashboardCredentialAccess	class	lines 815-927	related-only	dashboard credential access facade target oauth services	src/mindroom/api/tools.py:194; src/mindroom/api/oauth.py:117
DashboardCredentialAccess.resolve	method	lines 822-842	related-only	resolve dashboard access target oauth services	src/mindroom/api/tools.py:194; src/mindroom/api/oauth.py:117
DashboardCredentialAccess.match	method	lines 844-846	related-only	delegates OAuth service match	src/mindroom/api/oauth.py:456
DashboardCredentialAccess.reject_token_service	method	lines 848-850	related-only	delegates reject OAuth token service	src/mindroom/api/oauth.py:485
DashboardCredentialAccess.reject_stored_oauth_credentials	method	lines 852-854	related-only	delegates reject OAuth credentials document	src/mindroom/api/oauth.py:404
DashboardCredentialAccess.load	method	lines 856-859	duplicate-found	load credential through resolved target	src/mindroom/api/oauth.py:407; src/mindroom/credentials.py:486
DashboardCredentialAccess.save	method	lines 861-864	duplicate-found	save credential through resolved target	src/mindroom/api/oauth.py:414; src/mindroom/credentials.py:544
DashboardCredentialAccess.delete	method	lines 866-869	duplicate-found	delete credential through resolved target	src/mindroom/api/oauth.py:497; src/mindroom/credentials.py:579
DashboardCredentialAccess.response_credentials	method	lines 871-878	related-only	filter dashboard credentials response	src/mindroom/credential_policy.py:111
DashboardCredentialAccess.credentials_for_save	method	lines 880-895	related-only	normalize dashboard credentials for save client config preservation	src/mindroom/oauth/providers.py:348
DashboardCredentialAccess.list_services	method	lines 897-927	related-only	list scoped visible credential services shared worker primary runtime	src/mindroom/credentials.py:394; src/mindroom/api/tools.py:171
SetApiKeyRequest	class	lines 930-935	not-a-behavior-symbol	request schema	none
CredentialStatus	class	lines 938-943	not-a-behavior-symbol	response schema	none
SetCredentialsRequest	class	lines 946-949	not-a-behavior-symbol	request schema	none
list_services	async_function	lines 953-959	related-only	route delegates dashboard access list services	src/mindroom/api/tools.py:313
get_credential_status	async_function	lines 963-986	related-only	credential status route load/filter/key names	src/mindroom/api/oauth.py:435
set_credentials	async_function	lines 990-1011	related-only	set credential route validate save ui source	src/mindroom/api/homeassistant_integration.py:86; src/mindroom/api/integrations.py:114
set_api_key	async_function	lines 1015-1039	related-only	set single API key field route	src/mindroom/credentials.py:208; src/mindroom/credentials.py:222
get_api_key	async_function	lines 1043-1082	none-found	API key metadata masked value route	none
get_credentials	async_function	lines 1086-1107	related-only	get credential document route filter response	src/mindroom/api/homeassistant_integration.py:365
delete_credentials	async_function	lines 1111-1128	related-only	delete credential route resolved target	src/mindroom/api/oauth.py:485; src/mindroom/api/homeassistant_integration.py:365
copy_credentials	async_function	lines 1132-1165	related-only	copy credential stripping internal keys source ui	src/mindroom/credentials_sync.py:111
validate_credentials	async_function	lines 1169-1191	none-found	placeholder credentials exist validation route	none
```

## Findings

### 1. Scoped credential load/save/delete policy is duplicated around the primary runtime store

`src/mindroom/api/credentials.py:540`, `src/mindroom/api/credentials.py:592`, and `src/mindroom/api/credentials.py:624` implement dashboard-specific load/save/delete dispatch.
They choose between global base credentials, target worker credentials, primary-runtime scoped credentials, local shared credentials, and grantable shared credentials.
The same core storage policy lives in `src/mindroom/credentials.py:486`, `src/mindroom/credentials.py:544`, and `src/mindroom/credentials.py:579`.

The duplication is not literal because the dashboard API has an extra rule: services with `uses_primary_runtime_global_credentials` must read/write/delete through `target.base_manager`.
For non-global primary-runtime scoped services, the dashboard path and `credentials.py` path are functionally the same shape: resolve worker target, select scoped/shared manager by `credential_service_policy`, then merge or mutate credentials.

Differences to preserve:

- Dashboard routes must continue honoring `uses_primary_runtime_global_credentials`.
- Dashboard list responses include visibility filtering through `OAuthCredentialServices.dashboard_may_show_service`.
- Shared worker-grantable services must remain constrained by `allowed_shared_services`.

### 2. Pending dashboard OAuth state duplicates conversation OAuth connect-token lifecycle

`src/mindroom/api/credentials.py:220` issues a short-lived opaque OAuth state and `src/mindroom/api/credentials.py:248` reads, validates, consumes, and converts payload fields into a typed dataclass.
`src/mindroom/oauth/service.py:61`, `src/mindroom/oauth/service.py:87`, `src/mindroom/oauth/service.py:109`, and `src/mindroom/oauth/service.py:119` do the same lifecycle for conversation-issued OAuth connect tokens.

Both flows:

- Use a dataclass as the typed target.
- Serialize empty optional strings into payload fields.
- Bind provider/service, agent, worker scope/target data, requester identity, and TTL to an opaque token.
- Parse payload back to a dataclass and reject mismatches.

Differences to preserve:

- Dashboard pending state is bound to authenticated dashboard `user_id` and may include PKCE `code_verifier`.
- Conversation connect tokens are provider-bound and include `requester_id` plus `worker_key`.
- Dashboard code currently reads then consumes to check service/user before delete; `consume_opaque_oauth_state` consumes and returns in one call.

### 3. Worker target payload conversion is repeated in OAuth routes

`src/mindroom/api/credentials.py:582` resolves a `ResolvedWorkerTarget` from `RequestCredentialsTarget`.
`src/mindroom/api/oauth.py:179` performs the same resolution to build a target-binding payload, and `src/mindroom/api/oauth.py:194` repeats it for status/callback/disconnect credential operations.

This is duplicated behavior because all call sites derive the same worker target from the same three fields: `worker_scope`, `agent_name`, and `execution_identity`.

Differences to preserve:

- `_target_binding_payload` also serializes provider identity and credential service.
- Unscoped targets must return `None` for credential operations but serialize `"unscoped"` and an empty worker key for target-binding checks.

## Proposed Generalization

1. Add a small public helper in `mindroom.api.credentials`, for example `worker_target_for_request_credentials_target(target)`, replacing both `_worker_target_for_credentials_target` and `_resolved_worker_target_for_credentials` in `api/oauth.py`.
2. Consider moving the dashboard-aware primary-runtime-global branch into `credentials.py` as a narrowly named helper such as `load_dashboard_scoped_credentials`, only if more dashboard routes need it.
3. Extract a tiny OAuth state codec helper in `oauth/service.py` only if another OAuth state type is added; with two distinct flows, the current separation is acceptable.
4. Leave API-key masking and OAuth client-config validation local to `api/credentials.py`; they are route policy, not shared behavior.

No production refactor is recommended from this audit alone.
The only immediately low-risk cleanup candidate is sharing the `RequestCredentialsTarget` to `ResolvedWorkerTarget` conversion between `api/credentials.py` and `api/oauth.py`.

## Risk/tests

Primary risk for any refactor is credential scope regression: accidentally exposing worker-private credentials, hiding grantable shared credentials, or writing OAuth tokens to the wrong manager.
Tests should cover dashboard credential CRUD for unscoped, shared, user, and user_agent scopes; OAuth connect/callback target mismatch; primary-runtime-global credential services; and dashboard tool status for authoritative versus draft-scope previews.

No production code was edited.
