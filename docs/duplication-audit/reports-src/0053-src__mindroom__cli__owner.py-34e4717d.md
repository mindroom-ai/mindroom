## Summary

No meaningful duplication found for `src/mindroom/cli/owner.py`.
The closest related code is Matrix user ID validation in `src/mindroom/matrix/identity.py`, invite filtering in `src/mindroom/orchestration/rooms.py`, and explicit mention validation in `src/mindroom/matrix/mentions.py`.
Those call sites serve different contracts from CLI owner onboarding, so no refactor is recommended.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
parse_owner_matrix_user_id	function	lines 13-20	related-only	parse_owner_matrix_user_id, owner_user_id, matrix_user_id, Matrix user ID validation, startswith @ colon, try_parse_historical_matrix_user_id	src/mindroom/cli/connect.py:77, src/mindroom/cli/connect.py:120, src/mindroom/cli/config.py:118, src/mindroom/matrix/identity.py:144, src/mindroom/matrix/identity.py:154, src/mindroom/matrix/identity.py:161, src/mindroom/orchestration/rooms.py:16, src/mindroom/matrix/mentions.py:274, src/mindroom/api/auth.py:268, src/mindroom/api/auth.py:297, src/mindroom/api/credentials.py:195, src/mindroom/api/credentials.py:209
replace_owner_placeholders_in_text	function	lines 23-33	none-found	replace_owner_placeholders_in_text, OWNER_MATRIX_USER_ID_PLACEHOLDER, __MINDROOM_OWNER_USER_ID_FROM_PAIRING__, __PLACEHOLDER__, replace owner placeholders	src/mindroom/cli/connect.py:129, src/mindroom/cli/config.py:407, src/mindroom/constants.py:1001, src/mindroom/config_template.yaml:92, src/mindroom/config_template.yaml:98, src/mindroom/config_template.yaml:102
```

## Findings

No real duplicated behavior found.

Related Matrix user ID validation appears in `src/mindroom/matrix/identity.py:144`, `src/mindroom/matrix/identity.py:154`, and `src/mindroom/matrix/identity.py:161`.
Those helpers parse canonical Matrix IDs and validate server-name grammar, UTF-8 length, and historical/current localpart rules.
`parse_owner_matrix_user_id` in `src/mindroom/cli/owner.py:13` has a narrower CLI-onboarding contract: accept only string inputs, trim whitespace, and return the original trimmed owner MXID only when it matches a simple no-whitespace `@localpart:server` shape.
Switching directly to the shared identity parser would change accepted values and error behavior, so this is related validation rather than safe duplicate code.

Related concrete-user filtering appears in `src/mindroom/orchestration/rooms.py:16`.
It checks only inviteability constraints for configured authorization entries, including rejecting wildcards, question marks, and spaces.
That behavior is intentionally looser than owner parsing and does not trim or canonicalize values.

Related explicit mention validation appears in `src/mindroom/matrix/mentions.py:274`.
It delegates to current Matrix ID parsing for rendered user mentions and is stricter than owner onboarding parsing.

Owner placeholder replacement is centralized in `src/mindroom/cli/owner.py:23`.
The only occurrences elsewhere are call sites in `src/mindroom/cli/connect.py:129` and `src/mindroom/cli/config.py:407`, constants/templates in `src/mindroom/constants.py:1001` and `src/mindroom/config_template.yaml:92`, and generated config template usage in `src/mindroom/cli/config.py`.
No second implementation of replacing `__MINDROOM_OWNER_USER_ID_FROM_PAIRING__` or the legacy `__PLACEHOLDER__` token was found under `src`.

## Proposed Generalization

No refactor recommended.
The existing `src/mindroom/cli/owner.py` module is already the focused owner-onboarding helper shared by `connect` and `config init`.

## Risk/Tests

No production code changes were made.
If this area is refactored later, tests should preserve the current owner parser contract for non-string input, whitespace trimming, malformed owner IDs, quoted YAML-safe replacement, and legacy placeholder replacement.
