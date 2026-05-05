## Summary

Top duplication candidates:

1. Internal Matrix account resolution is repeated in `src/mindroom/matrix/users.py`, `src/mindroom/matrix/rooms.py`, `src/mindroom/orchestrator.py`, and `src/mindroom/matrix/identity.py`.
2. Password login plus display-name synchronization in `src/mindroom/matrix/users.py` partially overlaps with `src/mindroom/matrix/client_session.py` login behavior.
3. Provisioning and direct HTTP registration both contain local HTTP error-detail extraction, but the response shapes and permanent-error policy differ enough that only a small helper would be justified.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_account_key_for_agent	function	lines 27-29	related-only	agent_ account key managed_account_usernames INTERNAL_USER_ACCOUNT_KEY	src/mindroom/matrix/state.py:122; src/mindroom/matrix/identity.py:303; src/mindroom/orchestrator.py:1546
_extract_domain_from_user_id	function	lines 36-40	related-only	MatrixID.parse domain invalid Matrix ID localhost	src/mindroom/matrix/identity.py:42; src/mindroom/matrix/identity.py:117; src/mindroom/matrix_identifiers.py:75
AgentMatrixUser	class	lines 44-57	related-only	AgentMatrixUser create_agent_user login_agent_user dataclass user_id password device_id access_token	src/mindroom/bot.py:56; src/mindroom/api/matrix_operations.py:89; src/mindroom/api/schedules.py:238
AgentMatrixUser.matrix_id	method	lines 55-57	related-only	MatrixID.parse user_id matrix_id property	src/mindroom/bot.py:624; src/mindroom/matrix/identity.py:42
_get_agent_credentials	function	lines 60-84	duplicate-found	matrix_state_for_runtime get_account agent_ username password device_id access_token	src/mindroom/matrix/state.py:122; src/mindroom/orchestrator.py:1546; src/mindroom/matrix/rooms.py:549; src/mindroom/avatar_generation.py:446
_save_agent_credentials	function	lines 87-122	related-only	MatrixState.load add_account save extract_server_name_from_homeserver agent_credentials_saved	src/mindroom/matrix/state.py:69; src/mindroom/matrix/rooms.py:196; src/mindroom/matrix/rooms.py:458
_persist_agent_session	function	lines 125-148	related-only	persist session device_id access_token save_agent_credentials restore_login	src/mindroom/matrix/client_session.py:135; src/mindroom/matrix/users.py:711
_homeserver_requires_registration_token	async_function	lines 151-176	none-found	m.login.registration_token _matrix/client/v3/register flows stages httpx.AsyncClient	none
_registration_failure_message	async_function	lines 179-202	related-only	Invalid registration token M_FORBIDDEN unknown error requires registration token	matrix/mindroom/users.py:292; src/mindroom/matrix/client_session.py:45
_register_user_with_token	async_function	lines 205-269	related-only	register_with_token MATRIX_REGISTRATION_TOKEN direct token UIAA M_USER_IN_USE	src/mindroom/matrix/provisioning.py:72; src/mindroom/matrix/users.py:307
_registration_http_error_details	function	lines 272-289	duplicate-found	response.text response.json errcode error unknown error provisioning HTTP detail	src/mindroom/matrix/provisioning.py:105; src/mindroom/api/credentials.py:1151
_direct_token_registration_error	function	lines 292-304	related-only	M_FORBIDDEN Invalid registration token M_INVALID_USERNAME matrix_startup_error permanent	src/mindroom/matrix/client_session.py:20; src/mindroom/matrix/client_session.py:45
_register_user_with_token_via_nio	async_function	lines 307-339	related-only	matrix_client register_with_token handle_register_response	src/mindroom/matrix/users.py:591; src/mindroom/matrix/client_session.py:100
_register_user_with_token_via_nio.<locals>._register_with_client	nested_async_function	lines 319-336	related-only	register_with_token register handle_register_response nested client	src/mindroom/matrix/users.py:602; src/mindroom/matrix/client_session.py:100
_account_collision_error	function	lines 342-347	related-only	Matrix account collision M_USER_IN_USE login failed permanent startup error	src/mindroom/matrix/client_session.py:45; src/mindroom/matrix/users.py:390; src/mindroom/matrix/users.py:411
_login_and_sync_display_name	async_function	lines 350-366	duplicate-found	client.login set_displayname LoginResponse matrix_login_succeeded	src/mindroom/matrix/client_session.py:116; src/mindroom/matrix/rooms.py:559
_login_existing_user	async_function	lines 369-387	duplicate-found	matrix_client login password display_name close client src/mindroom/matrix/client_session.py:100	src/mindroom/matrix/client_session.py:116; src/mindroom/matrix/rooms.py:559
_login_existing_user.<locals>._login_with_client	nested_async_function	lines 379-384	related-only	nested _login_with_client login_and_sync_display_name matrix_client	src/mindroom/matrix/users.py:319; src/mindroom/matrix/users.py:602
_login_existing_user_or_raise_collision	async_function	lines 390-408	related-only	login existing raise account collision M_USER_IN_USE	src/mindroom/matrix/users.py:411; src/mindroom/matrix/client_session.py:116
_login_existing_user_with_client_or_raise_collision	async_function	lines 411-426	related-only	login with client raise account collision set display name	src/mindroom/matrix/users.py:390; src/mindroom/matrix/users.py:350
_handle_register_response	async_function	lines 429-478	related-only	RegisterResponse ErrorResponse M_USER_IN_USE set_displayname matrix_startup_error	src/mindroom/matrix/client_session.py:116; src/mindroom/matrix/users.py:350
_register_user	async_function	lines 481-536	related-only	register user MatrixID.from_username extract_server_name_from_homeserver registration_token provisioning	src/mindroom/matrix/provisioning.py:72; src/mindroom/orchestrator.py:394
_register_user_via_provisioning_if_configured	async_function	lines 539-588	related-only	provisioning_url local client credentials register_user_via_provisioning_service user_in_use	src/mindroom/matrix/provisioning.py:44; src/mindroom/matrix/provisioning.py:72
_register_user_without_token	async_function	lines 591-621	related-only	matrix_client register device_name handle_register_response	src/mindroom/matrix/users.py:307; src/mindroom/matrix/client_session.py:100
_register_user_without_token.<locals>._register_with_client	nested_async_function	lines 602-618	related-only	client.register handle_register_response nested client	src/mindroom/matrix/users.py:319; src/mindroom/matrix/client_session.py:100
create_agent_user	async_function	lines 624-708	related-only	create or retrieve Matrix user credentials generated saved registration	internal calls only; src/mindroom/bot.py:877; src/mindroom/orchestrator.py:394; src/mindroom/api/matrix_operations.py:89; src/mindroom/api/schedules.py:238
login_agent_user	async_function	lines 711-765	related-only	restore_login login persist_agent_session AgentMatrixUser	src/mindroom/matrix/client_session.py:116; src/mindroom/matrix/client_session.py:135; src/mindroom/matrix/rooms.py:559
_ensure_all_agent_users	async_function	lines 769-831	related-only	ensure all agent users router agents teams create_agent_user	src/mindroom/orchestrator.py:383; src/mindroom/bot.py:867; tests/test_router_rooms.py:233
```

## Findings

### Duplicate account-to-user-id resolution

`src/mindroom/matrix/users.py` has `_get_agent_credentials()` and `_save_agent_credentials()` around Matrix state account keys.
The same persisted-account lookup and username-to-full-ID reconstruction appears elsewhere for the internal user:

- `src/mindroom/orchestrator.py:1546` loads `matrix_state_for_runtime()`, gets `INTERNAL_USER_ACCOUNT_KEY`, derives `server_name`, then builds `MatrixID.from_username(...).full_id`.
- `src/mindroom/matrix/rooms.py:549` repeats the same state lookup, account guard, server-name extraction, and full user ID construction.
- `src/mindroom/matrix/identity.py:301` uses `managed_account_usernames()` and `MatrixID.from_username()` to reconstruct active internal sender IDs.

The behavior is duplicated because each caller independently knows how persisted account usernames become current-runtime Matrix IDs.
Differences to preserve: `rooms.py` logs and returns when the internal account is missing, while `orchestrator.py` silently returns authorized IDs unchanged.

### Duplicate login flow with local display-name synchronization

`src/mindroom/matrix/users.py:350` logs in with `client.login(password)` and then calls `client.set_displayname(display_name)` on success.
`src/mindroom/matrix/client_session.py:116` performs the shared password login and startup-error handling, and `src/mindroom/matrix/rooms.py:559` has another direct `matrix_client()` plus `client.login()` path for the internal user joining rooms.

The behavior is not identical because `_login_and_sync_display_name()` intentionally returns the raw nio login response instead of raising, so registration collision handling can distinguish `M_USER_IN_USE` from a failed password login.
Still, password login mechanics and successful-login logging/cleanup are split between helpers.
Differences to preserve: registration collision paths must keep access to `nio.LoginError`; regular bot login should continue raising via `matrix_startup_error`; room membership login currently logs and skips instead of raising.

### Repeated HTTP error detail parsing

`src/mindroom/matrix/users.py:272` extracts response text, optional JSON `errcode`, and optional JSON `error`.
`src/mindroom/matrix/provisioning.py:105` also derives text detail from a failed `httpx.Response`, but it does not need Matrix `errcode`.

This is a small duplication of response-detail extraction, not a full registration-flow duplicate.
Differences to preserve: provisioning maps HTTP 401/403/404 to provisioning-specific permanent messages, while Matrix registration maps Matrix errcodes such as `M_USER_IN_USE`, `M_FORBIDDEN`, and `M_INVALID_USERNAME`.

## Proposed Generalization

Add one focused helper in `src/mindroom/matrix/state.py` or a small sibling module such as `src/mindroom/matrix/accounts.py`:

- `managed_account(runtime_paths, account_key)` returning the persisted account or `None`.
- `managed_account_user_id(runtime_paths, account_key, homeserver)` returning the current-runtime full Matrix user ID or `None`.

Use it only for the repeated internal-user resolution sites first.
No broad registration refactor is recommended.
For login behavior, consider a later narrow enhancement to `matrix.client_session.login()` that accepts an optional successful-login hook for display-name sync only if it can preserve the raw-response collision paths.

## Risk/tests

Risks:

- Account ID resolution must preserve current-domain behavior when persisted usernames drift from configured usernames.
- Missing internal-user account handling differs by caller and should not be collapsed into one mandatory exception path.
- Login refactoring could accidentally turn collision detection into a generic startup failure.

Tests to check for any future refactor:

- `tests/test_matrix_agent_manager.py` registration, existing-user, collision, and provisioning tests.
- `tests/test_cli.py` internal user creation and display-name behavior.
- `tests/test_matrix_identity.py` managed account sender ID tests.
- Room membership tests covering `ensure_user_in_rooms()` if present or added.
