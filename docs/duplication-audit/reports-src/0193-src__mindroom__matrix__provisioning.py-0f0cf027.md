## Summary

Top duplication candidate: `register_user_via_provisioning_service` repeats the provisioning HTTP call and response-shape validation already used by CLI pairing in `src/mindroom/cli/connect.py`.
The overlap is real but endpoint-specific enough that a broad abstraction is not recommended yet.
The environment readers and result dataclass are related to nearby configuration and Matrix registration flows, but no meaningful duplicated behavior was found for them.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
provisioning_url_from_env	function	lines 14-17	related-only	MINDROOM_PROVISIONING_URL env_value strip rstrip; provisioning_url rstrip	src/mindroom/cli/main.py:281; src/mindroom/cli/connect.py:115; src/mindroom/cli/config.py:88; src/mindroom/config/matrix.py:261
registration_token_from_env	function	lines 20-23	related-only	MATRIX_REGISTRATION_TOKEN env_value strip; registration_token	src/mindroom/cli/config.py:89; src/mindroom/matrix/users.py:506; src/mindroom/matrix/users.py:519
_local_provisioning_client_credentials_from_env	function	lines 26-41	related-only	MINDROOM_LOCAL_CLIENT_ID MINDROOM_LOCAL_CLIENT_SECRET env_value incomplete credentials; client_id client_secret strip	src/mindroom/cli/connect.py:99; src/mindroom/cli/main.py:332; src/mindroom/oauth/providers.py:347; src/mindroom/api/integrations.py:152
required_local_provisioning_client_credentials_for_registration	function	lines 44-61	none-found	provisioning_url registration_token required credentials registration; MINDROOM_PROVISIONING_URL missing credentials	src/mindroom/matrix/users.py:539; src/mindroom/matrix/users.py:506; src/mindroom/matrix/users.py:519
_ProvisioningRegisterResult	class	lines 65-69	not-a-behavior-symbol	dataclass frozen status Literal created user_in_use user_id result	src/mindroom/cli/connect.py:23; src/mindroom/custom_tools/matrix_conversation_operations.py:49; src/mindroom/knowledge/registry.py:69
register_user_via_provisioning_service	async_function	lines 72-138	duplicate-found	register-agent provisioning service httpx post timeout verify invalid JSON status user_id response detail	src/mindroom/cli/connect.py:40; src/mindroom/cli/connect.py:145; src/mindroom/cli/connect.py:173; src/mindroom/matrix/users.py:205; src/mindroom/matrix/users.py:272; src/mindroom/codex_model.py:160
```

## Findings

### Provisioning HTTP request and response validation overlaps with CLI pairing

`src/mindroom/matrix/provisioning.py:72` and `src/mindroom/cli/connect.py:40` both call the hosted provisioning service with `httpx`, use a 10-second timeout, normalize the provisioning base URL before appending a `/v1/local-mindroom/...` path, turn transport failures into user-facing exceptions, reject non-success responses, parse JSON, require a JSON object, and validate required string fields in the returned body.

The duplicated behavior is not literal line-for-line duplication because the registration flow is async, uses custom local-client headers, maps some HTTP statuses to `matrix_startup_error`, and accepts `status` values of `created` or `user_in_use`.
The CLI pairing flow is sync, posts pair metadata, extracts JSON/plaintext error details with `_extract_error_detail`, and validates `client_id`, `client_secret`, namespace, and owner fields.

There is a smaller helper-level duplicate between `src/mindroom/matrix/provisioning.py:129` to `src/mindroom/matrix/provisioning.py:136` and `src/mindroom/cli/connect.py:145` to `src/mindroom/cli/connect.py:153`: both trim and require non-empty string fields from provisioning JSON.
The current error types and messages differ and should be preserved.

### Environment value normalization is related but not enough to extract

`src/mindroom/matrix/provisioning.py:14`, `src/mindroom/matrix/provisioning.py:20`, and `src/mindroom/matrix/provisioning.py:26` use the common project pattern of reading `RuntimePaths.env_value`, treating missing values as empty strings, and stripping whitespace.
Similar normalization appears in `src/mindroom/config/matrix.py:263` and `src/mindroom/config/matrix.py:268`, and provisioning URL persistence strips trailing slashes in `src/mindroom/cli/connect.py:115`.

This is related behavior, but the exact semantics are small and context-specific: URL values additionally remove trailing slashes, token values preserve internal content and return `None` for empty strings, and local credentials must fail when only one side is present.
No refactor is recommended for these functions.

## Proposed Generalization

No broad refactor recommended.

If this duplication grows, the smallest useful extraction would be a private provisioning helper, probably in `src/mindroom/cli/connect.py` or a new focused module only if both CLI and Matrix provisioning should share it.
It could provide:

1. A `provisioning_endpoint(base_url: str, path: str) -> str` helper that consistently applies `rstrip("/")`.
2. A `required_non_empty_string(data: dict[str, object], key: str, *, message_prefix: str) -> str` helper for validated provisioning response fields.
3. A compact error-detail extractor for provisioning service responses, with callers still deciding which exception type and permanent-startup behavior to use.

Do not merge the sync and async HTTP clients unless more provisioning endpoints appear.
The current call sites have different execution models and error semantics.

## Risk/tests

Any future extraction should preserve:

- `matrix_startup_error(..., permanent=True)` for invalid/revoked local provisioning credentials, unsupported register-agent endpoints, invalid JSON, invalid status, and missing `user_id`.
- CLI `ValueError` and `TypeError` behavior from `complete_local_pairing`.
- Existing URL trailing-slash behavior for both persisted env values and request endpoints.
- The distinction between `created` and `user_in_use` in the register-agent response.

Relevant tests would be focused unit tests for `complete_local_pairing`, `register_user_via_provisioning_service`, and `_register_user_via_provisioning_if_configured`.
No production code was edited for this audit.
