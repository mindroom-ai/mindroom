# ARCH Validation

Test path: `tests/test_issue_176_validation.py`.
The test builds a real `_EventCache` on a temp SQLite database, seeds cached rows for `thread-A` and `thread-B` in one room, starts `MatrixConversationCache.get_thread_history()` for `thread-A`, sleeps for 200 ms inside `event_cache_threads.load_thread_events()` while the real `_EventCache` room lock is held, and measures wall-clock time for `MatrixConversationCache.get_thread_history()` on sibling `thread-B`.
The branch measurement on `issue-176-thread-barrier` at `edd43e0b0e` was `202.2 ms` median across three runs.
The comparison measurement with `src/mindroom/matrix/cache/` checked out from `origin/main` was `201.8 ms` median across three runs.
These numbers are effectively the same, so REV-C is right for this scenario: the per-thread coordinator does not deliver meaningful user-visible parallelism for sibling thread reads in the same room because the real `_EventCache` still re-serializes both reads at the per-room SQLite lock.
This validation used sleep injection inside the real locked section instead of queueing many operations, so it measures direct lock contention in the actual cache layer rather than aggregate delay from a synthetic backlog.
