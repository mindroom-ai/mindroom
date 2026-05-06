Summary: The main duplication in `src/mindroom/matrix/thread_room_scan.py` is exact or near-exact cache/event lookup normalization already implemented in `src/mindroom/matrix/thread_membership.py`.
The room-scan membership access builder is related to other `ThreadMembershipAccess` adapter builders, but its room-scan client binding is narrow enough that no broad refactor is recommended from this file alone.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
RoomScanConversationCache	class	lines 20-27	related-only	RoomScanConversationCache ConversationCacheProtocol get_event get_thread_id_for_event	src/mindroom/matrix/conversation_cache.py:127; src/mindroom/matrix/thread_membership.py:477
RoomScanConversationCache.get_event	async_method	lines 23-24	related-only	get_event RoomGetEventResponse RoomGetEventError EventLookupResult	src/mindroom/matrix/conversation_cache.py:133; src/mindroom/matrix/conversation_cache.py:472; src/mindroom/matrix/cache/event_cache.py:53
RoomScanConversationCache.get_thread_id_for_event	async_method	lines 26-27	related-only	get_thread_id_for_event cached thread root	src/mindroom/matrix/conversation_cache.py:183; src/mindroom/matrix/conversation_cache.py:888; src/mindroom/matrix/cache/event_cache.py:140
_scan_thread_event_sources	async_function	lines 30-37	none-found	_fetch_thread_event_sources_via_room_messages event_sources root_found room_scan_thread_root_proof	src/mindroom/matrix/client_thread_history.py:67; src/mindroom/matrix/thread_membership.py:381
_event_info_from_lookup_response	function	lines 40-55	duplicate-found	_event_info_from_lookup_response RoomGetEventResponse M_NOT_FOUND Failed to resolve Matrix event	src/mindroom/matrix/thread_membership.py:488; src/mindroom/conversation_resolver.py:434
_lookup_thread_id_from_conversation_cache	async_function	lines 58-66	duplicate-found	lookup_thread_id_from_conversation_cache conversation_cache None get_thread_id_for_event	src/mindroom/matrix/thread_membership.py:477; src/mindroom/matrix/conversation_cache.py:103
_fetch_event_info_from_conversation_cache	async_function	lines 69-82	duplicate-found	fetch_event_info_from_conversation_cache get_event _event_info_from_lookup_response strict	src/mindroom/matrix/thread_membership.py:506; src/mindroom/matrix/thread_bookkeeping.py:150
_room_scan_membership_access_for_client	function	lines 85-120	related-only	room_scan_membership_access_for_client room_scan_thread_membership_access ThreadMembershipAccess lookup_thread_id fetch_event_info	src/mindroom/matrix/thread_membership.py:455; src/mindroom/conversation_resolver.py:396; src/mindroom/matrix/thread_bookkeeping.py:371
_room_scan_membership_access_for_client.<locals>.lookup_thread_id	nested_async_function	lines 93-98	duplicate-found	lookup_thread_id nested get_thread_id_for_event _lookup_thread_id_from_conversation_cache	src/mindroom/matrix/thread_membership.py:477; src/mindroom/conversation_resolver.py:404; src/mindroom/matrix/thread_bookkeeping.py:379
_room_scan_membership_access_for_client.<locals>.resolved_fetch_event_info	nested_async_function	lines 100-110	related-only	resolved_fetch_event_info fetch_event_info override conversation_cache None strict True	src/mindroom/matrix/thread_membership.py:506; src/mindroom/matrix/thread_membership.py:522; src/mindroom/conversation_resolver.py:434
```

Findings:

1. `thread_room_scan.py` duplicates cache-backed event-info normalization helpers from `thread_membership.py`.
   `src/mindroom/matrix/thread_room_scan.py:40` has `_event_info_from_lookup_response`, and `src/mindroom/matrix/thread_membership.py:488` has the same behavior: accept a `RoomGetEventResponse`, return `EventInfo.from_event(response.event.source)`, return `None` for non-strict failures and strict `M_NOT_FOUND`, and raise `RuntimeError` with Matrix event detail otherwise.
   `src/mindroom/matrix/thread_room_scan.py:69` duplicates `src/mindroom/matrix/thread_membership.py:506` by calling `conversation_cache.get_event()` and passing the result into that normalizer with the same `event_id` and `strict` controls.
   Difference to preserve: the room-scan copy is typed against `RoomScanConversationCache`, while the shared helper uses `ConversationCacheProtocol`.

2. `thread_room_scan.py` duplicates cached thread-id lookup.
   `src/mindroom/matrix/thread_room_scan.py:58` and `src/mindroom/matrix/thread_membership.py:477` both return `None` when no cache is available and otherwise delegate to `conversation_cache.get_thread_id_for_event(room_id, event_id)`.
   Difference to preserve: only the protocol type differs; the runtime behavior is identical.

3. `thread_room_scan.py` has related but not fully duplicate `ThreadMembershipAccess` adapter construction.
   `src/mindroom/matrix/thread_room_scan.py:85` builds a client-backed room-scan access object by adapting cache lookup, optional direct event fetch, and `_fetch_thread_event_sources_via_room_messages`.
   `src/mindroom/matrix/thread_membership.py:455`, `src/mindroom/conversation_resolver.py:396`, and `src/mindroom/matrix/thread_bookkeeping.py:371` build similar access objects, but each binds a different proof source or mutation context.
   This is an adapter pattern repeated across the thread-membership code, but only the nested `lookup_thread_id` body is directly duplicate.

Proposed generalization:

Use the existing shared helpers in `src/mindroom/matrix/thread_membership.py` instead of the private duplicates in `thread_room_scan.py`.
That would mean importing `fetch_event_info_from_conversation_cache` and `lookup_thread_id_from_conversation_cache`, then removing `_event_info_from_lookup_response`, `_lookup_thread_id_from_conversation_cache`, and `_fetch_event_info_from_conversation_cache` from `thread_room_scan.py`.
To keep the narrower protocol, either widen the shared helper type to a small event/thread lookup protocol in `thread_membership.py`, or make `RoomScanConversationCache` satisfy the existing `ConversationCacheProtocol` only if that broader protocol is intentionally acceptable.
No refactor is recommended for `_room_scan_membership_access_for_client` beyond using the shared cache helpers, because its scan-source binding is specific and still small.

Risk/tests:

The main risk is type-surface churn: `ConversationCacheProtocol` includes more methods than `RoomScanConversationCache`, so blindly reusing the helper signatures could force room-scan callers to depend on a larger protocol than they need.
Tests should cover strict and non-strict lookup behavior for `RoomGetEventResponse`, `RoomGetEventError(status_code="M_NOT_FOUND")`, and other `RoomGetEventError` responses.
Existing tests around `resolve_thread_root_event_id_for_client`, `resolve_event_thread_impact_for_client`, and redaction thread impact would be the most relevant regression targets.
