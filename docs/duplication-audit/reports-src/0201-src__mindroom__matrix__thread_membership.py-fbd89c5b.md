## Summary

Top duplication candidates for `src/mindroom/matrix/thread_membership.py`:

1. `thread_room_scan.py` repeats the room-get-event response normalization, conversation-cache thread-id lookup, and cache-backed event-info fetch helpers now also present in `thread_membership.py`.
2. `thread_bookkeeping.py` repeats thread-child proof checks for page-local and cached event sources, with behavior close to `map_backed_thread_membership_access()` and `_room_scan_event_source_counts_as_thread_child_proof()`.
3. Several modules repeat the simple explicit thread-id extraction `event_info.thread_id or event_info.thread_id_from_edit`, but this is low-risk related duplication rather than a strong refactor target by itself.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SupportsEventId	class	lines 31-34	related-only	SupportsEventId event_id Protocol snapshot entries	src/mindroom/matrix/thread_projection.py:384; src/mindroom/matrix/client_thread_history.py:1022
ThreadRootProofState	class	lines 41-46	none-found	ThreadRootProofState PROVEN NOT_A_THREAD_ROOT PROOF_UNAVAILABLE	none
ThreadRootProof	class	lines 50-69	related-only	ThreadRootProof proven not_a_thread_root proof_unavailable	src/mindroom/matrix/thread_bookkeeping.py:21; src/mindroom/matrix/thread_bookkeeping.py:96; src/mindroom/matrix/thread_bookkeeping.py:347; src/mindroom/matrix/thread_bookkeeping.py:353; src/mindroom/matrix/thread_bookkeeping.py:366
ThreadRootProof.proven	method	lines 57-59	related-only	ThreadRootProof.proven root proof children	src/mindroom/matrix/thread_bookkeeping.py:347; src/mindroom/matrix/thread_bookkeeping.py:366
ThreadRootProof.not_a_thread_root	method	lines 62-64	related-only	ThreadRootProof.not_a_thread_root no children	src/mindroom/matrix/thread_bookkeeping.py:366
ThreadRootProof.proof_unavailable	method	lines 67-69	related-only	ThreadRootProof.proof_unavailable lookup failure	src/mindroom/matrix/thread_bookkeeping.py:353; src/mindroom/matrix/thread_bookkeeping.py:355
ThreadResolutionState	class	lines 72-77	none-found	ThreadResolutionState THREADED ROOM_LEVEL INDETERMINATE	none
ThreadResolution	class	lines 81-106	related-only	ThreadResolution state thread_id error mutation impact	src/mindroom/matrix/thread_bookkeeping.py:57; src/mindroom/matrix/thread_bookkeeping.py:65
ThreadResolution.threaded	method	lines 89-91	related-only	ThreadResolution.threaded MutationThreadImpact.threaded	src/mindroom/matrix/thread_bookkeeping.py:72
ThreadResolution.room_level	method	lines 94-96	related-only	ThreadResolution.room_level MutationThreadImpact.room_level	src/mindroom/matrix/thread_bookkeeping.py:77
ThreadResolution.indeterminate	method	lines 99-101	related-only	ThreadResolution.indeterminate MutationThreadImpact.unknown	src/mindroom/matrix/thread_bookkeeping.py:82
ThreadResolution.is_threaded	method	lines 104-106	none-found	is_threaded resolution state THREADED	none
ThreadMembershipProofError	class	lines 109-110	none-found	ThreadMembershipProofError proof unavailable	none
ThreadMembershipLookupError	class	lines 113-114	related-only	ThreadMembershipLookupError lookup unavailable	src/mindroom/matrix/thread_bookkeeping.py:231; src/mindroom/matrix/thread_bookkeeping.py:322; src/mindroom/matrix/thread_bookkeeping.py:356
ThreadRoomScanRootNotFoundError	class	lines 117-118	none-found	ThreadRoomScanRootNotFoundError root not found	none
_next_related_event_target	function	lines 121-127	none-found	next_related_event_id current_event_id original reference reply	none
ThreadMembershipAccess	class	lines 131-136	related-only	ThreadMembershipAccess lookup_thread_id fetch_event_info prove_thread_root	src/mindroom/matrix/thread_bookkeeping.py:371; src/mindroom/matrix/thread_room_scan.py:85
_resolution_from_root_proof	function	lines 139-149	none-found	root proof to thread resolution proven room level indeterminate	none
_strict_thread_id_from_resolution	function	lines 152-161	none-found	strict thread id proof unavailable resolution error	none
resolve_event_thread_membership	async_function	lines 164-188	related-only	resolve event thread membership explicit thread_id related event current root	src/mindroom/matrix/thread_bookkeeping.py:254; src/mindroom/matrix/cache/thread_writes.py:364; src/mindroom/matrix/cache/sqlite_event_cache_events.py:495; src/mindroom/matrix/cache/postgres_event_cache_events.py:585
resolve_related_event_thread_membership	async_function	lines 191-240	none-found	resolve related event thread membership hops visited fetch event info prove root	none
resolve_event_thread_id	async_function	lines 243-259	related-only	resolve event thread id strict wrapper	src/mindroom/matrix/conversation_cache.py:84
resolve_related_event_thread_id	async_function	lines 262-274	none-found	resolve related event thread id strict wrapper	none
resolve_event_thread_id_best_effort	async_function	lines 277-293	related-only	resolve event thread id best effort wrapper	src/mindroom/conversation_resolver.py:341
resolve_related_event_thread_id_best_effort	async_function	lines 296-308	related-only	resolve related event thread id best effort wrapper	src/mindroom/bot.py:723; src/mindroom/conversation_resolver.py:382
map_backed_thread_membership_access	function	lines 311-342	duplicate-found	map backed thread membership access event_infos resolved_thread_ids child proof	src/mindroom/matrix/thread_bookkeeping.py:328; src/mindroom/matrix/thread_bookkeeping.py:339; src/mindroom/matrix/thread_bookkeeping.py:422
map_backed_thread_membership_access.<locals>.lookup_thread_id	nested_async_function	lines 318-319	related-only	resolved_thread_ids get event id cached_thread_ids page_resolved_thread_ids	src/mindroom/matrix/thread_bookkeeping.py:289; src/mindroom/matrix/thread_bookkeeping.py:379
map_backed_thread_membership_access.<locals>.fetch_event_info	nested_async_function	lines 321-322	related-only	event_infos get event id page_event_infos cached_event_infos	src/mindroom/matrix/thread_bookkeeping.py:306; src/mindroom/matrix/thread_bookkeeping.py:386
map_backed_thread_membership_access.<locals>.prove_thread_root	nested_async_function	lines 324-336	duplicate-found	page event infos counts as thread child proof	src/mindroom/matrix/thread_bookkeeping.py:328; src/mindroom/matrix/thread_bookkeeping.py:339; src/mindroom/matrix/thread_bookkeeping.py:422
_is_thread_root_not_found_error	function	lines 345-347	none-found	ThreadRoomScanRootNotFoundError isinstance not found proof	none
thread_messages_root_proof	async_function	lines 350-364	related-only	thread messages root proof has children event_id != root	src/mindroom/matrix/thread_bookkeeping.py:350; src/mindroom/matrix/thread_bookkeeping.py:359
snapshot_thread_root_proof	async_function	lines 367-378	none-found	snapshot thread root proof fetch thread snapshot	none
room_scan_thread_root_proof	async_function	lines 381-403	related-only	room scan root proof event sources root_found children	src/mindroom/matrix/thread_room_scan.py:30; src/mindroom/matrix/thread_bookkeeping.py:350
_room_scan_event_source_counts_as_thread_child_proof	function	lines 406-416	duplicate-found	event source counts as thread child proof edit original root	src/mindroom/matrix/thread_bookkeeping.py:407; src/mindroom/matrix/thread_bookkeeping.py:422
thread_messages_thread_membership_access	function	lines 419-438	related-only	thread membership access adapter prove_thread_root closure	src/mindroom/matrix/thread_bookkeeping.py:371; src/mindroom/matrix/thread_room_scan.py:85
thread_messages_thread_membership_access.<locals>.prove_thread_root	nested_async_function	lines 427-432	related-only	prove_thread_root closure delegates thread_messages_root_proof	src/mindroom/matrix/thread_bookkeeping.py:393; src/mindroom/matrix/thread_room_scan.py:112
snapshot_thread_membership_access	function	lines 441-452	none-found	snapshot thread membership access fetch_thread_snapshot	none
room_scan_thread_membership_access	function	lines 455-474	related-only	room scan thread membership access adapter	src/mindroom/matrix/thread_room_scan.py:85
room_scan_thread_membership_access.<locals>.prove_thread_root	nested_async_function	lines 463-468	related-only	prove_thread_root closure delegates room_scan_thread_root_proof	src/mindroom/matrix/thread_room_scan.py:112
lookup_thread_id_from_conversation_cache	async_function	lines 477-485	duplicate-found	lookup thread id from conversation cache get_thread_id_for_event none	src/mindroom/matrix/thread_room_scan.py:58
_event_info_from_lookup_response	function	lines 488-503	duplicate-found	RoomGetEventResponse RoomGetEventError M_NOT_FOUND EventInfo.from_event	src/mindroom/matrix/thread_room_scan.py:40; src/mindroom/matrix/conversation_cache.py:555; src/mindroom/custom_tools/matrix_api.py:1277
fetch_event_info_from_conversation_cache	async_function	lines 506-519	duplicate-found	fetch event info from conversation cache get_event strict lookup response	src/mindroom/matrix/thread_room_scan.py:69; src/mindroom/matrix/conversation_cache.py:555
fetch_event_info_for_client	async_function	lines 522-535	related-only	room_get_event fetch event info for client lookup response	src/mindroom/custom_tools/matrix_api.py:1267; src/mindroom/matrix/stale_stream_cleanup.py:963
```

## Findings

### 1. Duplicate room-get-event response normalization

`src/mindroom/matrix/thread_membership.py:488` and `src/mindroom/matrix/thread_room_scan.py:40` contain the same `_event_info_from_lookup_response()` behavior:

- `RoomGetEventResponse` becomes `EventInfo.from_event(response.event.source)`.
- non-strict failures return `None`.
- strict `M_NOT_FOUND` returns `None`.
- other failures raise `RuntimeError(f"Failed to resolve Matrix event {event_id}: {detail}")`.

The same responsibility appears in narrower form in `src/mindroom/matrix/conversation_cache.py:555`, which resolves `EventInfo` from a cache lookup but returns `None` for all non-response values instead of supporting strict error behavior.
`src/mindroom/custom_tools/matrix_api.py:1277` also handles `RoomGetEventError` and `RoomGetEventResponse`, but it returns tool payloads rather than `EventInfo`, so it is related only.

Differences to preserve:

- `thread_membership.py` accepts `object` because it is shared by direct-client and cache paths.
- `thread_room_scan.py` uses the narrower `EventLookupResult` alias.
- tool-facing code must preserve structured payload output.

### 2. Duplicate conversation-cache thread-id and event-info adapters

`src/mindroom/matrix/thread_membership.py:477` duplicates `src/mindroom/matrix/thread_room_scan.py:58`.
Both helpers return `None` when the conversation cache is unavailable and otherwise call `get_thread_id_for_event(room_id, event_id)`.

`src/mindroom/matrix/thread_membership.py:506` duplicates `src/mindroom/matrix/thread_room_scan.py:69`.
Both helpers fetch `conversation_cache.get_event(room_id, event_id)` and normalize the room-get-event response into `EventInfo`.

Differences to preserve:

- `thread_room_scan.py` currently exposes its helper names through `__all__` for `thread_bookkeeping.py`.
- `thread_membership.py` types against `ConversationCacheProtocol`, while `thread_room_scan.py` uses a smaller `RoomScanConversationCache` protocol.

### 3. Duplicate thread-child proof predicates

`src/mindroom/matrix/thread_membership.py:324` checks whether any map-backed event other than the candidate root has `event_info.thread_id` or `event_info.thread_id_from_edit` equal to the candidate root.
`src/mindroom/matrix/thread_bookkeeping.py:422` implements the same page-local predicate as `_page_event_info_counts_as_thread_child_proof()`.

`src/mindroom/matrix/thread_membership.py:406` and `src/mindroom/matrix/thread_bookkeeping.py:407` both parse an event source, reject the root event itself, and reject edits whose original event is the candidate root.
The bookkeeping version additionally requires `event_info.thread_id == thread_root_id`, while the room-scan version treats any non-root, non-root-edit event returned by the authoritative room scan as child proof.

Differences to preserve:

- For map/page-local event-info proof, edits carrying `thread_id_from_edit` are accepted.
- For cached event-source proof, bookkeeping requires an explicit thread relation because cached thread events may include stored root/self rows.
- For room scans, the source collection is already filtered by the Matrix room scan for the candidate thread, so the predicate intentionally does not require `event_info.thread_id == thread_root_id`.

### 4. Repeated explicit thread-id extraction

`src/mindroom/matrix/thread_membership.py:173`, `src/mindroom/matrix/thread_membership.py:220`, `src/mindroom/matrix/thread_bookkeeping.py:264`, `src/mindroom/matrix/cache/thread_writes.py:366`, `src/mindroom/matrix/cache/sqlite_event_cache_events.py:501`, and `src/mindroom/matrix/cache/postgres_event_cache_events.py:592` all choose `event_info.thread_id` first and fall back to `event_info.thread_id_from_edit`.

This is real repeated behavior, but it is a small expression and some call sites also validate non-empty strings or shape storage rows.
I would not refactor this alone unless the thread-child proof predicates are already being touched.

## Proposed Generalization

1. Move the duplicated room-get-event normalization and cache lookup helpers into one public section of `thread_membership.py`, then update `thread_room_scan.py` to import them instead of keeping private copies.
2. Add a tiny pure helper such as `event_info_has_thread_child_proof(thread_root_id: str, *, event_id: str, event_info: EventInfo) -> bool` in `thread_membership.py` and use it from both `map_backed_thread_membership_access()` and `thread_bookkeeping._page_event_info_counts_as_thread_child_proof()`.
3. If touching cached source proof code, add a parameterized source predicate only if it remains explicit about the `require_explicit_thread_id` difference between room scans and cache bookkeeping.
4. Leave `ThreadResolution`, `ThreadRootProof`, and wrapper functions as the canonical source of truth; they are reused by callers rather than duplicated.

## Risk/tests

Primary risk is weakening strict lookup behavior by changing which errors return `None` versus raise.
Tests should cover `RoomGetEventResponse`, strict and non-strict non-found, strict non-not-found errors, and a conversation-cache `None` path.

Thread-child proof tests should cover root event exclusion, root-edit exclusion, explicit thread child, edit child via `thread_id_from_edit`, and the bookkeeping-specific requirement that cached event sources must explicitly target the candidate thread.

No production code was edited.
