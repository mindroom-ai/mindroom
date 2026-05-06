## Summary

Top duplication candidates in `src/mindroom/oauth/state.py` are JSON persistence with advisory file locks, corrupt-file quarantine, and temp-file replacement.
No other source module duplicates the full opaque OAuth state-token lifecycle of issue/read/consume with `kind`, `exp`, and `data` records.
The only actionable refactor would be a very small shared JSON-store helper, but the differences between current stores are large enough that no immediate refactor is recommended from this file alone.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_state_file	function	lines 31-32	related-only	oauth_state storage path, sync token path, invited_rooms path	src/mindroom/matrix/sync_tokens.py:30; src/mindroom/matrix/invited_rooms_store.py:21
_state_lock_file	function	lines 35-36	related-only	lock_file path derivation, .lock sibling paths	src/mindroom/codex_model.py:121; src/mindroom/interactive.py:319; src/mindroom/handled_turns.py:443
_locked_state_store	function	lines 40-58	duplicate-found	fcntl.flock JSON store, thread lock plus file lock, save on exit	src/mindroom/codex_model.py:119; src/mindroom/interactive.py:264; src/mindroom/interactive.py:292; src/mindroom/handled_turns.py:440
_corrupt_state_file	function	lines 61-67	duplicate-found	corrupt file quarantine, .corrupt replace, malformed JSON repair	src/mindroom/handled_turns.py:461; src/mindroom/handled_turns.py:522
_load_state_store	function	lines 70-99	duplicate-found	json.loads path read validate dict prune expired entries	src/mindroom/memory/auto_flush.py:203; src/mindroom/matrix/invited_rooms_store.py:26; src/mindroom/handled_turns.py:450; src/mindroom/matrix/sync_tokens.py:48
_save_state_store	function	lines 102-107	duplicate-found	json.dumps temp path write replace atomic persistence	src/mindroom/memory/auto_flush.py:231; src/mindroom/matrix/invited_rooms_store.py:49; src/mindroom/handled_turns.py:405; src/mindroom/codex_model.py:139; src/mindroom/constants.py:1088
issue_opaque_oauth_state	function	lines 110-128	related-only	token_urlsafe ttl exp data record issue state	src/mindroom/api/sandbox_worker_prep.py:81; src/mindroom/api/sandbox_worker_prep.py:103; src/mindroom/oauth/providers.py:239; src/mindroom/api/credentials.py:220; src/mindroom/oauth/service.py:79
read_opaque_oauth_state	function	lines 131-160	related-only	validate token record kind exp data without consuming	src/mindroom/oauth/service.py:109; src/mindroom/api/credentials.py:248; src/mindroom/oauth/service.py:87
consume_opaque_oauth_state	function	lines 163-187	duplicate-found	read and consume share identical OAuth state record validation	src/mindroom/oauth/state.py:131; src/mindroom/oauth/service.py:119; src/mindroom/api/credentials.py:260
```

## Findings

### 1. JSON file stores repeat locking, loading, mutation, and saving behavior

`src/mindroom/oauth/state.py:40` wraps a module thread lock, an `fcntl.flock` sibling lock file, `_load_state_store`, caller mutation, and `_save_state_store`.
Similar advisory-lock persistence appears in `src/mindroom/codex_model.py:119`, `src/mindroom/interactive.py:264`, `src/mindroom/interactive.py:292`, and `src/mindroom/handled_turns.py:440`.
The shared behavior is cross-process serialization around small JSON-backed state files.
Differences to preserve are important: OAuth always takes an exclusive lock and prunes expired state during load, interactive persistence uses both shared and exclusive locks and merges dirty in-memory questions, handled-turn persistence supports shared reads and richer repair logging, and Codex auth has file permissions requirements.

### 2. Corrupt JSON quarantine is duplicated with handled-turn storage

`src/mindroom/oauth/state.py:61` renames malformed OAuth state to a `.corrupt-<timestamp>` sibling and adds a UUID if the timestamp collides.
`src/mindroom/handled_turns.py:461` detects malformed or structurally invalid persisted JSON and `src/mindroom/handled_turns.py:522` moves it to `.corrupt-<time_ns>`.
Both behaviors preserve the bad file for diagnosis and reset to an empty in-memory state.
Differences to preserve are the filename uniqueness strategy, the handled-turn `FileNotFoundError` tolerance, and the OAuth `load_failed` flag that prevents overwriting a corrupted store unless the yielded state changed.

### 3. Temp-file JSON replacement is repeated across stores

`src/mindroom/oauth/state.py:102` writes compact sorted JSON to a uniquely named temp file and replaces the target.
Equivalent persistence patterns exist in `src/mindroom/memory/auto_flush.py:231`, `src/mindroom/matrix/invited_rooms_store.py:49`, `src/mindroom/handled_turns.py:405`, and `src/mindroom/codex_model.py:139`.
`src/mindroom/constants.py:1088` already provides `safe_replace`, and `src/mindroom/matrix/invited_rooms_store.py:58` uses it, but OAuth uses raw `Path.replace`.
Differences to preserve include compact versus indented JSON, newline conventions, `fsync` in handled-turn storage, `chmod(0o600)` in Codex auth, and `safe_replace` fallback needs for bind mounts.

### 4. `read_opaque_oauth_state` and `consume_opaque_oauth_state` duplicate record validation internally

`src/mindroom/oauth/state.py:131` and `src/mindroom/oauth/state.py:163` both validate that the record is a dict, `kind` matches, `exp` is numeric and unexpired, and `data` is a dict.
The only behavioral difference is retrieval mode: `read` gets without saving while `consume` pops and saves.
This is the clearest local duplication in the primary module.

## Proposed Generalization

No refactor recommended as part of this audit because production code must not be edited and the cross-module JSON stores have meaningful differences.

If this duplication is addressed later, the smallest safe steps are:

1. Extract a private `_validated_state_data(record, kind, now)` helper inside `src/mindroom/oauth/state.py` for the shared `read` and `consume` validation.
2. Consider a focused `mindroom.storage.json_files` helper only for atomic JSON writes using a caller-provided serializer and optional `safe_replace`.
3. Keep corrupt-file quarantine opt-in and caller-owned, since repair policy and logging differ by store.
4. Do not merge the lock managers until a second module needs exactly the OAuth-style exclusive read-modify-write context.

## Risk/Tests

For the local OAuth validation helper, tests should cover invalid token, mismatched kind, expired state, missing `exp`, non-dict `data`, read-without-consume, and consume-removes-token.
For any shared JSON write helper, tests should cover temp-file cleanup, replace fallback behavior if `safe_replace` is used, formatting expectations where asserted, and unchanged behavior for corrupt JSON repair.
For any shared lock helper, tests should include nested thread contention and cross-process lock behavior, because current modules differ in shared versus exclusive locking and save-on-exit policy.
