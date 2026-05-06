## Summary

The only meaningful duplication candidate is Matrix user-id validation/concreteness checking.
`src/mindroom/orchestration/rooms.py` uses a local invite-oriented predicate that overlaps with stricter Matrix ID parsing in `src/mindroom/matrix/identity.py` and owner onboarding validation in `src/mindroom/cli/owner.py`.

No broader invite-list collection duplication was found under `src`; the authorization and root-space invite helpers are small, orchestration-specific selectors.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_is_concrete_matrix_user_id	function	lines 16-20	duplicate-found	concrete Matrix user id validation; startswith @ colon wildcard space; parse matrix user id	src/mindroom/cli/owner.py:10-20; src/mindroom/matrix/identity.py:117-168; src/mindroom/matrix/identity.py:249-260; src/mindroom/tool_approval.py:188; src/mindroom/hooks/sender.py:44
_filter_concrete_matrix_user_ids	function	lines 23-29	related-only	filter concrete matrix user ids; skipped wildcard placeholder entries; non-concrete authorization users	src/mindroom/orchestration/rooms.py:16-20; tests/test_multi_agent_bot.py:11747-11787
get_authorized_user_ids_to_invite	function	lines 32-40	related-only	authorized user ids to invite; global_users room_permissions invite; ensure room invitations	src/mindroom/orchestrator.py:1629-1643; src/mindroom/authorization.py:86-113; tests/test_multi_agent_bot.py:11694-11744
get_root_space_user_ids_to_invite	function	lines 43-52	related-only	root space user ids invite; global_users internal user root space invites	src/mindroom/orchestrator.py:1497-1509; tests/test_matrix_spaces.py:411-472
```

## Findings

### 1. Matrix user-id validity checks are duplicated with different strictness

`src/mindroom/orchestration/rooms.py:16-20` defines `_is_concrete_matrix_user_id` as a lightweight predicate for inviteable configured users.
It accepts strings that start with `@`, contain `:`, and do not contain wildcard characters or spaces.

Related validation exists in `src/mindroom/cli/owner.py:10-20`, where `_OWNER_MATRIX_USER_ID_RE` and `parse_owner_matrix_user_id` validate owner IDs as `@localpart:server` with no whitespace and at least one non-colon localpart character.
More complete parsing exists in `src/mindroom/matrix/identity.py:117-168`, where `parse_historical_matrix_user_id` and `try_parse_historical_matrix_user_id` parse Matrix IDs, validate server names, reject invalid Unicode/length, and return canonical IDs.
There are also small inline shape checks in `src/mindroom/tool_approval.py:188` and `src/mindroom/hooks/sender.py:44`.

These are functionally related because all protect boundaries that expect a Matrix user ID.
They are not identical: room invitations intentionally reject wildcard authorization patterns like `@admin:*`, while historical Matrix parsing may accept syntactically valid IDs that this invite helper rejects only because they contain `*`, `?`, or spaces.
Any consolidation must preserve that wildcard-filtering behavior for authorization invite fan-out.

### 2. Concrete-user filtering is local to invite selection

`src/mindroom/orchestration/rooms.py:23-29` filters a set through `_is_concrete_matrix_user_id` and logs skipped entries.
No equivalent set-filtering helper was found elsewhere in `src`.
The closest behavioral coverage is `tests/test_multi_agent_bot.py:11747-11787`, which verifies that non-Matrix authorization entries are not invited.

This is related to the Matrix user-id validation duplication above, but the set filtering and warning behavior itself is not duplicated.

### 3. Authorized-room invite candidates are not duplicated

`src/mindroom/orchestration/rooms.py:32-40` collects `authorization.global_users` plus all `authorization.room_permissions` values before filtering concrete Matrix IDs.
The only production caller is `src/mindroom/orchestrator.py:1629-1643`, which later uses `is_authorized_sender` to decide whether each candidate can access each joined room.

`src/mindroom/authorization.py:86-113` reads the same authorization structures, but it answers a different question: whether one sender is authorized for one room.
The helper under audit builds the candidate invite set for all rooms.
That is related data access, not duplicated behavior.

### 4. Root-space invite candidates are not duplicated

`src/mindroom/orchestration/rooms.py:43-52` collects only concrete global users and optionally appends the internal MindRoom user ID for root Space invites.
The caller in `src/mindroom/orchestrator.py:1497-1509` performs membership checks and Matrix invites.
Tests in `tests/test_matrix_spaces.py:411-472` cover the important distinction that root-space invites include global users and the internal user but exclude room-specific collaborators.

No second production implementation of this root-space candidate selection was found.

## Proposed Generalization

A small shared predicate could live in `src/mindroom/matrix/identity.py`, for example `is_concrete_matrix_user_id_for_invite(user_id: str) -> bool`, implemented on top of existing parsing plus explicit wildcard rejection if the stricter behavior is desired.
`src/mindroom/orchestration/rooms.py` and owner/dashboard boundary checks could then converge on one Matrix ID parsing source of truth where semantics match.

No immediate refactor is strongly recommended from this file alone.
The current duplication is small and the invite helper's wildcard rejection is intentionally policy-specific.
If this is refactored, keep `_filter_concrete_matrix_user_ids` in the orchestration module or rename it to make the authorization-invite policy explicit.

## Risk/tests

The main risk is changing which configured authorization entries are invited.
Tests should cover valid Matrix IDs, wildcard authorization patterns such as `@admin:*`, placeholders such as `__MINDROOM_OWNER_USER_ID_FROM_PAIRING__`, missing-domain strings, whitespace, and any historical Matrix localparts currently allowed by `parse_historical_matrix_user_id`.

Relevant existing tests are `tests/test_multi_agent_bot.py:11747-11787` for managed-room invite filtering and `tests/test_matrix_spaces.py:411-472` for root-space invite selection.
