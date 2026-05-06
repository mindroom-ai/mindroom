Summary: One small literal duplication exists: `_event_id_from_source` is defined both in `thread_projection.py` and `client_thread_history.py`.
The rest of this module mostly acts as the shared implementation for thread ordering, scanned-event membership resolution, and latest visible thread-tail projection.
Related ordering exists in cache SQL and edit selection, but it is not functionally identical enough to justify a source-level refactor.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SupportsThreadMessageOrdering	class	lines 17-21	related-only	"event_id timestamp protocol ResolvedVisibleMessage thread messages ordering"	src/mindroom/matrix/client_visible_messages.py:25; src/mindroom/matrix/thread_membership.py:31
SupportsVisibleThreadMessage	class	lines 24-32	related-only	"visible_event_id thread_id latest_event_id content cleanup message protocol"	src/mindroom/matrix/client_visible_messages.py:25; src/mindroom/matrix/stale_stream_cleanup.py:513
_thread_history_input_order	function	lines 35-42	related-only	"input_order_by_event_id timestamp tie stable order rowid write_seq"	src/mindroom/matrix/client_thread_history.py:267; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:36; src/mindroom/matrix/cache/postgres_event_cache_threads.py:61
_build_same_timestamp_relation_graph	function	lines 45-59	none-found	"same timestamp relation graph in_degree children_by_parent heapq topological"	none
_topologically_sort_same_timestamp_event_ids	function	lines 62-117	none-found	"topologically sort same timestamp event ids parent child relation"	none
_sort_same_timestamp_group	function	lines 120-140	none-found	"same timestamp group ancestry aware sort relation"	none
sort_thread_items_root_first	function	lines 143-183	related-only	"root first chronological thread sort timestamp event_id origin_server_ts"	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:36; src/mindroom/matrix/cache/postgres_event_cache_threads.py:61; src/mindroom/matrix/client_thread_history.py:325
sort_thread_messages_root_first	function	lines 186-201	related-only	"sort_thread_messages_root_first thread messages root first ResolvedVisibleMessage"	src/mindroom/matrix/client_thread_history.py:325
_event_id_from_source	function	lines 204-207	duplicate-found	"def _event_id_from_source event_source get event_id isinstance str"	src/mindroom/matrix/client_thread_history.py:218
sort_thread_event_sources_root_first	function	lines 210-236	related-only	"sort raw event sources root first origin_server_ts EventInfo next_related_event_id"	src/mindroom/matrix/client_thread_history.py:1070; src/mindroom/matrix/client_thread_history.py:1152
ordered_event_ids_from_scanned_event_sources	function	lines 239-260	related-only	"ordered_event_ids_from_scanned_event_sources scanned origin_server_ts input order ties"	src/mindroom/matrix/client_thread_history.py:1056; src/mindroom/matrix/stale_stream_cleanup.py:646; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:48; src/mindroom/matrix/cache/postgres_event_cache_threads.py:75
_visible_thread_message_is_better_candidate	function	lines 263-276	related-only	"latest visible candidate timestamp latest_event_id thread_id better candidate edit tie"	src/mindroom/matrix/client_visible_messages.py:380; src/mindroom/matrix/stale_stream_cleanup.py:594
_visible_thread_key	function	lines 279-288	none-found	"visible thread key thread root ids event_id thread_id cleanup bucket"	none
_related_event_id_by_visible_event_id	function	lines 291-302	related-only	"related_event_id_by_visible_event_id EventInfo from content next_related_event_id"	src/mindroom/matrix/client_thread_history.py:267; src/mindroom/matrix/event_info.py:80
latest_visible_thread_event_id_by_thread	function	lines 305-344	related-only	"latest visible thread event id by thread stale stream cleanup latest_thread_event_id"	src/mindroom/matrix/stale_stream_cleanup.py:513; src/mindroom/matrix/client_visible_messages.py:380
resolve_thread_ids_for_event_infos	async_function	lines 347-381	related-only	"resolve thread ids for event infos fixpoint map backed thread membership"	src/mindroom/matrix/thread_membership.py:311; src/mindroom/matrix/client_thread_history.py:1056; src/mindroom/matrix/stale_stream_cleanup.py:649; src/mindroom/matrix/thread_bookkeeping.py:204
```

Findings:

1. Duplicate raw Matrix event-id extraction helper.
   `src/mindroom/matrix/thread_projection.py:204` and `src/mindroom/matrix/client_thread_history.py:218` both implement the same behavior: read `event_id` from a mapping and return it only when it is a string.
   The behavior is functionally identical.
   The only difference is local ownership: `thread_projection` uses it for raw projection helpers, while `client_thread_history` uses it when rebuilding a dict after sorted scanned sources.

Related but not duplicate:

1. Chronological thread ordering appears in SQL cache reads at `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:36` and `src/mindroom/matrix/cache/postgres_event_cache_threads.py:61`.
   These order persisted thread rows by timestamp plus storage insertion sequence (`rowid` or `write_seq`), while `sort_thread_items_root_first` also handles root-first placement, event-id tie breaking, input-order preservation, and same-timestamp relation ancestry.
   This is related ordering policy, not a duplicate implementation, because the database queries cannot preserve the same parent-child graph ordering without loading and projecting event content.

2. Latest visible candidate selection is related to edit selection in `src/mindroom/matrix/client_visible_messages.py:380`.
   Both choose a newest visible state using timestamp and event-id-like tie breakers.
   The semantics differ: edit selection compares edit events for one original event, while `_visible_thread_message_is_better_candidate` prefers threaded copies and compares `latest_event_id` for cleanup thread-tail projection.

3. Thread-membership resolution in `resolve_thread_ids_for_event_infos` intentionally delegates to `src/mindroom/matrix/thread_membership.py:311`.
   Call sites in `src/mindroom/matrix/client_thread_history.py:1056`, `src/mindroom/matrix/stale_stream_cleanup.py:649`, and `src/mindroom/matrix/thread_bookkeeping.py:204` use this helper rather than duplicating the fixpoint loop.

Proposed generalization:

1. Optionally move the duplicated `_event_id_from_source` helper to a small shared location already used for raw event normalization, such as `mindroom.matrix.event_info` or a Matrix event-source utility module, and import it from both modules.
2. Keep all ordering and visible-thread projection behavior in `mindroom.matrix.thread_projection`.
3. Do not refactor the cache SQL ordering into Python projection helpers unless a caller requires ancestry-aware ordering from cached rows before history hydration.

Risk/tests:

The only recommended refactor is low risk but would touch imports in two modules.
Tests that should cover it are existing thread-history and thread-projection users: `tests/test_thread_history.py`, `tests/test_threading_error.py`, and `tests/test_stale_stream_cleanup.py`.
No production code was edited for this audit.
