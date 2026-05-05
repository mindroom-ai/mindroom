## Summary

Top duplication candidates for `src/mindroom/oauth/client.py`:

1. `OAuthConnectionRequired` structured result serialization is repeated in the OAuth client, sandbox runner, and in-process tool hook paths.
2. Stored OAuth token validation and usability checks overlap with `mindroom.oauth.service.oauth_credentials_usable`, though the client must additionally instantiate and refresh Google credentials.
3. Google OAuth token-data construction is duplicated between the generic provider parser and Google-specific parser, and the client consumes the same token-data shape when building `google.oauth2.credentials.Credentials`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
OAuthAuthSource	class	lines 37-43	none-found	OAuthAuthSource PROVIDED_CREDENTIALS ORIGINAL_AUTH VALID_CREDENTIALS STORED_OAUTH	src/mindroom/oauth/client.py:37; src/mindroom/oauth/client.py:259
_AuthDescriptor	class	lines 46-50	none-found	_AuthDescriptor Protocol __get__ original auth descriptor	src/mindroom/oauth/client.py:46; src/mindroom/custom_tools/gmail.py:63; src/mindroom/custom_tools/google_calendar.py:74; src/mindroom/custom_tools/google_drive.py:64; src/mindroom/custom_tools/google_sheets.py:70
_AuthDescriptor.__get__	method	lines 49-50	none-found	__get__ auth method bind original auth	src/mindroom/oauth/client.py:49; src/mindroom/oauth/client.py:103
ScopedOAuthClientMixin	class	lines 53-320	related-only	ScopedOAuthClientMixin OAuth client mixin Google tools	src/mindroom/custom_tools/gmail.py:26; src/mindroom/custom_tools/google_calendar.py:26; src/mindroom/custom_tools/google_drive.py:30; src/mindroom/custom_tools/google_sheets.py:32
ScopedOAuthClientMixin._apply_runtime_original_auth_kwargs	method	lines 68-78	duplicate-found	GOOGLE_SERVICE_ACCOUNT_FILE delegated_user service_account_path runtime env	src/mindroom/custom_tools/gmail.py:51; src/mindroom/custom_tools/google_calendar.py:62; src/mindroom/custom_tools/google_drive.py:56; src/mindroom/custom_tools/google_sheets.py:58; src/mindroom/oauth/service.py:174
ScopedOAuthClientMixin._initialize_oauth_client	method	lines 80-99	duplicate-found	initialize oauth client provided_creds worker_target credentials_manager logger defer_to_original_auth	src/mindroom/custom_tools/gmail.py:45; src/mindroom/custom_tools/google_calendar.py:45; src/mindroom/custom_tools/google_drive.py:44; src/mindroom/custom_tools/google_sheets.py:51
ScopedOAuthClientMixin._set_original_auth	method	lines 101-103	related-only	set original auth __get__ AgnoGoogle _auth	src/mindroom/custom_tools/gmail.py:63; src/mindroom/custom_tools/google_calendar.py:74; src/mindroom/custom_tools/google_drive.py:64; src/mindroom/custom_tools/google_sheets.py:70
ScopedOAuthClientMixin._wrap_oauth_function_entrypoints	method	lines 105-123	duplicate-found	wrap function entrypoint function.entrypoint setattr wraps	src/mindroom/tool_system/output_files.py:545; src/mindroom/tool_system/output_files.py:586; src/mindroom/tool_system/sandbox_proxy.py:947; src/mindroom/tool_system/sandbox_proxy.py:985
ScopedOAuthClientMixin._wrap_oauth_function_entrypoints.<locals>.oauth_entrypoint	nested_function	lines 113-120	related-only	oauth_entrypoint ensure_structured_auth wraps entrypoint structured auth	src/mindroom/tool_system/output_files.py:552; src/mindroom/tool_system/output_files.py:565; src/mindroom/tool_system/sandbox_proxy.py:947; src/mindroom/tool_system/sandbox_proxy.py:985
ScopedOAuthClientMixin._load_token_data	method	lines 125-131	related-only	load_scoped_credentials credential_service worker_target	src/mindroom/credentials.py:486; src/mindroom/api/oauth.py:407
ScopedOAuthClientMixin._save_token_data	method	lines 133-140	related-only	save_scoped_credentials credential_service worker_target	src/mindroom/credentials.py:544; src/mindroom/api/oauth.py:414
ScopedOAuthClientMixin._connection_required	method	lines 142-156	duplicate-found	OAuthConnectionRequired connect_url message provider_id oauth_connect_url build_oauth_connect_instruction	src/mindroom/oauth/service.py:296; src/mindroom/oauth/service.py:315; src/mindroom/api/sandbox_runner.py:1058
ScopedOAuthClientMixin._raise_connection_required	method	lines 158-159	related-only	raise connection required OAuthConnectionRequired	src/mindroom/oauth/client.py:285; src/mindroom/oauth/client.py:300; src/mindroom/oauth/client.py:310
ScopedOAuthClientMixin._structured_auth_failure	method	lines 161-169	duplicate-found	oauth_connection_required error provider connect_url json dumps	src/mindroom/api/sandbox_runner.py:1058; src/mindroom/tool_system/tool_hooks.py:634
ScopedOAuthClientMixin._ensure_structured_auth	method	lines 171-182	related-only	ensure structured auth select auth source OAuthConnectionRequired	src/mindroom/api/sandbox_runner.py:1023; src/mindroom/tool_system/tool_hooks.py:634
ScopedOAuthClientMixin._token_expiry	method	lines 184-190	duplicate-found	expires_at finite bool int float datetime fromtimestamp token expiry	src/mindroom/oauth/service.py:203; src/mindroom/oauth/service.py:211; src/mindroom/oauth/providers.py:181; src/mindroom/oauth/google.py:95
ScopedOAuthClientMixin._expires_at_from_credentials	method	lines 192-198	related-only	credentials expiry timestamp tzinfo UTC expires_at	src/mindroom/oauth/providers.py:181; src/mindroom/oauth/google.py:95
ScopedOAuthClientMixin._credentials_from_token_data	method	lines 200-222	duplicate-found	google_credentials.Credentials token refresh_token token_uri client_id scopes expiry	src/mindroom/oauth/providers.py:167; src/mindroom/oauth/google.py:82; src/mindroom/oauth/service.py:217; src/mindroom/oauth/service.py:226
ScopedOAuthClientMixin._load_stored_credentials	method	lines 224-249	duplicate-found	credentials required scopes identity policy client id usable stored oauth	src/mindroom/oauth/service.py:181; src/mindroom/api/tools.py:106; src/mindroom/api/oauth.py:407
ScopedOAuthClientMixin._should_fallback_to_original_auth	method	lines 251-253	duplicate-found	should_fallback_to_original_auth service_account_path GOOGLE_SERVICE_ACCOUNT_FILE	src/mindroom/custom_tools/gmail.py:66; src/mindroom/custom_tools/google_calendar.py:77; src/mindroom/custom_tools/google_drive.py:95; src/mindroom/custom_tools/google_sheets.py:73; src/mindroom/oauth/service.py:174
ScopedOAuthClientMixin._should_skip_auth	method	lines 255-257	related-only	provided creds valid skip auth creds.valid	src/mindroom/oauth/client.py:261; src/mindroom/oauth/client.py:315
ScopedOAuthClientMixin._select_auth_source	method	lines 259-267	related-only	auth priority provided original valid stored	src/mindroom/oauth/client.py:172; src/mindroom/oauth/client.py:314
ScopedOAuthClientMixin._auth_with_original_fallback	method	lines 269-275	related-only	original auth fallback original_auth_completed creds valid	src/mindroom/custom_tools/gmail.py:63; src/mindroom/custom_tools/google_calendar.py:74; src/mindroom/custom_tools/google_drive.py:64; src/mindroom/custom_tools/google_sheets.py:70
ScopedOAuthClientMixin._auth_with_stored_oauth	method	lines 277-310	duplicate-found	stored oauth load token required scopes identity policy refresh google credentials save token data	src/mindroom/oauth/service.py:181; src/mindroom/oauth/providers.py:473; src/mindroom/credentials.py:486; src/mindroom/credentials.py:544
ScopedOAuthClientMixin._auth	method	lines 312-320	related-only	auth selected source stored oauth original fallback	src/mindroom/oauth/client.py:171; src/mindroom/oauth/client.py:269; src/mindroom/oauth/client.py:277
```

## Findings

### 1. Structured OAuth-required result serialization is duplicated

`ScopedOAuthClientMixin._structured_auth_failure` serializes `OAuthConnectionRequired` to a JSON object with `error`, `oauth_connection_required`, `provider`, and `connect_url` at `src/mindroom/oauth/client.py:161`.
The same field mapping is returned as a dict in `_oauth_connection_required_result` at `src/mindroom/api/sandbox_runner.py:1058` and in the tool hook exception branch at `src/mindroom/tool_system/tool_hooks.py:634`.

This is functionally the same user-visible protocol for OAuth connection prompts.
The only difference is representation: `oauth.client` returns a JSON string because wrapped tool entrypoints return tool text, while sandbox and tool hooks return Python dicts.

### 2. Google service-account fallback setup is repeated in every Google wrapper

`_apply_runtime_original_auth_kwargs` populates `service_account_path` and `delegated_user` from runtime env at `src/mindroom/oauth/client.py:68`.
Each Google wrapper calls it during nearly identical OAuth setup: Gmail at `src/mindroom/custom_tools/gmail.py:51`, Calendar at `src/mindroom/custom_tools/google_calendar.py:62`, Drive at `src/mindroom/custom_tools/google_drive.py:56`, and Sheets at `src/mindroom/custom_tools/google_sheets.py:58`.
Those wrappers also repeat the same fallback predicate using `self.service_account_path or GOOGLE_SERVICE_ACCOUNT_FILE` at `src/mindroom/custom_tools/gmail.py:66`, `src/mindroom/custom_tools/google_calendar.py:77`, `src/mindroom/custom_tools/google_drive.py:95`, and `src/mindroom/custom_tools/google_sheets.py:73`.

This is the same behavior: prefer upstream Agno auth when a Google service account is configured, while otherwise using stored MindRoom OAuth.
The wrappers differ only in provider, tool name, parent class, and small per-tool constructor normalization.

### 3. Stored token usability checks overlap between client auth and dashboard status

`_load_stored_credentials` and `_auth_with_stored_oauth` both require token presence, provider scopes, identity policy, and valid Google credential material at `src/mindroom/oauth/client.py:224` and `src/mindroom/oauth/client.py:277`.
`oauth_credentials_usable` performs a similar token-level usability decision for dashboard/tool status at `src/mindroom/oauth/service.py:181`, including client-id matching, scope checks, identity policy, token presence, `expires_at`, and refresh-token handling.

This is related duplication, but not a direct replacement.
The mixin must create `google.oauth2.credentials.Credentials`, refresh them through `google.auth.transport.requests.Request`, and persist refreshed access tokens.
The service helper is provider-agnostic and intentionally avoids Google credential objects.

### 4. OAuth token-data normalization is duplicated between generic and Google providers

`_credentials_from_token_data` consumes the token-data shape with `token`, `refresh_token`, `token_uri`, `client_id`, `scopes`, and `expires_at` at `src/mindroom/oauth/client.py:200`.
That shape is constructed in `_default_token_parser` at `src/mindroom/oauth/providers.py:150` and repeated in the Google-specific parser at `src/mindroom/oauth/google.py:82`.
Both parsers derive scopes from the response `scope` string, store core OAuth metadata, preserve `refresh_token` and `token_type`, and compute `expires_at`.

The duplication is real between provider parsers.
The client side is a consumer rather than a duplicate constructor, but it depends tightly on the same token-data contract.

### 5. Toolkit entrypoint wrapping has related duplicate mechanics

`_wrap_oauth_function_entrypoints` iterates registered functions, wraps non-`None` entrypoints, updates `function.entrypoint`, and installs the wrapper on the toolkit instance at `src/mindroom/oauth/client.py:105`.
`wrap_function_for_output_files` and `_wrap_entrypoint` perform similar registered-function entrypoint wrapping and metadata preservation at `src/mindroom/tool_system/output_files.py:545` and `src/mindroom/tool_system/output_files.py:586`.
Sandbox proxy wrapping also mutates function entrypoints at `src/mindroom/tool_system/sandbox_proxy.py:947` and `src/mindroom/tool_system/sandbox_proxy.py:985`.

The shared behavior is entrypoint interception around Agno tool functions.
The differences are significant: OAuth wrapping is sync-only and returns structured auth failures before executing the function, output-file wrapping supports async/sync and changes schemas, and sandbox proxy wrapping routes calls out-of-process.

## Proposed Generalization

1. Add a small OAuth serialization helper, for example `oauth_connection_required_payload(exc: OAuthConnectionRequired) -> dict[str, object]`, in `mindroom.oauth.providers` or `mindroom.oauth.service`.
2. Have `_structured_auth_failure` call that helper and `json.dumps` the result, while sandbox runner and tool hooks return the dict directly.
3. Consider a tiny `google_service_account_configured_for_tool(tool, runtime_paths)` helper if the four Google wrappers continue to grow, but the current duplication is low risk and localized.
4. Keep stored credential loading and refresh logic in `ScopedOAuthClientMixin`; do not merge it with `oauth_credentials_usable` unless a provider-agnostic credential refresh abstraction is introduced.
5. Extract common token-data construction from `_default_token_parser` and the Google parser only if another provider parser repeats the same shape.

## Risk/tests

No production code was edited.

If the OAuth serialization helper is introduced, tests should cover:

- in-process tool hook OAuth-required results,
- sandbox runner OAuth-required results,
- wrapped OAuth toolkit function results that return JSON strings,
- exact payload keys and provider/connect URL preservation.

If Google wrapper fallback setup is consolidated, tests should cover all four Google tool wrappers with explicit credentials, stored OAuth credentials, and service-account env configuration.

If token-data construction is extracted, tests should cover generic OAuth parsing, Google ID-token parsing, refresh-token preservation, scope normalization, and `expires_at` handling.
