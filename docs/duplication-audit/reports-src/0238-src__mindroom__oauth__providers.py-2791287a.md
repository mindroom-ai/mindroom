Summary: Top duplication candidates are OAuth token response normalization duplicated between `src/mindroom/oauth/providers.py` and `src/mindroom/oauth/google.py`, public MindRoom OAuth base URL construction duplicated between provider redirect URI logic and `src/mindroom/oauth/service.py`, and unverified JWT payload decoding duplicated with Codex CLI OAuth token handling in `src/mindroom/codex_model.py`.
The remaining symbols are mostly provider contracts, dataclasses, or orchestration methods with related call sites but no independent duplicate implementation.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
OAuthProviderError	class	lines 38-39	related-only	OAuthProviderError exceptions OAuthProviderError imports	src/mindroom/oauth/state.py:17; src/mindroom/oauth/service.py:11; src/mindroom/api/oauth.py:27
OAuthProviderNotConfiguredError	class	lines 42-43	none-found	OAuthProviderNotConfiguredError not configured OAuth provider	none
OAuthClaimValidationError	class	lines 46-47	related-only	OAuthClaimValidationError claim validation OAuth	src/mindroom/oauth/google.py:15; src/mindroom/oauth/service.py:11; src/mindroom/api/oauth.py:25
OAuthConnectionRequired	class	lines 50-62	related-only	OAuthConnectionRequired oauth_connection_required connect_url	src/mindroom/oauth/client.py:142; src/mindroom/tool_system/tool_hooks.py:634
OAuthConnectionRequired.__init__	method	lines 53-62	related-only	provider_id connect_url exception attributes	src/mindroom/oauth/client.py:152; src/mindroom/tool_system/tool_hooks.py:637
OAuthClientConfig	class	lines 66-71	related-only	OAuthClientConfig client_id client_secret redirect_uri	src/mindroom/oauth/service.py:16; src/mindroom/oauth/google.py:16; src/mindroom/oauth/client.py:204
OAuthClientConfigResolution	class	lines 75-79	none-found	OAuthClientConfigResolution client_config_resolution service	none
OAuthTokenResult	class	lines 83-88	related-only	OAuthTokenResult token_data claims claims_verified	src/mindroom/oauth/google.py:18; src/mindroom/oauth/service.py:263; src/mindroom/oauth/service.py:333
OAuthClaimValidationContext	class	lines 92-99	none-found	OAuthClaimValidationContext claim_validator provider_id runtime_paths	none
_normalize_env_names	function	lines 110-115	none-found	normalize env names string sequence tuple none	none
_split_csv	function	lines 118-121	related-only	split comma strip lower csv env	src/mindroom/custom_tools/claude_agent.py:75; src/mindroom/tool_system/sandbox_proxy.py:156; src/mindroom/tool_system/metadata.py:1178
_runtime_env_value	function	lines 124-129	none-found	runtime_paths env_value first env names	none
_runtime_port	function	lines 132-133	duplicate-found	MINDROOM_PORT default 8765 runtime_paths env_value	src/mindroom/oauth/service.py:164
_decode_jwt_claims_unverified	function	lines 136-147	duplicate-found	urlsafe_b64decode jwt payload json loads exp	src/mindroom/codex_model.py:149
_default_token_parser	function	lines 150-191	duplicate-found	access_token refresh_token scopes token_uri expires_at id_token	src/mindroom/oauth/google.py:34
_token_result_with_core_metadata	function	lines 194-211	related-only	_source oauth _oauth_provider scopes client_id token metadata	src/mindroom/oauth/google.py:82; src/mindroom/credential_policy.py:101
_verified_claims_for_storage	function	lines 214-216	related-only	_oauth_claims verified claims dict storage	src/mindroom/oauth/service.py:255; src/mindroom/api/oauth.py:251
_claim_email_domain	function	lines 219-223	none-found	email rsplit domain claims	none
oauth_expires_at_from_response	function	lines 226-234	related-only	expires_at expires_in time response token	src/mindroom/oauth/google.py:95; src/mindroom/oauth/client.py:192; src/mindroom/oauth/service.py:203
generate_pkce_code_verifier	function	lines 237-239	related-only	secrets token_urlsafe verifier state token	src/mindroom/oauth/state.py:119; src/mindroom/api/sandbox_worker_prep.py:109
pkce_s256_code_challenge	function	lines 242-245	none-found	code_challenge S256 sha256 urlsafe_b64encode	none
OAuthProvider	class	lines 249-578	related-only	OAuthProvider dataclass authorization_url token_url scopes providers	src/mindroom/oauth/google_calendar.py:18; src/mindroom/oauth/google_drive.py:18; src/mindroom/oauth/google_gmail.py:20; src/mindroom/oauth/google_sheets.py:18
OAuthProvider.__post_init__	method	lines 273-308	related-only	validate_service_name oauth_client suffix scopes redirect path	src/mindroom/credential_policy.py:91; src/mindroom/api/credentials.py:761; src/mindroom/api/credentials.py:778
OAuthProvider.all_client_config_services	method	lines 311-313	related-only	client_config_services shared_client_config_services	src/mindroom/api/oauth.py:462; src/mindroom/api/oauth.py:467
OAuthProvider.redirect_path	method	lines 316-318	related-only	api oauth provider callback path	src/mindroom/oauth/service.py:171; src/mindroom/oauth/service.py:293
OAuthProvider.client_config	method	lines 320-323	related-only	client_config runtime_paths provider.client_config	src/mindroom/oauth/service.py:158; src/mindroom/oauth/service.py:189; src/mindroom/oauth/client.py:204
OAuthProvider.client_config_resolution	method	lines 325-336	related-only	client_config_resolution load_credentials client config services	src/mindroom/api/oauth.py:456
OAuthProvider._stored_client_config_from_service	method	lines 338-360	related-only	client_id client_secret redirect_uri strip oauth client config	src/mindroom/api/credentials.py:772; src/mindroom/api/credentials.py:778
OAuthProvider.require_client_config	method	lines 362-369	none-found	require client_config not configured Store client_id client_secret	none
OAuthProvider.default_redirect_uri	method	lines 371-378	duplicate-found	MINDROOM_PUBLIC_URL MINDROOM_BASE_URL MINDROOM_PORT localhost redirect	src/mindroom/oauth/service.py:151
OAuthProvider.issue_pkce_code_verifier	method	lines 380-384	related-only	pkce_code_challenge_method code_verifier	src/mindroom/api/oauth.py:124; src/mindroom/api/oauth.py:157
OAuthProvider.authorization_uri	method	lines 386-417	related-only	OAuth2Session create_authorization_url code_challenge state	src/mindroom/api/homeassistant_integration.py:218
OAuthProvider.exchange_code	async_method	lines 419-471	related-only	AsyncOAuth2Client fetch_token authorization_code token_exchanger	src/mindroom/api/homeassistant_integration.py:293; src/mindroom/codex_model.py:160
OAuthProvider.refresh_token_data	async_method	lines 473-515	related-only	refresh_token AsyncOAuth2Client refresh_token preserve claims	src/mindroom/oauth/client.py:291; src/mindroom/codex_model.py:160
OAuthProvider.resolved_allowed_email_domains	method	lines 517-521	related-only	allowed_email_domains env split csv domains	src/mindroom/oauth/google.py:102
OAuthProvider.resolved_allowed_hosted_domains	method	lines 523-527	related-only	allowed_hosted_domains env split csv domains	src/mindroom/oauth/google.py:102
OAuthProvider.validate_claims	method	lines 529-560	related-only	email_verified hd claim validate claims policy	src/mindroom/oauth/service.py:241; src/mindroom/api/oauth.py:251
OAuthProvider.token_result_with_safe_claims	method	lines 562-578	related-only	safe claims pop id_token client_secret _oauth_claims	src/mindroom/credential_policy.py:111; src/mindroom/oauth/service.py:333
```

Findings:

1. Token response normalization is duplicated between default and Google OAuth parsers.
`src/mindroom/oauth/providers.py:150` builds normalized token data from `access_token`, response `scope`, `refresh_token`, `token_type`, `expires_at`, and optional `id_token`.
`src/mindroom/oauth/google.py:34` repeats nearly the same token-data construction at lines 40-99 after Google-specific identity-token verification.
The duplicated behavior is the provider-independent conversion from an OAuth token response into MindRoom credential fields.
Differences to preserve: Google must require and verify identity claims, support reusing stored verified claims during refresh, and raise Google-specific validation messages.

2. Public/local OAuth URL origin resolution is duplicated.
`src/mindroom/oauth/providers.py:371` builds the default callback redirect URI from `MINDROOM_PUBLIC_URL`, `MINDROOM_BASE_URL`, or `http://localhost:{MINDROOM_PORT}`.
`src/mindroom/oauth/service.py:151` independently builds the same public base URL using the same env names and port fallback, with one extra fallback that derives the origin from a stored provider redirect URI.
The duplicated behavior is choosing the canonical MindRoom origin for OAuth URLs.
Differences to preserve: `mindroom_public_base_url` may inspect a configured provider redirect URI, while `default_redirect_uri` must append the provider callback path.

3. Unverified JWT payload decoding is duplicated with Codex OAuth token handling.
`src/mindroom/oauth/providers.py:136` decodes the JWT payload to a dict for default OAuth `id_token` claims.
`src/mindroom/codex_model.py:149` decodes the same base64url JWT payload shape to read the `exp` claim from a Codex access token.
The duplicated behavior is padding and base64url-decoding the second JWT segment and parsing JSON.
Differences to preserve: providers returns an empty dict on malformed or non-dict claims, while Codex returns `None` unless `exp` is an int.

Proposed generalization:

1. Add a small internal token-normalization helper in `src/mindroom/oauth/providers.py`, for example `_token_data_from_response(provider, token_response, client_config, *, access_token_error)`, and let both `_default_token_parser` and `_google_token_parser` call it after provider-specific claim handling.
2. Extract the shared base-origin selection from `mindroom_public_base_url` into an OAuth service helper that can be used by `OAuthProvider.default_redirect_uri`; keep provider redirect-origin fallback optional.
3. Add a tiny JWT payload helper, likely in a focused module such as `src/mindroom/oauth/jwt.py` only if another OAuth-adjacent caller is added; otherwise leave the Codex-specific `exp` reader alone to avoid coupling Codex model auth to provider OAuth internals.

Risk/tests:

Token normalization refactoring would need tests covering default token parsing, Google parser identity-token verification, scope override from response `scope`, refresh-token preservation during refresh, and removal of `_id_token` from safe storage.
Base URL refactoring would need tests for `MINDROOM_PUBLIC_URL`, `MINDROOM_BASE_URL`, default `MINDROOM_PORT`, provider callback path appending, and service fallback to a stored redirect URI origin.
JWT helper extraction is low risk but could alter malformed-token handling if exception sets are not preserved exactly.
No refactor is recommended for the exception/dataclass contract symbols because the related usages are consumers of the provider API rather than duplicated implementations.
