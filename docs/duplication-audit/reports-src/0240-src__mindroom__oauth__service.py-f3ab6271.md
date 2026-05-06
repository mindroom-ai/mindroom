Summary: top duplication candidates are OAuth target payload construction split between service and API callback binding, duplicated user-facing OAuth connection-required messages, and repeated MindRoom/base redirect URL derivation.
The rest of the module is mostly thin orchestration over `oauth.state` and `OAuthProvider`, with related but not clearly duplicate credential validation logic.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
OAuthConnectTarget	class	lines 50-58	related-only	OAuthConnectTarget PendingOAuthState RequestCredentialsTarget worker_scope worker_key requester_id	src/mindroom/api/credentials.py:63; src/mindroom/api/credentials.py:103; src/mindroom/api/oauth.py:179
issue_oauth_connect_token	function	lines 61-84	related-only	issue_opaque_oauth_state oauth connect token worker_target requester_id	src/mindroom/api/credentials.py:220; src/mindroom/oauth/state.py:110
_connect_target_from_payload	function	lines 87-106	duplicate-found	provider credential_service worker_scope worker_key OAuth state payload validation	src/mindroom/api/credentials.py:248; src/mindroom/api/oauth.py:228; src/mindroom/api/oauth.py:242
lookup_oauth_connect_token	function	lines 109-116	related-only	read_opaque_oauth_state lookup oauth token without consume	src/mindroom/oauth/state.py:131; src/mindroom/api/oauth.py:102
consume_oauth_connect_token	function	lines 119-136	related-only	consume_opaque_oauth_state expected_target state changed	src/mindroom/oauth/state.py:163; src/mindroom/api/credentials.py:248
oauth_connect_target_payload	function	lines 139-148	duplicate-found	provider credential_service agent_name worker_scope worker_key target binding payload	src/mindroom/api/oauth.py:179
mindroom_public_base_url	function	lines 151-165	duplicate-found	MINDROOM_PUBLIC_URL MINDROOM_BASE_URL localhost port redirect origin dashboard url	src/mindroom/oauth/providers.py:371; src/mindroom/api/integrations.py:29
oauth_success_redirect_url	function	lines 168-171	related-only	oauth success redirect url api oauth success origin	src/mindroom/api/oauth.py:301; src/mindroom/oauth/providers.py:371
oauth_provider_service_account_configured	function	lines 174-178	related-only	GOOGLE_SERVICE_ACCOUNT_FILE google service account provider ids	src/mindroom/oauth/client.py:68; src/mindroom/config/models.py:368
oauth_credentials_usable	function	lines 181-214	related-only	credentials usable refresh_token expires_at scopes identity policy	src/mindroom/oauth/client.py:224; src/mindroom/oauth/client.py:277
oauth_credentials_match_client_id	function	lines 217-223	duplicate-found	client_id strip token same oauth client	src/mindroom/api/oauth.py:272; src/mindroom/oauth/client.py:200
oauth_credentials_have_required_scopes	function	lines 226-238	related-only	scope scopes split provider.scopes required scopes implications	src/mindroom/oauth/providers.py:150; src/mindroom/oauth/google.py:77; src/mindroom/oauth/client.py:224
oauth_credentials_satisfy_identity_policy	function	lines 241-272	related-only	_oauth_claims_verified validate_claims allowed domains claim_validator	src/mindroom/api/oauth.py:251; src/mindroom/oauth/providers.py:529; src/mindroom/oauth/client.py:224
build_oauth_authorize_url	function	lines 275-293	related-only	urlencode authorize agent_name execution_scope connect_token	auth provider authorize urls src/mindroom/oauth/providers.py:386; src/mindroom/api/homeassistant_integration.py:232
oauth_connect_url	function	lines 296-312	related-only	connect url worker_target agent_name execution_scope issue token	src/mindroom/oauth/client.py:142; src/mindroom/api/oauth.py:315
build_oauth_connect_instruction	function	lines 315-330	duplicate-found	not connected for this agent Open this MindRoom link connect_url	src/mindroom/oauth/client.py:142
sanitized_oauth_token_result	function	lines 333-335	related-only	token_result_with_safe_claims safe claims sanitized oauth token	src/mindroom/oauth/providers.py:562; src/mindroom/api/oauth.py:403
```

## Findings

1. OAuth target payload construction and validation are duplicated across the generic OAuth service and API route binding checks.
`oauth_connect_target_payload()` serializes `provider`, `credential_service`, `agent_name`, `worker_scope`, `worker_key`, and `requester_id` at `src/mindroom/oauth/service.py:139`.
`_target_binding_payload()` independently serializes almost the same target fields at `src/mindroom/api/oauth.py:179`, omitting only `requester_id` because callback binding compares credential target shape, not link ownership.
`_connect_target_from_payload()` validates provider/service/scope/key at `src/mindroom/oauth/service.py:87`, while `_verify_connect_target_binding()` and `_verify_pending_target_binding()` repeat semantic target equality checks at `src/mindroom/api/oauth.py:228` and `src/mindroom/api/oauth.py:242`.
The difference to preserve is that conversation-issued links include `requester_id`, while dashboard pending-state binding deliberately does not.

2. OAuth connection-required instruction text is duplicated.
`build_oauth_connect_instruction()` builds the exact user-facing sentence at `src/mindroom/oauth/service.py:315`.
`ScopedOAuthClientMixin._connection_required()` independently builds the same sentence after deriving the same connect URL at `src/mindroom/oauth/client.py:142`.
The mixin also needs the structured `provider_id` and `connect_url` fields for `OAuthConnectionRequired`, so only message construction is duplicated.

3. Public MindRoom URL/origin derivation is repeated in nearby OAuth code.
`mindroom_public_base_url()` checks `MINDROOM_PUBLIC_URL`, `MINDROOM_BASE_URL`, provider redirect URI origin, then localhost port at `src/mindroom/oauth/service.py:151`.
`OAuthProvider.default_redirect_uri()` repeats the env-origin and localhost-port part at `src/mindroom/oauth/providers.py:371`.
`get_dashboard_url()` in the older Spotify/Home Assistant integration path derives a dashboard base from `request.base_url` at `src/mindroom/api/integrations.py:29`, which is related but request-surface-specific.
The provider fallback to parse `client_config.redirect_uri` is unique to `mindroom_public_base_url()` and should be preserved.

4. Client ID equality is duplicated with a small normalization mismatch.
`oauth_credentials_match_client_id()` requires a string, strips it, and compares to the configured client ID at `src/mindroom/oauth/service.py:217`.
`_same_oauth_client()` repeats the token-client comparison against stored credentials at `src/mindroom/api/oauth.py:272`.
The API helper compares old token data to new token data for refresh-token preservation, not active client config, so a shared lower-level string comparison helper would be the only safe extraction.

## Proposed Generalization

1. Add a small target payload helper in `mindroom.oauth.service`, for example `oauth_target_binding_payload(provider, agent_name, worker_scope, worker_key, requester_id=None)`, and have both `oauth_connect_target_payload()` and `src/mindroom/api/oauth.py:_target_binding_payload()` use it.
2. Have `ScopedOAuthClientMixin._connection_required()` call `build_oauth_connect_instruction()` for the exception message while keeping its existing `connect_url` and provider fields.
3. Consider a private origin helper shared between `mindroom_public_base_url()` and `OAuthProvider.default_redirect_uri()`, but only if this code is being touched for OAuth URL work.
4. If client ID comparison changes are needed, extract only a tiny normalized string predicate rather than merging the higher-level credential helpers.

## Risk/tests

Main risk is changing OAuth state payload equality and accidentally invalidating existing connect links or pending callback state.
Tests should cover conversation connect-token issue/lookup/consume, dashboard authorize with `connect_token`, callback pending target binding, and the OAuth client mixin connection-required message.
No refactor is required for this audit; the conservative recommendation is to deduplicate the target payload and connection message during the next OAuth change.
