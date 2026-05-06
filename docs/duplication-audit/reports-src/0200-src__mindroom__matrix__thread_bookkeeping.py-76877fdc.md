## Summary

One small duplication candidate exists in thread-root child proof checks.
`ThreadMutationResolver._prove_thread_root_for_mutation_context` uses `_page_event_info_counts_as_thread_child_proof`, while `map_backed_thread_membership_access.prove_thread_root` in `src/mindroom/matrix/thread_membership.py` repeats the same page-local `event_id != root` plus `thread_id/thread_id_from_edit == root` predicate.
Most other symbols in `thread_bookkeeping.py` are mutation-specific adapters around the canonical `thread_membership.py` resolver, not duplicate implementations.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
is_thread_affecting_relation	function	lines 45-49	related-only	is_thread_affecting_relation is_thread is_edit is_reply m.reference relation_type	src/mindroom/matrix/cache/thread_writes.py:52; src/mindroom/matrix/event_info.py:80; src/mindroom/matrix/thread_membership.py:164
_redaction_can_affect_thread_cache	function	lines 52-54	none-found	redaction thread cache is_reaction m.annotation redact reaction	src/mindroom/matrix/cache/thread_writes.py:180; src/mindroom/matrix/cache/thread_write_cache_ops.py:164; src/mindroom/custom_tools/matrix_api.py:709
MutationThreadImpactState	class	lines 57-62	related-only	MutationThreadImpactState ThreadResolutionState THREADED ROOM_LEVEL UNKNOWN INDETERMINATE	src/mindroom/matrix/thread_membership.py:72; src/mindroom/matrix/cache/thread_writes.py:138; src/mindroom/matrix/cache/thread_write_cache_ops.py:176
MutationThreadImpact	class	lines 66-85	related-only	MutationThreadImpact ThreadResolution dataclass state thread_id error	src/mindroom/matrix/thread_membership.py:80; src/mindroom/matrix/cache/thread_writes.py:124; src/mindroom/custom_tools/matrix_api.py:724
MutationThreadImpact.threaded	method	lines 73-75	related-only	ThreadResolution.threaded MutationThreadImpact.threaded thread_id	src/mindroom/matrix/thread_membership.py:88; src/mindroom/matrix/thread_bookkeeping.py:99
MutationThreadImpact.room_level	method	lines 78-80	related-only	ThreadResolution.room_level MutationThreadImpact.room_level	src/mindroom/matrix/thread_membership.py:93; src/mindroom/matrix/thread_bookkeeping.py:99
MutationThreadImpact.unknown	method	lines 83-85	related-only	ThreadResolution.indeterminate MutationThreadImpact.unknown proof unavailable	src/mindroom/matrix/thread_membership.py:98; src/mindroom/matrix/thread_bookkeeping.py:99
MutationResolutionContext	class	lines 89-96	related-only	page_event_infos page_resolved_thread_ids cached_event_infos cached_thread_root_proofs	src/mindroom/matrix/thread_projection.py:347; src/mindroom/matrix/thread_membership.py:311; src/mindroom/matrix/cache/thread_writes.py:958
_mutation_thread_impact_from_resolution	function	lines 99-108	related-only	ThreadResolutionState MutationThreadImpact from resolution mapping	src/mindroom/matrix/thread_membership.py:139; src/mindroom/matrix/thread_membership.py:152; src/mindroom/custom_tools/matrix_api.py:724
resolve_event_thread_impact_for_client	async_function	lines 111-131	related-only	resolve_event_thread_membership client conversation_cache impact	src/mindroom/matrix/thread_membership.py:164; src/mindroom/matrix/thread_room_scan.py:85; src/mindroom/custom_tools/matrix_api.py:779
resolve_redaction_thread_impact_for_client	async_function	lines 134-166	related-only	resolve_redaction_thread_impact_for_client resolve_related_event_thread_membership redact conversation_cache	src/mindroom/matrix/thread_bookkeeping.py:214; src/mindroom/custom_tools/matrix_api.py:709; src/mindroom/matrix/cache/thread_writes.py:180
ThreadMutationResolver	class	lines 169-404	related-only	ThreadMutationResolver thread_membership_access mutation resolver resolve_thread_impact	src/mindroom/matrix/thread_membership.py:164; src/mindroom/matrix/thread_room_scan.py:85; src/mindroom/matrix/cache/thread_writes.py:657
ThreadMutationResolver.__init__	method	lines 172-181	none-found	logger_getter runtime fetch_event_info_for_thread_resolution constructor	src/mindroom/matrix/cache/thread_writes.py:247; src/mindroom/matrix/cache/thread_write_cache_ops.py:23
ThreadMutationResolver.logger	method	lines 184-186	related-only	logger property logger_getter facade bound logger	src/mindroom/matrix/cache/thread_write_cache_ops.py:57; src/mindroom/matrix/cache/thread_writes.py:261
ThreadMutationResolver.build_sync_mutation_resolution_context	async_method	lines 188-212	related-only	page_event_infos ordered_event_ids resolve_thread_ids_for_event_infos sync batch	src/mindroom/matrix/thread_projection.py:347; src/mindroom/matrix/cache/thread_writes.py:958
ThreadMutationResolver.resolve_redaction_thread_impact	async_method	lines 214-252	related-only	resolve_related_event_thread_membership redaction impact ThreadMembershipLookupError	src/mindroom/matrix/thread_bookkeeping.py:134; src/mindroom/matrix/cache/thread_writes.py:180; src/mindroom/matrix/thread_membership.py:191
ThreadMutationResolver.resolve_thread_impact_for_mutation	async_method	lines 254-287	related-only	explicit_thread_id resolve_event_thread_membership thread impact mutation	src/mindroom/matrix/thread_membership.py:164; src/mindroom/matrix/thread_projection.py:347; src/mindroom/matrix/cache/thread_writes.py:669
ThreadMutationResolver._lookup_thread_id_for_mutation_context	async_method	lines 289-304	related-only	page_resolved_thread_ids cached_thread_ids get_thread_id_for_event lookup_thread_id	src/mindroom/matrix/thread_membership.py:318; src/mindroom/matrix/thread_room_scan.py:58; src/mindroom/matrix/cache/event_cache.py:140
ThreadMutationResolver._event_info_for_mutation_context	async_method	lines 306-326	related-only	page_event_infos cached_event_infos fetch_event_info ThreadMembershipLookupError	src/mindroom/matrix/thread_membership.py:321; src/mindroom/matrix/thread_room_scan.py:69; src/mindroom/matrix/thread_room_scan.py:100
ThreadMutationResolver._prove_thread_root_for_mutation_context	async_method	lines 328-369	duplicate-found	prove_thread_root page_event_info_counts_as_thread_child_proof thread_root_id has_children	src/mindroom/matrix/thread_membership.py:324; src/mindroom/matrix/thread_membership.py:381; src/mindroom/matrix/thread_bookkeeping.py:422
ThreadMutationResolver._thread_membership_access	method	lines 371-404	related-only	ThreadMembershipAccess lookup_thread_id fetch_event_info prove_thread_root nested access	src/mindroom/matrix/thread_membership.py:311; src/mindroom/matrix/thread_room_scan.py:85; src/mindroom/matrix/thread_membership.py:419
ThreadMutationResolver._thread_membership_access.<locals>.lookup_thread_id	nested_async_function	lines 379-384	related-only	lookup_thread_id nested ThreadMembershipAccess resolved_thread_ids get_thread_id_for_event	src/mindroom/matrix/thread_membership.py:318; src/mindroom/matrix/thread_room_scan.py:93
ThreadMutationResolver._thread_membership_access.<locals>.fetch_event_info	nested_async_function	lines 386-391	related-only	fetch_event_info nested ThreadMembershipAccess EventInfo	src/mindroom/matrix/thread_membership.py:321; src/mindroom/matrix/thread_room_scan.py:100
ThreadMutationResolver._thread_membership_access.<locals>.prove_thread_root	nested_async_function	lines 393-398	related-only	prove_thread_root nested ThreadMembershipAccess ThreadRootProof	src/mindroom/matrix/thread_membership.py:324; src/mindroom/matrix/thread_membership.py:427; src/mindroom/matrix/thread_room_scan.py:112
_event_source_counts_as_thread_child_proof	function	lines 407-419	related-only	event_source_counts thread child proof event_id edit original_event_id thread_id	src/mindroom/matrix/thread_membership.py:406; src/mindroom/matrix/client_thread_history.py:301; src/mindroom/matrix/cache/postgres_event_cache_events.py:582
_page_event_info_counts_as_thread_child_proof	function	lines 422-437	duplicate-found	page_event_info_counts thread_id_from_edit thread_id event_id root map_backed_thread_membership_access	src/mindroom/matrix/thread_membership.py:324; src/mindroom/matrix/thread_projection.py:347; src/mindroom/matrix/thread_bookkeeping.py:339
```

## Findings

### 1. Page-local thread child proof predicate is duplicated

`src/mindroom/matrix/thread_bookkeeping.py:422` defines `_page_event_info_counts_as_thread_child_proof`.
It returns false for the root event itself, then treats either `event_info.thread_id` or `event_info.thread_id_from_edit` as child proof when it matches the candidate root.

`src/mindroom/matrix/thread_membership.py:324` repeats the same predicate inline inside `map_backed_thread_membership_access.prove_thread_root`.
The two implementations are functionally the same for page-local `EventInfo` maps: skip the root event, then scan `thread_id` and `thread_id_from_edit`.

Difference to preserve: the `thread_bookkeeping.py` helper is private to mutation bookkeeping, while `thread_membership.py` owns canonical membership access.
Moving this exact predicate into `thread_membership.py` would better match ownership, but it would expose or reuse a small helper across modules.

### Related, not duplicate: event-source child proof variants

`src/mindroom/matrix/thread_bookkeeping.py:407` and `src/mindroom/matrix/thread_membership.py:406` both inspect raw event sources for thread-root proof, but they do not have the same semantics.
The bookkeeping variant only proves a child when the raw event has `thread_id == thread_root_id`, and it excludes edits of the root.
The room-scan variant only needs proof that the authoritative scan returned any non-root, non-root-edit event source, because the scan was already scoped to the requested thread root.
No shared helper is recommended for these two functions.

### Related, not duplicate: mutation impact versus canonical membership

`MutationThreadImpact`, `MutationThreadImpactState`, and `_mutation_thread_impact_from_resolution` mirror `ThreadResolution` and `ThreadResolutionState` from `src/mindroom/matrix/thread_membership.py`.
This is an adapter boundary rather than accidental duplication: canonical membership has `INDETERMINATE` with an optional error, while mutation bookkeeping needs `UNKNOWN` so cache writers can fail closed through room invalidation.
No refactor is recommended.

### Related, not duplicate: `ThreadMembershipAccess` adapters

`ThreadMutationResolver._thread_membership_access`, `map_backed_thread_membership_access`, and `_room_scan_membership_access_for_client` all build `ThreadMembershipAccess`, but each binds a different lookup source and failure policy.
The nested function shape is repeated by design because `ThreadMembershipAccess` is the canonical dependency-injection protocol for thread resolution.
No generalization is recommended beyond the predicate in finding 1.

## Proposed Generalization

Move the page-local `EventInfo` child-proof predicate to `src/mindroom/matrix/thread_membership.py`, for example as a private helper near `map_backed_thread_membership_access`.
Use it both in `map_backed_thread_membership_access.prove_thread_root` and in `thread_bookkeeping.py`.
Keep the helper narrow:

1. Accept `thread_root_id`, `event_id`, and `event_info`.
2. Return false when `event_id == thread_root_id`.
3. Return true when either `event_info.thread_id` or `event_info.thread_id_from_edit` equals `thread_root_id`.
4. Do not merge it with raw event-source proof helpers, which have different source guarantees.

## Risk/tests

Behavior risk is low if the helper is mechanically extracted, but it sits on thread membership proof and cache invalidation paths, so regressions could cause room-level events to be treated as threaded or thread roots to be missed.
Tests should cover sync page-local proof for explicit thread messages, edits whose `m.new_content` carries a thread relation, and root-event self-exclusion.
Existing tests around `resolve_thread_ids_for_event_infos`, `ThreadMutationResolver._prove_thread_root_for_mutation_context`, and redaction/thread cache invalidation would be the right targets.
