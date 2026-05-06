## Summary

Top duplication candidates for `src/mindroom/matrix/identity.py` are Matrix user ID shape validation split across onboarding/invite helpers, Matrix localpart validation split between current user IDs and `mindroom_user.username`, and managed account key construction repeated between identity resolution and Matrix user persistence.
The remaining symbols are either simple constructors/properties or local implementation details with related call sites but no meaningful duplicated behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MatrixID	class	lines 34-88	related-only	MatrixID parse full_id agent_name from_username from_agent	src/mindroom/matrix/users.py:36, src/mindroom/entity_resolution.py:48, src/mindroom/config/main.py:908, src/mindroom/thread_utils.py:124, src/mindroom/authorization.py:188
MatrixID.parse	method	lines 43-45	related-only	MatrixID.parse startswith @ colon parse user_id	src/mindroom/matrix/users.py:36, src/mindroom/matrix/room_cleanup.py:117, src/mindroom/thread_utils.py:112, src/mindroom/authorization.py:195
MatrixID.from_agent	method	lines 48-55	related-only	from_agent agent_username_localpart MatrixID	src/mindroom/entity_resolution.py:48, src/mindroom/bot.py:201, src/mindroom/agents.py:1092
MatrixID.from_username	method	lines 58-60	related-only	from_username full_id user_id server_name	src/mindroom/entity_resolution.py:61, src/mindroom/matrix/users.py:504, src/mindroom/matrix/users.py:684, src/mindroom/orchestrator.py:1555
MatrixID.full_id	method	lines 63-65	related-only	full_id f\"@{ username domain MatrixID	src/mindroom/matrix/users.py:504, src/mindroom/entity_resolution.py:61, src/mindroom/matrix/identity.py:287
MatrixID.agent_name	method	lines 67-84	related-only	agent_name managed_account_usernames active managed agent names	src/mindroom/thread_utils.py:106, src/mindroom/authorization.py:181, src/mindroom/matrix/room_cleanup.py:117, src/mindroom/matrix/stale_stream_cleanup.py:1436
MatrixID.__str__	method	lines 86-88	not-a-behavior-symbol	__str__ MatrixID full_id	none
_ThreadStateKey	class	lines 92-114	none-found	ThreadStateKey state_key thread_id agent_name split colon	none
_ThreadStateKey.parse	method	lines 99-105	none-found	state_key split thread_id agent_name Invalid state key	none
_ThreadStateKey.key	method	lines 108-110	none-found	thread_id agent_name state key f colon	none
_ThreadStateKey.__str__	method	lines 112-114	not-a-behavior-symbol	__str__ ThreadStateKey key	none
_parse_matrix_id	function	lines 118-141	duplicate-found	Matrix user id startswith @ colon split parse owner concrete	src/mindroom/cli/owner.py:10, src/mindroom/orchestration/rooms.py:16, src/mindroom/matrix/users.py:36, src/mindroom/thread_utils.py:21
parse_current_matrix_user_id	function	lines 144-151	duplicate-found	current matrix user localpart validation lowercase allowed chars	src/mindroom/config/matrix.py:27, src/mindroom/config/matrix.py:42
parse_historical_matrix_user_id	function	lines 154-158	related-only	historical matrix user id parse tolerant validation	src/mindroom/api/auth.py:268, src/mindroom/api/credentials.py:195
try_parse_historical_matrix_user_id	function	lines 161-168	related-only	try parse Matrix user id nullable ValueError None	src/mindroom/api/auth.py:268, src/mindroom/api/credentials.py:195
_validate_matrix_user_id_common	function	lines 171-185	related-only	Matrix ID common validation surrogate server name length	src/mindroom/cli/owner.py:10, src/mindroom/orchestration/rooms.py:16
_contains_surrogate	function	lines 188-189	none-found	surrogate ord D800 DFFF UnicodeEncodeError Matrix	none
_valid_current_server_name	function	lines 192-204	related-only	server_name host port bracketed ipv6 Matrix grammar	src/mindroom/matrix_identifiers.py:75
_valid_unbracketed_server_host	function	lines 207-210	none-found	server DNS label pattern host split dot	none
_valid_bracketed_ipv6_server_name	function	lines 213-224	none-found	bracketed IPv6 server name IPv6Address	none
_split_bracketed_server_name	function	lines 227-237	none-found	split bracketed server name closing bracket remainder	none
_valid_port	function	lines 240-241	none-found	port isdecimal 65535 Matrix server	none
is_agent_id	function	lines 244-246	related-only	is_agent_id extract_agent_name active internal sender	src/mindroom/response_runner.py:229, src/mindroom/tool_approval.py:188, src/mindroom/interactive.py:481, src/mindroom/turn_controller.py:420
extract_agent_name	function	lines 249-260	related-only	extract_agent_name sender_id MatrixID agent_name	src/mindroom/thread_utils.py:55, src/mindroom/conversation_resolver.py:151, src/mindroom/edit_regenerator.py:131, src/mindroom/matrix/stale_stream_cleanup.py:1436
_active_managed_account_keys	function	lines 263-270	duplicate-found	agent_ account_key router agents teams mindroom_user	src/mindroom/matrix/users.py:27, src/mindroom/matrix/users.py:32, src/mindroom/matrix/room_cleanup.py:37
_active_managed_agent_account_names	function	lines 273-278	duplicate-found	active managed agent account names router agents teams	src/mindroom/entity_resolution.py:48, src/mindroom/entity_resolution.py:18, src/mindroom/matrix/users.py:27
_configured_active_account_sender_ids	function	lines 281-294	related-only	configured sender ids MatrixID.from_agent mindroom_user_id	src/mindroom/entity_resolution.py:48, src/mindroom/entity_resolution.py:61
active_internal_sender_ids	function	lines 297-309	related-only	active internal sender ids trusted visible sender ids cleanup trusted	src/mindroom/matrix/client_visible_messages.py:135, src/mindroom/authorization.py:82, src/mindroom/matrix/stale_stream_cleanup.py:1047, src/mindroom/matrix/conversation_cache.py:412
```

## Findings

1. Matrix user ID shape validation is repeated with weaker local checks.

`_parse_matrix_id()` and the public `parse_*_matrix_user_id()` helpers provide canonical Matrix user ID parsing and validation in `src/mindroom/matrix/identity.py:118`, `src/mindroom/matrix/identity.py:144`, and `src/mindroom/matrix/identity.py:154`.
Similar concrete-user checks appear in `src/mindroom/cli/owner.py:10` and `src/mindroom/orchestration/rooms.py:16`.
`src/mindroom/matrix/users.py:36` also duplicates the initial `startswith("@") and ":"` shape check before delegating to `MatrixID.parse()`, returning `"localhost"` on invalid input.
These all classify user IDs as Matrix IDs, but they differ in strictness and fallback behavior.
The onboarding and invite helpers currently accept values that identity validation would reject, such as malformed server names or overlong IDs.

2. Current Matrix localpart validation is duplicated with a drift.

`parse_current_matrix_user_id()` validates the current Matrix user localpart with `_CURRENT_USER_LOCALPART_PATTERN` in `src/mindroom/matrix/identity.py:18` and `src/mindroom/matrix/identity.py:144`.
`MindRoomUserConfig.validate_username()` validates a configured localpart separately with `_MATRIX_LOCALPART_PATTERN` in `src/mindroom/config/matrix.py:27` and `src/mindroom/config/matrix.py:42`.
Both enforce a Matrix localpart grammar for current locally-created accounts, but the config pattern omits `+`, while the identity pattern accepts it.
That may be intentional for internal account usernames, but the duplicated grammar should be made explicit if preserved.

3. Managed Matrix account key derivation is repeated.

`_active_managed_account_keys()` and `_active_managed_agent_account_names()` build persisted account keys like `agent_<name>` in `src/mindroom/matrix/identity.py:263` and `src/mindroom/matrix/identity.py:273`.
`src/mindroom/matrix/users.py:27` defines `_account_key_for_agent()` with the same key format and exports `INTERNAL_USER_ACCOUNT_KEY` as `agent_user` at `src/mindroom/matrix/users.py:32`.
`src/mindroom/matrix/room_cleanup.py:37` then filters persisted account keys and imports `INTERNAL_USER_ACCOUNT_KEY`.
The behavior is the same persisted-key convention, but the source of truth is split across identity and user provisioning modules.

4. Agent MatrixID collection from candidate user IDs is related but not a clear dedupe target.

`MatrixID.agent_name()` is the canonical membership/classification primitive in `src/mindroom/matrix/identity.py:67`.
`src/mindroom/thread_utils.py:124` and `src/mindroom/authorization.py:188` both parse user IDs, call `agent_name()`, exclude or include router depending on context, and return sorted or insertion-ordered `MatrixID` lists.
The shared operation is recognizable, but ordering, router treatment, permission filtering, and malformed-ID assumptions differ enough that a helper would need parameters and may not reduce complexity.

## Proposed Generalization

1. Prefer reusing `try_parse_historical_matrix_user_id()` or `parse_current_matrix_user_id()` in owner and invite filtering code where strict canonical Matrix user IDs are desired.
Keep a local wildcard/placeholder filter only for `*`, `?`, and spaces if those are config-specific invite concerns.

2. If `mindroom_user.username` should use the same current localpart grammar as Matrix user IDs, move the localpart regex/validator to one exported helper in `matrix.identity` or a small identifier-validation module.
If `+` should remain disallowed for configured internal usernames, document that narrower policy and leave it separate.

3. Move persisted account-key construction to one tiny helper that is importable by both `matrix.identity` and `matrix.users`, or expose the existing `_account_key_for_agent()` from a lower-level module without creating a dependency cycle.
Use that helper in `_active_managed_account_keys()` and `_active_managed_agent_account_names()`.

No refactor is recommended for `_ThreadStateKey`, server-name internals, port validation, or agent-list collection at this point.

## Risk/tests

Changing user ID validation can reject previously accepted loose config values, especially owner IDs and authorization invite lists.
Tests should cover valid Matrix IDs, malformed domains, wildcard invite placeholders, historical user IDs, and current localpart edge cases including `+`.
Changing account-key helpers is low behavior risk but should be covered by Matrix state/provisioning tests that verify router, agents, teams, and `agent_user` still resolve to the same persisted keys.
