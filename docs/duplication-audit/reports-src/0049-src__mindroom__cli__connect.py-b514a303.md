Summary: The top duplication candidate is provisioning HTTP response handling across `src/mindroom/cli/connect.py`, `src/mindroom/matrix/provisioning.py`, and `src/mindroom/matrix/users.py`.
The `.env` update and namespace parsing code have related behavior elsewhere, but the semantics differ enough that I do not recommend immediate consolidation.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_PairCompleteResult	class	lines 24-32	related-only	provisioning result dataclass pair complete register result	src/mindroom/matrix/provisioning.py:64; tests/test_cli_connect.py:42
is_valid_pair_code	function	lines 35-37	none-found	pair code regex ABCD-EFGH is_valid_pair_code _PAIR_CODE_RE	none
complete_local_pairing	function	lines 40-96	duplicate-found	provisioning HTTP post response json required fields error detail	src/mindroom/matrix/provisioning.py:72; src/mindroom/matrix/users.py:272; tests/test_cli_connect.py:119
persist_local_provisioning_env	function	lines 99-126	related-only	env file write MINDROOM_PROVISIONING_URL dotenv_values append missing env defaults	src/mindroom/cli/config.py:156; src/mindroom/cli/config.py:187; src/mindroom/constants.py:248
replace_owner_placeholders_in_config	function	lines 129-142	related-only	replace owner placeholders parse_owner_matrix_user_id replace_owner_placeholders_in_text	src/mindroom/cli/owner.py:13; src/mindroom/cli/owner.py:23; src/mindroom/cli/config.py:118; src/mindroom/cli/config.py:408
_required_non_empty_string	function	lines 145-153	related-only	required string dict field isinstance str strip missing field	src/mindroom/approval_events.py:118; src/mindroom/matrix/provisioning.py:129; src/mindroom/api/auth.py:105
_parse_namespace	function	lines 156-165	duplicate-found	namespace normalize validate regex lower strip MINDROOM_NAMESPACE	src/mindroom/matrix_identifiers.py:10; src/mindroom/matrix_identifiers.py:13; src/mindroom/constants.py:815
_derive_namespace	function	lines 168-170	none-found	sha256 namespace derive client_id hexdigest first 8	none
_extract_error_detail	function	lines 173-187	duplicate-found	response json detail error response text unknown error httpx	src/mindroom/matrix/users.py:272; src/mindroom/matrix/provisioning.py:105
_upsert_env_var	function	lines 190-198	related-only	upsert env var export KEY=value preserve lines append defaults dotenv	src/mindroom/cli/config.py:187; src/mindroom/constants.py:248
```

## Findings

1. Provisioning HTTP response parsing is duplicated.
`complete_local_pairing()` in `src/mindroom/cli/connect.py:40` and `register_user_via_provisioning_service()` in `src/mindroom/matrix/provisioning.py:72` both build a provisioning endpoint, perform an `httpx` request with timeout and TLS verification, translate `httpx.HTTPError` into a user-facing `ValueError`, reject unsuccessful responses, parse JSON, require an object payload, and validate required string fields.
`src/mindroom/matrix/users.py:272` contains the same compact HTTP-error-detail extraction shape: start from `response.text.strip() or "unknown error"`, try `response.json()`, then prefer a structured JSON field.
Differences to preserve: pair completion is synchronous and reads JSON `detail`; agent registration is async, has special permanent startup errors for 401/403/404, and reads plaintext detail today; Matrix user registration reads Matrix-style `error` and `errcode`.

2. Namespace normalization is duplicated with different failure semantics.
`_parse_namespace()` in `src/mindroom/cli/connect.py:156` and `_normalize_namespace()` in `src/mindroom/matrix_identifiers.py:13` both strip, lowercase, reject empty values, and validate against the same lowercase alphanumeric 4-32 character rule.
The regex constants are effectively the same: `src/mindroom/cli/connect.py:20` and `src/mindroom/matrix_identifiers.py:10`.
Differences to preserve: CLI pairing treats invalid remote namespace values as soft failures and derives a fallback; runtime Matrix identifier generation raises `ValueError` for invalid configured `MINDROOM_NAMESPACE`.

3. `.env` persistence is related but not a strong duplication candidate.
`persist_local_provisioning_env()` in `src/mindroom/cli/connect.py:99` and `_append_missing_env_defaults()` in `src/mindroom/cli/config.py:187` both preserve existing `.env` content while adding generated key-value entries.
The connect helper replaces existing keys, accepts optional `export`, and writes secrets; config init only appends missing public defaults based on `dotenv_values()` and deliberately avoids replacing user-owned values.
Those different ownership rules make a shared helper possible but not compelling.

## Proposed Generalization

1. Add a small provisioning response helper in `src/mindroom/matrix/provisioning.py` or a new focused module such as `src/mindroom/provisioning_http.py`.
It should expose pure functions for `extract_http_error_detail(response, json_keys=("detail",))`, `response_json_object(response, context=...)`, and `required_response_string(body, key, context=...)`.

2. Update `complete_local_pairing()` and `register_user_via_provisioning_service()` to use the shared JSON-object and required-string helpers while keeping their current exception messages and permanent startup-error branches.

3. Add a namespace helper that can either return `None` or raise based on caller policy, or move only the shared regex constant to one place.
Given the different failure semantics, sharing only the validation predicate is likely the safest first step.

4. Leave `.env` writing alone unless a third caller appears.
Current callers have different overwrite/append rules, and a shared abstraction would mostly parameterize policy rather than remove meaningful complexity.

## Risk/Tests

The provisioning refactor risk is subtle message drift in CLI and startup errors.
Tests to run or add would include `tests/test_cli_connect.py`, provisioning registration coverage in `tests/test_matrix_agent_manager.py`, and Matrix registration error-detail tests around `src/mindroom/matrix/users.py`.

The namespace refactor risk is turning a soft pairing fallback into a hard runtime error or vice versa.
Tests should cover malformed pairing namespaces, valid runtime namespaces, and invalid `MINDROOM_NAMESPACE` failures through `tests/test_cli_connect.py` and `tests/test_config_discovery.py`.

No refactor is recommended for `.env` persistence until more callers need identical upsert semantics.
