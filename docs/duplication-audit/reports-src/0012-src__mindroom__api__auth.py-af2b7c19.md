# Summary

Top duplication candidates for `src/mindroom/api/auth.py`:

1. Bearer Authorization parsing is duplicated between `src/mindroom/api/auth.py` and `src/mindroom/api/openai_compat.py`.
2. Runtime/header blank-string normalization appears in several local helpers, but each call site has slightly different semantics or scope.
3. API auth-state snapshot caching overlaps structurally with config lifecycle snapshot access, but the auth-specific rebuild rules are not duplicated elsewhere.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_AuthSessionRequest	class	lines 38-41	none-found	AuthSessionRequest api_key BaseModel session payload	src/mindroom/api/auth.py:607; src/mindroom/api/frontend.py:53
_SupabaseUserProtocol	class	lines 44-46	none-found	Supabase user protocol id email get_user	src/mindroom/api/auth.py:318; src/mindroom/api/credentials.py:302
_SupabaseUserResponseProtocol	class	lines 49-50	none-found	Supabase user response protocol get_user response.user	src/mindroom/api/auth.py:324; src/mindroom/api/openai_compat.py:556
_SupabaseAuthProtocol	class	lines 53-54	none-found	Supabase auth protocol get_user	src/mindroom/api/auth.py:150; src/mindroom/api/auth.py:318
_SupabaseAuthProtocol.get_user	method	lines 54-54	none-found	get_user token supabase auth	src/mindroom/api/auth.py:324
_SupabaseClientProtocol	class	lines 57-58	none-found	Supabase client protocol auth create_client	src/mindroom/api/auth.py:150; src/mindroom/api/auth.py:173
TrustedUpstreamAuthSettings	class	lines 62-69	none-found	trusted upstream auth settings headers matrix template	src/mindroom/api/auth.py:113; src/mindroom/api/auth.py:274
ApiAuthSettings	class	lines 73-81	none-found	ApiAuthSettings platform_login_url supabase_url mindroom_api_key	src/mindroom/api/auth.py:93; src/mindroom/api/main.py:253
ApiAuthState	class	lines 85-90	related-only	ApiAuthState auth_state snapshot runtime_paths	src/mindroom/api/config_lifecycle.py:57; src/mindroom/api/config_lifecycle.py:69
build_auth_settings	function	lines 93-102	related-only	build settings env_value account_id runtime_paths	src/mindroom/api/main.py:253; src/mindroom/api/runtime_reload.py:72; src/mindroom/oauth/service.py:153
_env_text	function	lines 105-110	related-only	env_value strip return stripped or None runtime env text	src/mindroom/oauth/providers.py:124; src/mindroom/constants.py:776; src/mindroom/tool_system/sandbox_proxy.py:87; src/mindroom/tool_system/metadata.py:1208
build_trusted_upstream_auth_settings	function	lines 113-124	none-found	MINDROOM_TRUSTED_UPSTREAM build settings env headers	src/mindroom/api/auth.py:113
app_auth_state	function	lines 127-147	related-only	app auth state snapshot lock cached auth_state	src/mindroom/api/config_lifecycle.py:107; src/mindroom/api/auth.py:334
_init_supabase_auth	function	lines 150-173	none-found	import supabase create_client auto_install_tool_extra	src/mindroom/tool_system/dependencies.py:80; src/mindroom/api/auth.py:150
_extract_bearer_token	function	lines 176-181	duplicate-found	Authorization Bearer removeprefix strip token	src/mindroom/api/openai_compat.py:578; src/mindroom/api/auth.py:189
_is_standalone_public_path	function	lines 184-186	none-found	standalone public paths callback unauthenticated	src/mindroom/api/homeassistant_integration.py:397; src/mindroom/api/integrations.py:36
_get_request_token	function	lines 189-205	related-only	bearer token cookies request token auth cookie	src/mindroom/api/openai_compat.py:578; src/mindroom/api/frontend.py:53
_get_configured_header	function	lines 208-216	related-only	request headers get strip configured header	src/mindroom/api/auth.py:287; src/mindroom/tool_system/metadata.py:1208
_trusted_upstream_email_localpart	function	lines 219-226	none-found	email localpart partition count @	src/mindroom/oauth/providers.py:517; src/mindroom/api/auth.py:251
_validated_trusted_upstream_email_to_matrix_template	function	lines 229-248	none-found	email to matrix template localpart placeholder trusted upstream	src/mindroom/api/auth.py:251; src/mindroom/matrix/identity.py:161
_derive_trusted_upstream_matrix_user_id	function	lines 251-271	related-only	derive matrix user id email localpart template parse matrix	src/mindroom/api/credentials.py:195; src/mindroom/cli/config.py:121; src/mindroom/matrix/identity.py:161
_trusted_upstream_auth_user	function	lines 274-310	none-found	trusted upstream auth user headers matrix_user_id auth_source	src/mindroom/api/auth.py:377; src/mindroom/api/auth.py:551
_supabase_auth_error_class	function	lines 313-315	none-found	supabase_auth errors AuthError importlib	src/mindroom/api/auth.py:318
_validate_supabase_token	function	lines 318-331	none-found	validate supabase token auth get_user AuthError	src/mindroom/api/auth.py:377; src/mindroom/api/auth.py:551
bind_authenticated_request_snapshot	function	lines 334-363	related-only	bind request snapshot auth_state store_request_snapshot config_lock	src/mindroom/api/config_lifecycle.py:37; src/mindroom/api/config_lifecycle.py:107; src/mindroom/api/auth.py:127
request_auth_state	function	lines 366-374	related-only	request snapshot auth_state fallback app_auth_state	src/mindroom/api/config_lifecycle.py:37; src/mindroom/api/auth.py:424
request_has_frontend_access	function	lines 377-408	related-only	frontend access trusted upstream supabase api key cookie	src/mindroom/api/frontend.py:53; src/mindroom/api/auth.py:551
sanitize_next_path	function	lines 411-415	none-found	next path startswith slash double slash redirect	src/mindroom/api/frontend.py:54; src/mindroom/api/auth.py:424
_request_path_with_query	function	lines 418-421	none-found	request url path query format path with query	src/mindroom/api/auth.py:424; src/mindroom/api/frontend.py:54
login_redirect_for_request	function	lines 424-435	none-found	login redirect redirect_to platform login next path	src/mindroom/api/oauth.py:95; src/mindroom/api/frontend.py:55
_render_standalone_login_page	function	lines 438-548	none-found	render standalone login page html api key nextPath	src/mindroom/api/auth.py:635
verify_user	async_function	lines 551-603	related-only	verify user trusted upstream standalone supabase request scope auth_user	src/mindroom/api/auth.py:377; src/mindroom/api/credentials.py:195; src/mindroom/api/oauth.py:95
create_auth_session	async_function	lines 607-624	none-found	create auth session set_cookie mindroom_api_key	src/mindroom/api/auth.py:377; src/mindroom/api/frontend.py:53
clear_auth_session	async_function	lines 628-631	none-found	clear auth session delete_cookie	src/mindroom/api/auth.py:607
standalone_login	async_function	lines 635-644	none-found	standalone login HTMLResponse login form next path	src/mindroom/api/frontend.py:53; src/mindroom/api/auth.py:424
```

# Findings

## 1. Bearer token parsing is duplicated

`src/mindroom/api/auth.py:176` extracts a bearer token by checking `authorization.startswith("Bearer ")`, removing the prefix, stripping the remainder, and treating an empty token as missing.

`src/mindroom/api/openai_compat.py:578` repeats the same header-shape check and `removeprefix("Bearer ").strip()` extraction before validating OpenAI-compatible API keys.

The behavior is functionally the same for valid and whitespace-only bearer tokens.
The only meaningful difference is error handling: `api/auth.py` returns `None`, while `api/openai_compat.py` returns an OpenAI-style JSON error when the header is absent or malformed.
That difference can be preserved by sharing only the parser, not the response policy.

## 2. Blank-string runtime/config normalization is related but not an immediate dedupe target

`src/mindroom/api/auth.py:105` reads one runtime env var, strips it, and returns `None` for missing or blank values.
Similar normalization exists at `src/mindroom/oauth/providers.py:124`, `src/mindroom/constants.py:776`, `src/mindroom/tool_system/metadata.py:1208`, and other env/config call sites.

These are related because they all collapse blank strings after stripping.
They are not exact duplicates:

- `oauth/providers.py:124` accepts multiple env names and currently returns `""` if a configured env var exists but contains only whitespace.
- `constants.py:776` resolves a nonblank env value to a path relative to the runtime config directory.
- `tool_system/metadata.py:1208` validates typed tool metadata fields, not runtime env.
- `api/auth.py:208` performs the same blank-string collapse for a configured HTTP header, not env.

The shared primitive would be small, but unifying it broadly would touch unrelated behavior and may accidentally change whitespace handling.

## 3. Auth snapshot caching has structural overlap with config lifecycle but auth-specific behavior is local

`src/mindroom/api/auth.py:127` and `src/mindroom/api/auth.py:334` both cache or bind `ApiAuthState` on top of `ApiSnapshot`.
They use `config_lifecycle.require_api_state`, compare `runtime_paths`, rebuild auth settings and Supabase client when stale, and write the new auth state back into the snapshot.

The surrounding snapshot primitives live in `src/mindroom/api/config_lifecycle.py:57` and `src/mindroom/api/config_lifecycle.py:107`.
This is related, not a strong duplicate, because the auth module owns the `auth_state` rebuild criteria and request binding behavior.
Extracting a generic snapshot extension mechanism would be broader than this module needs.

## 4. Frontend access and API verification share auth branches inside the same module

`src/mindroom/api/auth.py:377` and `src/mindroom/api/auth.py:551` both evaluate trusted-upstream auth, standalone API-key auth, and Supabase auth.
They differ intentionally:

- `request_has_frontend_access` returns a boolean for browser UI gating and suppresses client-side auth failures.
- `verify_user` raises HTTP exceptions for API access and supports standalone public callback paths.
- `verify_user` returns and stores the authenticated user in more branches.

This is same-module duplication, but the task asks for duplicated behavior elsewhere in `./src`.
It is not counted as a cross-source finding.

# Proposed Generalization

1. Move the bearer parser to a tiny shared API helper, for example `src/mindroom/api/auth_tokens.py` with `extract_bearer_token(authorization: str | None) -> str | None`.
2. Replace `api/auth.py:_extract_bearer_token` with the shared helper or a private alias.
3. Use the same helper in `api/openai_compat.py:_authenticate_request`, preserving the existing OpenAI-style error response when the helper returns `None`.
4. Do not generalize blank-string env/header normalization yet unless a dedicated cleanup touches those call sites together.
5. Do not extract auth snapshot caching without a second consumer that needs the same auth-state rebuild semantics.

# Risk/tests

For the bearer parser, risk is low if the helper preserves the current case-sensitive `Bearer ` prefix, whitespace trimming, and empty-token rejection.
Tests should cover dashboard auth token extraction and OpenAI-compatible API authorization with missing, malformed, valid, and whitespace-only bearer tokens.

For env/header normalization, risk is medium because related call sites differ on whether whitespace-only env values become `None`, `""`, or caller-specific defaults.
No refactor is recommended without explicit tests for each affected env variable.

For auth snapshot caching, risk is medium-high because stale `RuntimePaths` comparisons and request-bound snapshots affect hot reload and per-request consistency.
No refactor is recommended.
