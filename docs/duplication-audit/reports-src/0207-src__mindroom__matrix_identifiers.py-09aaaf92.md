Summary: Two meaningful duplication candidates were found.
The namespace parser in `src/mindroom/cli/connect.py` repeats the same normalization and regex validation as `src/mindroom/matrix_identifiers.py`, with a different invalid-value policy.
The current-domain helper in `src/mindroom/matrix/state.py` repeats the same homeserver-to-server-name fallback extraction as `extract_server_name_from_homeserver`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_normalize_namespace	function	lines 13-23	duplicate-found	MINDROOM_NAMESPACE namespace strip lower regex ^[a-z0-9]{4,32}$ _NAMESPACE_RE	src/mindroom/cli/connect.py:20, src/mindroom/cli/connect.py:156, src/mindroom/constants.py:815
mindroom_namespace	function	lines 26-28	related-only	runtime_mindroom_namespace mindroom_namespace namespace runtime_paths	src/mindroom/constants.py:815, src/mindroom/config/matrix.py:276, src/mindroom/matrix/mentions.py:312
agent_username_localpart	function	lines 31-36	related-only	mindroom_ agent username localpart AGENT_PREFIX from_agent configured_bots	src/mindroom/matrix/identity.py:40, src/mindroom/matrix/identity.py:48, src/mindroom/matrix/identity.py:76, src/mindroom/config/main.py:861, src/mindroom/matrix/mentions.py:302
managed_room_alias_localpart	function	lines 39-44	none-found	managed_room_alias_localpart room_key namespace alias localpart room alias	src/mindroom/matrix/rooms.py:273, src/mindroom/config/main.py:886, src/mindroom/config/matrix.py:157, src/mindroom/authorization.py:27
managed_space_alias_localpart	function	lines 47-49	none-found	_mindroom_root_space root space alias managed_space_alias_localpart	src/mindroom/matrix/rooms.py:463, src/mindroom/config/main.py:886
managed_room_key_from_alias_localpart	function	lines 52-65	related-only	managed room key from alias localpart suffix namespace invite_only_rooms permission lookup	src/mindroom/config/matrix.py:170, src/mindroom/authorization.py:40
room_alias_localpart	function	lines 68-72	related-only	room alias localpart startswith # split colon canonical_alias	src/mindroom/config/matrix.py:170, src/mindroom/authorization.py:40, src/mindroom/matrix/identity.py:125
extract_server_name_from_homeserver	function	lines 75-83	duplicate-found	runtime_matrix_server_name homeserver split :// split : server_name current_domain	src/mindroom/matrix/state.py:149, src/mindroom/cli/local_stack.py:115, src/mindroom/matrix/users.py:504, src/mindroom/matrix/rooms.py:273
```

Findings:

1. Namespace normalization and validation is duplicated between `src/mindroom/matrix_identifiers.py:13` and `src/mindroom/cli/connect.py:156`.
Both functions accept an optional/raw namespace, strip whitespace, lowercase it, reject empty values, and validate against the same `^[a-z0-9]{4,32}$` rule.
The behavioral difference is important: `_normalize_namespace` raises `ValueError` on malformed non-empty input, while `_parse_namespace` returns `None` so pairing can derive a fallback namespace and mark `namespace_invalid`.

2. Matrix server-name extraction is duplicated between `src/mindroom/matrix_identifiers.py:75` and `src/mindroom/matrix/state.py:149`.
Both prefer `runtime_matrix_server_name(runtime_paths)`, otherwise strip a URL scheme by splitting on `://`, then strip a port by splitting on `:`.
The state helper takes the homeserver from `runtime_matrix_homeserver(runtime_paths)` internally, while `extract_server_name_from_homeserver` accepts a homeserver argument.
The behavior is otherwise the same for the covered string forms.

Related but not counted as duplicate:

- `src/mindroom/matrix/mentions.py:297` strips the `mindroom_` prefix and namespace suffix from mention localparts.
This is inverse/lookup behavior related to `agent_username_localpart`, but it also preserves historical alias matching and display-name resolution rules, so a direct shared helper would need care.
- `src/mindroom/config/matrix.py:157` and `src/mindroom/authorization.py:27` both use `room_alias_localpart` and `managed_room_key_from_alias_localpart` while expanding room identifier lookup keys.
That duplication is in their caller-side identifier aggregation, not in the primary file's helper implementations.

Proposed generalization:

1. Expose a non-raising namespace parser in `matrix_identifiers`, for example `parse_mindroom_namespace(value: object) -> str | None`, and have `_normalize_namespace` call it before raising.
2. Replace `src/mindroom/cli/connect.py:_parse_namespace` with the non-raising parser so the pairing fallback behavior stays intact.
3. Replace `src/mindroom/matrix/state.py:_current_runtime_domain` internals with `extract_server_name_from_homeserver(runtime_matrix_homeserver(runtime_paths), runtime_paths)`.
4. Add focused tests for malformed namespace fallback, invalid `MINDROOM_NAMESPACE` raising, and current-domain extraction with and without `MATRIX_SERVER_NAME`.

Risk/tests:

- Namespace consolidation risks changing whether malformed pairing namespaces raise or fall back.
Tests should assert that pairing still returns a derived namespace with `namespace_invalid=True`.
- Server-name extraction consolidation risks preserving the current simplistic IPv6 handling, because both existing implementations split on the first colon.
Tests should lock current behavior before any separate correctness improvement.
- No production code was edited for this audit.
