## Summary

The only meaningful duplication found is non-empty sync token normalization duplicated between `src/mindroom/matrix/sync_tokens.py` and `src/mindroom/matrix/sync_certification.py`.
The rest of the module is a narrowly scoped persistence boundary for Matrix sync-token records, with related but not duplicated patterns in Matrix state and knowledge metadata persistence.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SyncTokenRecord	class	lines 18-27	related-only	SyncTokenRecord SyncCheckpoint certified dataclass token checkpoint	src/mindroom/matrix/sync_certification.py:19; src/mindroom/matrix/sync_certification.py:26; src/mindroom/matrix/sync_certification.py:43
SyncTokenRecord.certified	method	lines 25-27	related-only	def certified checkpoint is not None certified property	src/mindroom/matrix/sync_certification.py:37
_sync_token_path	function	lines 30-32	none-found	sync_tokens .token agent_name storage_path path helper	src/mindroom/matrix/state.py:115; src/mindroom/constants.py:824; src/mindroom/bot.py:928
_sync_token_certification_path	function	lines 35-37	none-found	token.certified certification_path legacy marker sync token	src/mindroom/matrix/sync_tokens.py:86; tests/test_matrix_sync_tokens.py:101
_normalized_token	function	lines 40-45	duplicate-found	normalized token non_empty_token isinstance str strip or None	src/mindroom/matrix/sync_certification.py:63; src/mindroom/thread_tags.py:123; src/mindroom/constants.py:820
_record_from_json	function	lines 48-60	related-only	json loads version token record checkpoint malformed returns None	src/mindroom/knowledge/registry.py:280; src/mindroom/credentials_sync.py:155; src/mindroom/api/sandbox_runner.py:128
_record_json	function	lines 63-69	related-only	json dumps sort_keys separators version token payload	src/mindroom/matrix/large_messages.py:138; src/mindroom/workers/runtime.py:33; src/mindroom/knowledge/registry.py:318
save_sync_token	function	lines 72-86	related-only	write_text json mkdir unlink missing_ok token persistence	src/mindroom/knowledge/registry.py:318; src/mindroom/matrix/state.py:183; src/mindroom/bot.py:951
clear_sync_token	function	lines 89-94	related-only	clear saved token unlink missing_ok paired files	src/mindroom/attachments.py:288; src/mindroom/knowledge/manager.py:985; src/mindroom/bot.py:964
load_sync_token	function	lines 97-102	none-found	load_sync_token wrapper token_record token	src/mindroom/bot.py:925; tests/test_matrix_sync_tokens.py:70
load_sync_token_record	function	lines 105-123	related-only	read_text strip unicode decode json legacy plaintext load record	src/mindroom/knowledge/registry.py:280; src/mindroom/credentials_sync.py:122; src/mindroom/matrix/state.py:169
```

## Findings

### Duplicate: sync token non-empty string normalization

`src/mindroom/matrix/sync_tokens.py:40` implements `_normalized_token(value: object) -> str | None` by accepting only strings, trimming whitespace, and returning `None` for non-strings or empty results.
`src/mindroom/matrix/sync_certification.py:63` implements `_non_empty_token(token: str | None) -> str | None` with the same runtime behavior for all values the certification code passes and the same persisted-token semantics.

Both functions define the same Matrix sync-token invariant: a usable token is a string that remains non-empty after `strip()`.
The difference to preserve is only typing/interface intent: `sync_tokens._normalized_token` accepts `object` because it validates JSON payload fields, while `sync_certification._non_empty_token` is typed for certification inputs.

### Related-only: certified state properties

`SyncTokenRecord.certified` at `src/mindroom/matrix/sync_tokens.py:25` and `SyncCacheWriteResult.certified` at `src/mindroom/matrix/sync_certification.py:37` are similarly named boolean properties, but they answer different questions.
The token record checks whether persisted provenance includes a checkpoint.
The cache write result checks whether a sync response was durably cached without limited timelines or errors.
No shared helper is recommended.

### Related-only: JSON persistence shape

`_record_from_json`, `_record_json`, `save_sync_token`, and `load_sync_token_record` share broad JSON-file persistence mechanics with modules such as `src/mindroom/knowledge/registry.py:280` and `src/mindroom/matrix/state.py:183`.
Those modules persist different schemas, error policies, and atomicity guarantees.
The sync-token module intentionally supports a legacy plaintext token format at `src/mindroom/matrix/sync_tokens.py:117`, which makes the behavior domain-specific rather than a candidate for a generic JSON record loader.

## Proposed Generalization

Move the duplicated token normalization into the sync certification module as a public small helper, for example `normalize_sync_token(value: object) -> str | None` in `src/mindroom/matrix/sync_certification.py`, and have both persistence and certification call it.
This keeps the invariant next to the sync trust state machine while allowing JSON-loaded objects from `sync_tokens.py` to be validated through the same source of truth.

Refactor plan:

1. Add `normalize_sync_token(value: object) -> str | None` with the current `strip()` semantics.
2. Replace `_non_empty_token` call sites in `sync_certification.py` with the shared helper.
3. Replace `_normalized_token` call sites in `sync_tokens.py` with the shared helper and remove the private duplicate.
4. Run `uv run pytest tests/test_matrix_sync_tokens.py tests/test_threading_error.py -x -n 0 --no-cov -v`.

## Risk/tests

Risk is low if the helper keeps the broader `object` input type and exact `strip()` behavior.
Tests should cover certified JSON round trips, legacy plaintext token loading, whitespace-only files, invalid UTF-8 handling, `M_UNKNOWN_POS` clearing, and checkpoint save behavior.
Existing coverage in `tests/test_matrix_sync_tokens.py:70`, `tests/test_matrix_sync_tokens.py:84`, `tests/test_matrix_sync_tokens.py:101`, and `tests/test_matrix_sync_tokens.py:192` exercises the key persistence cases.
