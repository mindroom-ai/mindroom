## Summary

Top duplication candidate: `AuthorizationConfig.validate_unique_aliases` repeats the repository's ordered duplicate-detection validator shape from `src/mindroom/config/models.py`, with a close unordered variant in `src/mindroom/config/matrix.py`.
`AuthorizationConfig.resolve_alias` has related reverse-map lookup behavior elsewhere, but its authorization-specific fallback semantics make it too narrow to generalize now.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
AuthorizationConfig	class	lines 8-67	related-only	authorization config fields global_users room_permissions aliases agent_reply_permissions Config validation invite collection	src/mindroom/authorization.py:61, src/mindroom/authorization.py:116, src/mindroom/config/main.py:464, src/mindroom/orchestration/rooms.py:32
AuthorizationConfig.validate_unique_aliases	method	lines 47-60	duplicate-found	validate_unique aliases Duplicate bridge aliases seen duplicates field_validator duplicate validation	src/mindroom/config/models.py:187, src/mindroom/config/matrix.py:142, tests/test_authorization.py:1359
AuthorizationConfig.resolve_alias	method	lines 62-67	related-only	resolve_alias reverse lookup aliases canonical fallback room alias reverse lookup authorization alias resolution	src/mindroom/authorization.py:87, src/mindroom/authorization.py:141, src/mindroom/commands/handler.py:224, src/mindroom/matrix/rooms.py:229, tests/test_authorization.py:1347
```

## Findings

1. Ordered duplicate-list validation is duplicated between auth aliases and tool config entries.

- `src/mindroom/config/auth.py:47` walks nested alias lists, tracks `seen_aliases`, appends each duplicate once in first duplicate encounter order, raises `ValueError`, and returns the original value.
- `src/mindroom/config/models.py:187` does the same for `ToolConfigEntry.name`: track `seen`, append each duplicate once in encounter order, raise `ValueError`, and return the original value.
- `src/mindroom/config/matrix.py:142` is a related variant for `invite_only_rooms`; it checks `len(list) != len(set(list))`, derives duplicates with a set comprehension, sorts them in the error, then returns the original list.
- The shared behavior is "validate unique derived keys while preserving the original typed collection and producing a duplicate-name error."
- Differences to preserve: auth validates aliases derived from `dict[str, list[str]]`, tool config validates `entry.name`, matrix room access currently reports sorted duplicates, while auth and tool config preserve duplicate encounter order.

2. Authorization alias resolution has related lookup patterns, but no meaningful production duplicate.

- `src/mindroom/config/auth.py:62` scans `self.aliases.items()` and returns the canonical user for a sender alias, falling back to the original sender ID.
- Callers in `src/mindroom/authorization.py:87`, `src/mindroom/authorization.py:141`, and `src/mindroom/commands/handler.py:224` depend on this exact "identity fallback" behavior before checking global users or per-agent allowlists.
- `src/mindroom/matrix/rooms.py:229` performs a reverse lookup from room ID to alias, but it returns `None` on misses and reads persisted Matrix room-alias state rather than auth aliases.
- This is only a related map/reverse-map traversal pattern, not active duplicated authorization behavior.

## Proposed Generalization

Introduce a small private helper in `src/mindroom/config/models.py` only if more config validators need this pattern, or move it to a focused `src/mindroom/config/validation.py` if both auth and model config adopt it:

1. Add a pure helper such as `reject_duplicate_values(values, *, label, key=lambda item: item, sort_duplicates=False)`.
2. Use it in `AuthorizationConfig.validate_unique_aliases` by passing a flattened alias iterator and keeping encounter-order duplicate reporting.
3. Use it in `validate_unique_tool_entries` with `key=lambda entry: entry.name`.
4. Consider using it in `MatrixRoomAccessConfig.validate_unique_invite_only_rooms` only if preserving or intentionally changing sorted duplicate output is acceptable.

No refactor is recommended for `resolve_alias`.

## Risk/tests

Risk is low if the helper preserves exact duplicate ordering and error text for auth aliases and tool entries.
Tests to run for a refactor would include `tests/test_authorization.py::test_duplicate_bridge_alias_rejected`, `tests/test_authorization.py::test_resolve_alias_method`, and the config/model tests covering duplicate tool entries.
If matrix invite-only validation is included, add or update a focused test for duplicate reporting because its current duplicate ordering differs.
