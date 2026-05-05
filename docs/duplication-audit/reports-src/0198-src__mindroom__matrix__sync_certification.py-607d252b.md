Summary: One small real duplication exists between sync certification token normalization and sync token persistence token normalization.
The certification state machine itself is otherwise centralized; related code in `bot.py`, `sync_tokens.py`, and cache writers calls into this module instead of reimplementing certification decisions.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SyncTrustState	class	lines 10-16	none-found	SyncTrustState enum states cold pending certified uncertain; sync trust state	src/mindroom/bot.py:304; src/mindroom/bot.py:943; src/mindroom/bot.py:1318
SyncCheckpoint	class	lines 20-23	related-only	SyncCheckpoint token checkpoint; sync token checkpoint persistence	src/mindroom/matrix/sync_tokens.py:9; src/mindroom/matrix/sync_tokens.py:22; src/mindroom/matrix/sync_tokens.py:59; src/mindroom/matrix/sync_tokens.py:83
SyncCacheWriteResult	class	lines 27-40	related-only	SyncCacheWriteResult complete limited_room_ids errors runtime_diagnostics; cache sync certification result	src/mindroom/matrix/cache/thread_writes.py:1186; src/mindroom/matrix/cache/thread_writes.py:1192; src/mindroom/matrix/cache/thread_writes.py:1201; src/mindroom/matrix/cache/thread_writes.py:1216; src/mindroom/matrix/cache/thread_writes.py:1231
SyncCacheWriteResult.certified	method	lines 38-40	none-found	cache write certified complete no limited no errors; cache_write_certified	src/mindroom/matrix/cache/thread_writes.py:1230; src/mindroom/matrix/sync_certification.py:158
SyncCertificationDecision	class	lines 44-51	none-found	SyncCertificationDecision state checkpoint clear reset reason; apply sync certification decision	src/mindroom/bot.py:971; src/mindroom/bot.py:999; src/mindroom/bot.py:1064
SyncCertificationStart	class	lines 55-60	none-found	SyncCertificationStart startup sync_token legacy_token; restore saved sync token	src/mindroom/bot.py:940; src/mindroom/bot.py:943
_non_empty_token	function	lines 63-68	duplicate-found	non empty token normalize strip token; normalized_token safe sync token string	src/mindroom/matrix/sync_tokens.py:40; src/mindroom/matrix/sync_tokens.py:56; src/mindroom/matrix/sync_tokens.py:79; src/mindroom/matrix/sync_tokens.py:120
start_from_loaded_token	function	lines 71-90	related-only	start from loaded token checkpoint legacy token cold pending; load sync token record restored certified	src/mindroom/bot.py:925; src/mindroom/bot.py:940; src/mindroom/matrix/sync_tokens.py:105
_uncertain_decision	function	lines 93-104	none-found	clear_saved_token reset_client_token uncertain decision; matrix_sync_certification_uncertain	src/mindroom/bot.py:971; src/mindroom/bot.py:987
_uncertain_reason	function	lines 107-117	none-found	missing_next_batch cache_write_failed limited_sync_timeline cache_write_incomplete	src/mindroom/matrix/cache/thread_writes.py:1199; src/mindroom/matrix/cache/thread_writes.py:1227; src/mindroom/matrix/cache/thread_writes.py:1230
certify_sync_response	function	lines 120-143	none-found	certify sync response next_batch first_sync cache_result pending reset	src/mindroom/bot.py:999; src/mindroom/bot.py:1020; src/mindroom/matrix/cache/thread_writes.py:1186
handle_unknown_pos	function	lines 146-151	none-found	M_UNKNOWN_POS unknown_pos reset client token clear saved sync token	src/mindroom/bot.py:1059; src/mindroom/bot.py:1063; src/mindroom/bot.py:1064
sync_cache_write_diagnostics	function	lines 154-173	related-only	cache write diagnostics error types messages limited room ids runtime diagnostics	src/mindroom/bot.py:987; src/mindroom/matrix/cache/thread_write_cache_ops.py:45; src/mindroom/matrix/cache/event_cache.py:153; src/mindroom/matrix/cache/sqlite_event_cache.py:414; src/mindroom/matrix/cache/postgres_event_cache.py:692
```

Findings:

1. Duplicate token normalization for sync tokens.
   `src/mindroom/matrix/sync_certification.py:63` defines `_non_empty_token(token: str | None) -> str | None` by requiring a string, stripping whitespace, and returning `None` for empty values.
   `src/mindroom/matrix/sync_tokens.py:40` defines `_normalized_token(value: object) -> str | None` with the same behavior for non-string values, whitespace stripping, and empty-string rejection.
   These are functionally the same validation rule for Matrix sync tokens, used on the certification side at `src/mindroom/matrix/sync_certification.py:74`, `src/mindroom/matrix/sync_certification.py:85`, `src/mindroom/matrix/sync_certification.py:109`, and `src/mindroom/matrix/sync_certification.py:135`, and on the persistence side at `src/mindroom/matrix/sync_tokens.py:56`, `src/mindroom/matrix/sync_tokens.py:79`, and `src/mindroom/matrix/sync_tokens.py:120`.
   Difference to preserve: the sync certification helper is typed as `str | None`, while the persistence helper accepts `object` because it parses JSON payloads and file contents.

Related but not duplicate:

- `SyncCheckpoint` is shared between certification and persistence.
  `src/mindroom/matrix/sync_tokens.py:59` and `src/mindroom/matrix/sync_tokens.py:83` construct it, but they do not duplicate its behavior.
- Cache write completion semantics are produced in `src/mindroom/matrix/cache/thread_writes.py:1186` and consumed by `SyncCacheWriteResult.certified` and `_uncertain_reason`.
  The writer computes whether the cache operation is complete, while `sync_certification.py` maps that result to trust decisions.
- Diagnostics assembly in `sync_cache_write_diagnostics` combines fields from `SyncCacheWriteResult` with runtime diagnostics from cache backends.
  Cache backends expose related runtime diagnostic dictionaries at `src/mindroom/matrix/cache/thread_write_cache_ops.py:45`, `src/mindroom/matrix/cache/sqlite_event_cache.py:414`, and `src/mindroom/matrix/cache/postgres_event_cache.py:692`, but they do not duplicate the final sync-certification log payload construction.
- `handle_unknown_pos` centralizes the fail-closed `M_UNKNOWN_POS` decision.
  The only related caller is `src/mindroom/bot.py:1064`.

Proposed generalization:

Move the shared token normalization rule to a single small helper near token persistence, for example `normalize_sync_token(value: object) -> str | None` in `src/mindroom/matrix/sync_tokens.py`, and import it from `sync_certification.py`.
Keep the helper intentionally narrow: accept `object`, return stripped non-empty strings, and otherwise return `None`.
No broader state-machine refactor is recommended.

Risk/tests:

The main behavior risk is an import cycle between `sync_tokens.py` and `sync_certification.py`, because `sync_tokens.py` currently imports `SyncCheckpoint` from `sync_certification.py`.
A safer implementation would either move the helper to a tiny neutral module such as `src/mindroom/matrix/sync_token_values.py` or move `SyncCheckpoint` alongside token persistence before wiring both modules to the helper.
Tests should cover whitespace-only loaded tokens, non-string JSON token values, certified checkpoint startup, legacy token startup, missing `next_batch`, and certified cache write decisions.
