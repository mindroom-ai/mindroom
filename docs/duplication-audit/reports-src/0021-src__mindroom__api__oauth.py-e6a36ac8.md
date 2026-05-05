## Summary

Top duplication candidates in `src/mindroom/api/oauth.py`:

1. `_resolved_worker_target_for_credentials()` duplicates `src/mindroom/api/credentials.py`'s private `_worker_target_for_credentials_target()` helper exactly in behavior.
2. `_target_binding_payload()` duplicates most of `src/mindroom/oauth/service.py`'s `oauth_connect_target_payload()` serialization shape, but omits `requester_id` and derives the target from `RequestCredentialsTarget`.
3. The generic OAuth endpoints overlap with the legacy Spotify-specific OAuth flow in `src/mindroom/api/integrations.py`, especially connect/callback/status/disconnect request choreography, but provider-specific client handling makes this related duplication rather than a direct shared-helper candidate.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
OAuthConnectResponse	class	lines 52-57	related-only	OAuthConnectResponse auth_url completion_origin BaseModel	src/mindroom/api/integrations.py:80; src/mindroom/api/oauth.py:52
OAuthStatusResponse	class	lines 60-74	related-only	OAuthStatusResponse connected capabilities email hosted_domain BaseModel	src/mindroom/api/integrations.py:80; src/mindroom/api/oauth.py:60
_load_provider	function	lines 77-83	related-only	load_oauth_providers_for_snapshot provider_id Unknown OAuth provider	src/mindroom/api/credentials.py:642; src/mindroom/oauth/registry.py:207
_require_oauth_api_user	async_function	lines 86-87	related-only	verify_user authorization allow_public_paths False	src/mindroom/api/integrations.py:181; src/mindroom/api/oauth.py:310; src/mindroom/api/oauth.py:438
_require_oauth_browser_user	async_function	lines 90-99	none-found	login_redirect_for_request HTTPException 401 oauth browser user	none
_issue_authorization_url	function	lines 102-176	related-only	issue_pending_oauth_state authorization_uri connect_token OAuthConnectResponse	src/mindroom/api/integrations.py:147; src/mindroom/oauth/service.py:275; src/mindroom/oauth/service.py:296
_target_binding_payload	function	lines 179-191	duplicate-found	target_binding_payload oauth_connect_target_payload provider credential_service worker_key	src/mindroom/oauth/service.py:139; src/mindroom/oauth/service.py:61
_resolved_worker_target_for_credentials	function	lines 194-201	duplicate-found	worker_target_for_credentials_target resolve_worker_target RequestCredentialsTarget	src/mindroom/api/credentials.py:582
_verify_connect_target_authorized	function	lines 204-215	related-only	build_dashboard_execution_identity requester_id OAuth connect link current user	src/mindroom/api/credentials.py:289; src/mindroom/oauth/service.py:87
_verify_connect_target_query	function	lines 218-225	related-only	worker_scope unscoped execution_scope agent_name target does not match	src/mindroom/oauth/service.py:275; src/mindroom/oauth/service.py:296
_verify_connect_target_binding	function	lines 228-239	related-only	OAuthConnectTarget worker_key worker_scope agent_name target binding	src/mindroom/oauth/service.py:87; src/mindroom/oauth/service.py:139
_verify_pending_target_binding	function	lines 242-248	related-only	pending payload target binding OAuth state requested credential target	src/mindroom/api/credentials.py:248; src/mindroom/oauth/service.py:139
_claim_str	function	lines 251-258	related-only	_oauth_claims_verified _oauth_claims email hd sub	src/mindroom/oauth/service.py:241; src/mindroom/credential_policy.py:101
_same_external_identity	function	lines 261-269	none-found	same external identity sub email oauth claims refresh token	none
_same_oauth_client	function	lines 272-277	related-only	client_id strip oauth_credentials_match_client_id	src/mindroom/oauth/service.py:217; src/mindroom/api/credentials.py:778
_token_data_preserving_refresh_token	function	lines 280-294	none-found	preserve refresh_token same external identity same oauth client	none
_script_json	function	lines 297-298	related-only	json.dumps replace closing script slash script json	src/mindroom/custom_tools/attachments.py:67; src/mindroom/custom_tools/matrix_room.py:60; src/mindroom/custom_tools/dynamic_tools.py:48
_oauth_success_origin	function	lines 301-304	related-only	oauth_success_redirect_url urlparse scheme netloc origin	src/mindroom/oauth/service.py:151; src/mindroom/oauth/service.py:168
connect	async_function	lines 308-312	related-only	OAuth connect endpoint issue authorization URL generic provider	src/mindroom/api/integrations.py:147
authorize	async_function	lines 316-334	related-only	OAuth authorize endpoint browser redirect auth_url connect_token	src/mindroom/oauth/service.py:275; src/mindroom/api/integrations.py:147
success	async_function	lines 338-365	none-found	OAuth success HTML postMessage opener close window	none
callback	async_function	lines 369-432	related-only	OAuth callback state code exchange save credentials redirect success	src/mindroom/api/integrations.py:174
status	async_function	lines 436-482	related-only	OAuth status connected client config service account capabilities	src/mindroom/api/integrations.py:117; src/mindroom/oauth/service.py:181
disconnect	async_function	lines 486-502	related-only	OAuth disconnect delete scoped credentials provider status disconnected	src/mindroom/api/integrations.py:236; src/mindroom/api/credentials.py:624
```

## Findings

### 1. Duplicate worker-target resolution helper

`src/mindroom/api/oauth.py:194` and `src/mindroom/api/credentials.py:582` both implement the same behavior:

- Return `None` when `RequestCredentialsTarget.worker_scope` is `None`.
- Otherwise call `resolve_worker_target(target.worker_scope, target.agent_name, execution_identity=target.execution_identity)`.

This is exact behavioral duplication.
The copy in `oauth.py` exists because the equivalent helper in `credentials.py` is private.

Difference to preserve: none observed.
Both call sites operate on the same `RequestCredentialsTarget` type and need the same nullable `ResolvedWorkerTarget`.

### 2. Partially duplicated OAuth target payload serialization

`src/mindroom/api/oauth.py:179` builds a dict with `provider`, `credential_service`, `agent_name`, `worker_scope`, and `worker_key`.
`src/mindroom/oauth/service.py:139` serializes `OAuthConnectTarget` with the same fields plus `requester_id`.

The behavior is nearly the same serialization contract for binding opaque OAuth state to a credential target.
`oauth.py` derives `worker_key` by resolving a `RequestCredentialsTarget`; `oauth/service.py` serializes an already-resolved `OAuthConnectTarget`.

Differences to preserve:

- `oauth.py` encodes absent `worker_scope` as `"unscoped"` and absent `worker_key` as `""`.
- `oauth/service.py` expects an `OAuthConnectTarget` and includes `requester_id`.
- The pending dashboard OAuth state currently does not need `requester_id`; the conversation-issued connect token does.

### 3. Generic OAuth flow overlaps with legacy Spotify OAuth flow

`src/mindroom/api/oauth.py:308`, `src/mindroom/api/oauth.py:369`, `src/mindroom/api/oauth.py:436`, and `src/mindroom/api/oauth.py:486` follow the same high-level flow as `src/mindroom/api/integrations.py:147`, `src/mindroom/api/integrations.py:174`, `src/mindroom/api/integrations.py:117`, and `src/mindroom/api/integrations.py:236`:

- Resolve a dashboard credential target.
- Issue or consume pending OAuth state.
- Exchange provider callback input for token data.
- Store or delete credentials.
- Return a status or redirect.

This is duplicated behavior at the endpoint-flow level, but not a safe direct extraction from `oauth.py` alone.
Spotify uses `spotipy`, environment-specific config, and a different credential storage path (`target.target_manager`) while generic OAuth uses provider abstractions, scoped credential helpers, PKCE, claim validation, and refresh-token preservation.

Difference to preserve: the Spotify flow is service-specific and appears to predate the generic provider abstraction.
Consolidation would likely mean migrating Spotify into the generic `OAuthProvider` registry rather than adding endpoint helper wrappers.

## Proposed Generalization

1. Expose the existing credentials-target worker resolver from `src/mindroom/api/credentials.py` as a public helper, for example `worker_target_for_credentials_target()`, and reuse it from `src/mindroom/api/oauth.py`.
2. Consider adding a small `oauth_target_binding_payload(provider, worker_target, agent_name)` helper under `src/mindroom/oauth/service.py` only if another pending-state binding needs the same serialization.
3. Leave the Spotify flow alone unless there is a planned migration to a generic `OAuthProvider`; a helper extraction would mostly hide provider-specific differences.

No broad refactor recommended for this audit.

## Risk/Tests

The worker-target helper extraction is low risk but should be covered by existing OAuth credential-scope tests and dashboard credential target tests.
The target payload generalization is moderate risk because it protects OAuth state replay/binding checks; tests should cover shared, user, user-agent, unscoped, and mismatched `worker_key` cases.
Migrating Spotify to the generic provider flow is higher risk and would need endpoint compatibility tests for connect, callback, status, disconnect, state validation, and credential persistence.
