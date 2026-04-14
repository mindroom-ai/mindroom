# REVIEW — ISSUE-147 Cache Facade Refactor

**Verdict: APPROVE**

Two clean commits that consolidate the Matrix conversation cache surface, privatize implementation modules, and add reply-chain invalidation on redaction/edit.
No blockers found. Three minor findings, one observation.

---

## Findings

### MINOR — `tests/test_thread_mode.py:1359` — Stale test class and docstring names

`TestConversationAccessArchitecture` and its docstring `"Architecture guards for the explicit conversation-access seam."` still use the old "ConversationAccess" naming.
Should be `TestConversationCacheArchitecture` / `"conversation-cache seam"` for consistency.

Two source docstrings are also stale:
- `src/mindroom/conversation_resolver.py:455` — `"conversation-access policy"`
- `src/mindroom/thread_summary.py:153` — `"conversation-access seam"`

### MINOR — No test for edit-path reply chain invalidation

`test_live_redaction_invalidates_cached_reply_chain_for_redacted_edit` (`tests/test_threading_error.py:1242`) thoroughly covers redaction with transitive cascading.
However, there is no corresponding test for the edit path (`append_live_event` with `event_info.is_edit=True`, `conversation_cache.py:833-838`).
The implementation is symmetric and straightforward, but a test would lock down the behavior.

### MINOR — Sync-path redaction invalidation is shallower than live path

In `_group_sync_timeline_updates` (`conversation_cache.py:1063`), sync-path redactions call `_invalidate_reply_chain(room_id, redacted_event_id)` with only the redacted event ID.
The live path (`apply_redaction`, line 892) additionally inspects the event cache to check whether the redacted event was itself an edit and, if so, also invalidates the original event ID.
The sync path skips this extra lookup.
Likely acceptable since sync-path events may not yet be in the event cache, but the asymmetry is worth noting.

### OBSERVATION — `event_cache=None` degradation is pre-existing, not a regression

The prior review flagged `_reply_chain_invalidation_ids_for_redaction` returning only `{redacted_event_id}` when `event_cache is None` (line 382-383).
This is real: redacting an edit without cache access only evicts the edit node, not the original or its dependents.
However, before this PR, `ReplyChainCaches` had **zero** invalidation capability (confirmed: `git show main:src/mindroom/matrix/reply_chain.py` has no `invalidate` method).
This PR strictly improves the situation; the `event_cache=None` gap is a pre-existing limitation, not a regression.

### OBSERVATION — Sync token persistence re-introduced

The diff includes ~30 lines of sync-token save/restore/flush logic in `bot.py` (fields `_last_persisted_sync_token`, `_pending_sync_token`, methods `_restore_saved_sync_token`, `_persist_sync_token`, `_maybe_persist_sync_token`).
This was originally added by ISSUE-050 (`0ec2a13f9`) and then accidentally removed by the ISSUE-141 TurnStore consolidation (`2ab4947b9`).
This PR re-introduces it with improved field names and a configurable throttle interval (`_SYNC_TOKEN_SAVE_INTERVAL_SECONDS = 30.0`).
Not a problem — just noting it's beyond the stated refactor scope.

---

## What was checked

### Imports and renames — all clean
- Grepped for `MatrixConversationAccess`, `conversation_access`, `ConversationReadAccess`, `room_cache` (as import), `event_cache` (without underscore prefix), `event_cache_write_coordinator` (without underscore prefix) across all source and test files.
- Zero stale production imports found.
- `conversation_cache.py` re-exports `ConversationEventCache`, `EventCache`, `EventCacheWriteCoordinator`, `ConversationCacheProtocol`, `MatrixConversationCache`, `EventLookupResult`, `ThreadReadResult` via `__all__` (lines 54-62).

### Private module encapsulation — complete
- No production code outside `src/mindroom/matrix/` imports `_event_cache` or `_event_cache_write_coordinator` directly.
- Only `client.py` and `conversation_cache.py` (both within `matrix/`) reference the private modules.
- 4 test files import from private modules — expected and acceptable.
- `room_cache.py` fully absorbed; zero remaining imports of the deleted module.

### Reply chain invalidation — correct
- `ReplyChainCaches.invalidate()` (`reply_chain.py:127-133`) uses a while-loop over `_invalidate_nodes` + `_invalidate_roots` for transitive closure. Cascading logic is sound.
- Null/empty filtering handled (`isinstance(event_id, str) and event_id`).
- Room isolation enforced via `room_id` check in both private methods.
- Invalidation happens before async cache persistence in both `apply_redaction` and `append_live_event` — correct ordering.
- Three trigger paths wired: live redaction (`apply_redaction`), live edit (`append_live_event`), sync timeline (`_track_sync_cached_event` + `_group_sync_timeline_updates`).
- `bind_reply_chain_caches` DI pattern wired in `bot.py:418`.

### Test coverage
- `test_live_redaction_invalidates_cached_reply_chain_for_redacted_edit` covers: redacting an edit event, transitive eviction of original + dependent reply node, root eviction, and event cache write-through.
- All existing tests updated consistently for the rename (30+ `_conversation_access` → `_conversation_cache` attribute references, 15+ import changes).
- Edit-path invalidation test is missing (noted above as MINOR).

### Naming consistency
- All variable names updated (`_conversation_access` → `_conversation_cache`).
- One stale test class name and two stale docstrings (noted above as MINOR).
- `room_cache` in method names like `_queue_room_cache_update` refers to room-level cache operations, not the deleted module — semantically correct.

### Behavioral changes beyond rename
- Reply chain invalidation on redaction/edit (new feature, no prior equivalent).
- `cache_sync_timeline` refactored from inline processing to extracted helpers (`_collect_sync_timeline_cache_updates`, `_group_sync_timeline_updates`, `_track_sync_cached_event`). Logic is equivalent; structure centralizes edit/redaction tracking.
- `_reply_chain_caches_getter` field added to `MatrixConversationCache` dataclass, properly defaulted to `None` with `init=False`.
- Sync token persistence re-introduced (see observation above).
