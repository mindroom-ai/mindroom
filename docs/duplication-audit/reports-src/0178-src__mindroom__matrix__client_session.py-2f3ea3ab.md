## Summary

Top duplication candidates for `src/mindroom/matrix/client_session.py`:

1. Password-login response handling is repeated in Matrix user provisioning and user room membership flows, with different cleanup and display-name side effects.
2. Matrix SSL verification is consistently sourced from `runtime_matrix_ssl_verify`, but `client_session.py` has unique nio-specific `SSLContext` construction while other modules pass the boolean directly to `httpx`.
3. Matrix client construction and restored-session validation are centralized in this module; no second `nio.AsyncClient` factory or `whoami` restore validation duplicate was found.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
PermanentMatrixStartupError	class	lines 30-31	related-only	PermanentMatrixStartupError retry_if_not_exception_type matrix startup permanent	src/mindroom/bot.py:1099; src/mindroom/bot.py:1185; src/mindroom/bot.py:1195; src/mindroom/orchestrator.py:34; src/mindroom/orchestration/runtime.py:24
_require_runtime_paths_arg	function	lines 34-42	related-only	requires runtime_paths RuntimePaths omitted positional call TypeError	src/mindroom/runtime_support.py:116; src/mindroom/runtime_support.py:183
matrix_startup_error	function	lines 45-56	related-only	matrix_startup_error ErrorResponse status_code M_FORBIDDEN M_UNKNOWN_TOKEN permanent	src/mindroom/matrix/provisioning.py:40; src/mindroom/matrix/provisioning.py:109; src/mindroom/matrix/users.py:235; src/mindroom/matrix/users.py:300; src/mindroom/matrix/users.py:347; src/mindroom/matrix/users.py:478
_maybe_ssl_context	function	lines 59-68	related-only	runtime_matrix_ssl_verify create_default_context CERT_NONE check_hostname verify=	src/mindroom/matrix/provisioning.py:96; src/mindroom/matrix/users.py:158; src/mindroom/matrix/users.py:228; src/mindroom/orchestration/runtime.py:368; src/mindroom/cli/doctor.py:602
_create_matrix_client	function	lines 71-97	none-found	nio.AsyncClient encryption_keys_dir store_path safe_user_id access_token user_id	src/mindroom/matrix/users.py:338; src/mindroom/matrix/users.py:386; src/mindroom/matrix/rooms.py:559
matrix_client	async_function	lines 101-113	none-found	asynccontextmanager matrix_client client.close nio.AsyncClient cleanup	src/mindroom/matrix/users.py:338; src/mindroom/matrix/users.py:386; src/mindroom/matrix/rooms.py:559; src/mindroom/api/schedules.py:275; src/mindroom/api/matrix_operations.py:117
login	async_function	lines 116-132	duplicate-found	client.login LoginResponse Failed to login client.close matrix_startup_error	src/mindroom/matrix/users.py:350; src/mindroom/matrix/users.py:369; src/mindroom/matrix/users.py:411; src/mindroom/matrix/rooms.py:559
restore_login	async_function	lines 135-154	none-found	restore_login whoami WhoamiResponse access_token device_id client.close	src/mindroom/matrix/users.py:730
```

## Findings

### 1. Password-login response handling is repeated

`login` creates a Matrix client, calls `client.login(password)`, accepts only `nio.LoginResponse`, logs success, closes the client on failure, and raises a startup error at `src/mindroom/matrix/client_session.py:116`.

`_login_and_sync_display_name` repeats the core password-login and `LoginResponse` branch at `src/mindroom/matrix/users.py:350`, then adds display-name synchronization on success.
`_login_existing_user` wraps that login in `matrix_client` at `src/mindroom/matrix/users.py:369`, while `_login_existing_user_with_client_or_raise_collision` repeats the same response check and raises permanent collision errors at `src/mindroom/matrix/users.py:411`.

`ensure_user_room_membership` also opens `matrix_client`, calls `user_client.login(password=...)`, checks `LoginResponse`, logs failure, and returns without raising at `src/mindroom/matrix/rooms.py:559`.

These are functionally similar because they all implement "attempt Matrix password login and branch on `nio.LoginResponse`".
They differ in important behavior:

- `client_session.login` owns the client and must close it on failure.
- User provisioning must sync display names after successful login and must convert failures into permanent account-collision startup errors.
- Room membership should log and skip room joins on login failure instead of raising.

## Proposed Generalization

No immediate refactor recommended.

If this area is touched again, the smallest safe generalization would be a private helper in `src/mindroom/matrix/users.py` or `src/mindroom/matrix/client_session.py` that only performs `response = await client.login(password)` and returns `nio.LoginResponse | nio.LoginError`.
Callers should keep their current ownership, cleanup, display-name sync, and error policy outside the helper.

## Risk/tests

The repeated login branches are startup-sensitive.
Any consolidation would need tests for successful agent login, failed password login closing the client in `client_session.login`, account-collision errors in user provisioning, display-name sync after existing-user login, restored-session fallback in `login_agent_user`, and room-membership login failure logging without raising.

No production code was edited for this audit.
