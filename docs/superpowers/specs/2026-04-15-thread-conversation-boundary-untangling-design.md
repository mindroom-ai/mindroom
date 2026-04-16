# Thread Conversation Boundary Untangling Design

## Goal

Untangle the remaining thread and conversation boundary cluster so thread identity policy, mutation impact policy, and stale-stream cleanup each have a clearer owner.

This branch keeps public behavior stable.

This branch does not broaden Tach enforcement or redesign the full `mindroom.matrix.cache` package surface.

## Current Problem

`thread_membership.py` already owns canonical thread identity in principle, but some same-thread ordering and scanned-event classification logic still lives in `matrix/client.py`.

`stale_stream_cleanup.py` reaches into `matrix/client.py` internals and also reconstructs thread grouping and latest-thread-tail behavior itself.

That leaves thread policy spread across `thread_membership.py`, `matrix/client.py`, `stale_stream_cleanup.py`, and the conversation facade.

The result is a boundary that exists conceptually but still requires too many cross-file hops to understand.

## Desired Ownership Map

`src/mindroom/matrix/thread_membership.py` owns canonical thread identity and thread-aware ordering or grouping rules for scanned Matrix event sets.

`src/mindroom/matrix/thread_bookkeeping.py` owns mutation and bookkeeping impact decisions only.

`src/mindroom/matrix/conversation_cache.py` owns the conversation-facing seam that higher-level consumers call when they need thread or conversation services.

`src/mindroom/matrix/client.py` owns Matrix transport, visible-message reconstruction, and homeserver fetch logic, but not cleanup-specific thread policy helpers.

`src/mindroom/matrix/stale_stream_cleanup.py` owns restart cleanup flow only.

It should consume thread or conversation services instead of re-deriving thread policy.

## Design

Move the scanned-event ordering and grouping helpers that encode thread ancestry rules out of `matrix/client.py` and into `thread_membership.py`.

Keep those helpers close to `resolve_thread_ids_for_event_infos()` so one module owns both thread identity classification and the ordering semantics that depend on that classification.

Add one explicit cleanup-oriented conversation seam in `conversation_cache.py` for callers that need canonical thread grouping or latest-thread-tail derivation from scanned visible messages.

Refactor `stale_stream_cleanup.py` to call that seam instead of importing `_ordered_event_ids_from_scanned_event_sources`, `_sort_thread_history_root_first`, and direct membership resolution from multiple places.

Leave `thread_bookkeeping.py` unchanged except where type or helper reuse becomes clearer after the ownership shift.

## Non-Goals

Do not collapse `ThreadReadPolicy`, `ThreadWritePolicy`, `ConversationEventCache`, or `EventCacheWriteCoordinator` in this branch.

Do not redesign Tach exports in this branch.

Do not reopen broad `matrix.cache` package refactors.

Do not change external behavior unless needed to preserve existing thread invariants.

## Expected Diff Shape

`thread_membership.py` may grow because it is taking ownership of logic that is already thread policy.

`matrix/client.py` should shrink because cleanup-only thread-policy helpers leave that file.

`stale_stream_cleanup.py` should shrink because it stops being a second thread-policy engine.

`conversation_cache.py` may grow slightly if it needs one explicit helper seam to keep higher-level callers out of `matrix/client.py` internals.

Overall line count should stay roughly flat or go down once duplicate policy code is removed.

## Verification

Start from the existing clean baseline for:

`uv run pytest tests/test_threading_error.py tests/test_thread_history.py tests/test_stale_stream_cleanup.py -x -n 0 --no-cov -v`

Add focused regression coverage for the new seam and any moved ordering rule.

Re-run the same targeted suite after the refactor.

## Safe Next Boundary

If this branch succeeds, the next safe boundary to enforce is the thread and conversation slice where `stale_stream_cleanup.py` and adjacent callers depend on `thread_membership.py` and `conversation_cache.py` seams instead of `matrix/client.py` internals.
