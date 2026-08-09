# Bounded Hydration Installation Report

## RED evidence

- The exact RED command was `uv run pytest tests/test_event_journal_store.py::TestBoundedHydrationInstallation -n 0 --no-cov -q`.
- The first invocation exposed that the fresh worktree lacked the optional PostgreSQL driver, so `uv sync --all-extras` was completed and the identical command was rerun before any production edit.
- The valid RED run produced 10 failures across SQLite and PostgreSQL.
- Ordinary hydration and history recovery each exposed one transaction containing 257 projections plus the hydration marker, the injected second-write failure did not fire, cancellation left a published marker, and a membership fence ran only after the already-published install returned true.
- Self-review added the exact-debt RED command `uv run pytest tests/test_event_journal_store.py::TestRecoveryFinalizesOnlyItsExactDebt -n 0 --no-cov -q` before the debt-locking production edit.
- That RED run failed because the stale recovery returned `REPAID` and cleared a replacement obligation instead of returning `SUPERSEDED`.

## Design

- Hydration projection uses a production constant of 256 projected events per `Backend.write()` transaction.
- Every projection transaction materializes and self-assigns the room membership row, verifies the expected epoch, and projects only after that claim succeeds.
- Ordinary hydration commits its completeness and attempted-policy marker in one final marker-only transaction that claims the epoch again.
- Room-history recovery projects through the same bounded helper, then claims the membership row, locks and verifies the exact debt, publishes the room marker, and settles that debt in one final transaction.
- A crash, cancellation, busy error, or injected failure can leave idempotent projection rows, but it cannot publish new coverage or settle recovery before the final commit.
- A membership fence deletes chunks committed before it, and the next chunk or final marker refuses the stale epoch.
- No explicit `asyncio.sleep(0)` is necessary because every chunk awaits a real backend write, SQLite's FIFO writer queue serves admissions already queued behind the prior chunk before a later chunk, and each commit releases the lock for cross-process contenders.
- The one-million-message export window and two-million-fetched-event ceiling in `thread_export/projected_history.py` are unchanged.
- The design adds no staging or swap generation.

## Files changed

- `src/mindroom/event_journal/reads.py` extracts the reusable materialized membership claim and keeps marker publication monotonic within an epoch.
- `src/mindroom/event_journal/history_debt.py` adds a portable self-update that locks and returns the exact outstanding recovery obligation.
- `src/mindroom/event_journal/store.py` installs ordinary and recovery projections in bounded transactions and reserves publication and recovery settlement for the final transaction.
- `src/mindroom/event_journal/views.py` updates the hydration protocol documentation to match bounded installation.
- `tests/test_event_journal_store.py` adds real-backend transaction profiling and focused SQLite/PostgreSQL tests for ordinary hydration, empty hydration, recovery, failure, cancellation and retry, and membership fences.
- `.superpowers/sdd/pr-landing-blockers/export-brief.md` is the provided task requirement retained with the landing evidence.
- `.superpowers/sdd/pr-landing-blockers/export-report.md` records the implementation and verification evidence.

## Verification

- `uv sync --all-extras` completed successfully in the fresh worktree.
- `uv run pytest tests/test_event_journal_store.py::TestBoundedHydrationInstallation -n 0 --no-cov -q` passed 14 cases across SQLite and PostgreSQL.
- `uv run pytest tests/test_event_journal_store.py::TestRecoveryFinalizesOnlyItsExactDebt -n 0 --no-cov -q` passed its PostgreSQL two-store race after the locking edit.
- `uv run pytest tests/test_event_journal_store.py::TestBoundedHydrationInstallation tests/test_event_journal_store.py::TestRecoveryFinalizesOnlyItsExactDebt tests/test_event_journal_store.py::TestAFenceCannotBeSteppedOverByAConcurrentWalk tests/test_conversation_hydration.py tests/test_room_history_debt.py -n 0 --no-cov -q` passed after the final production edit.
- `uv run pytest tests/test_event_journal_store.py -n 0 --no-cov -q` passed 303 tests after the final production edit, with three pre-existing pytest marker warnings.
- `uv run pytest -n auto --no-cov -q` completed with exit code 0, with existing dependency and pytest marker warnings.
- `uv run ruff check src/mindroom/event_journal/history_debt.py src/mindroom/event_journal/reads.py src/mindroom/event_journal/store.py src/mindroom/event_journal/views.py tests/test_event_journal_store.py` passed.
- `uv run ruff format --check src/mindroom/event_journal/history_debt.py src/mindroom/event_journal/reads.py src/mindroom/event_journal/store.py src/mindroom/event_journal/views.py tests/test_event_journal_store.py` passed.
- `uv run ty check src/mindroom/event_journal/history_debt.py src/mindroom/event_journal/reads.py src/mindroom/event_journal/store.py src/mindroom/event_journal/views.py tests/test_event_journal_store.py` passed.
- `uv run pre-commit run --all-files` passed, including the repository Tach boundary hook.
- `git diff --check` passed.

## Production and test LOC

- Production changes contain 185 added lines and 103 removed lines across `history_debt.py`, `reads.py`, `store.py`, and `views.py`.
- Test changes contain 418 added lines and 3 removed lines in `test_event_journal_store.py`.

## Transaction bound

- The bound is structurally proven rather than inferred from wall-clock timing.
- A 257-event walk is observed as projection transactions of 256 and 1 events followed by a zero-projection publication transaction on both SQLite and PostgreSQL.
- Each nonempty projection transaction is asserted to contain at most 256 projected messages and exactly one materialized membership claim.
- The final ordinary transaction is asserted to contain one membership claim and one hydration marker with zero projected messages.
- The final recovery transaction is asserted to contain one membership claim, one hydration marker, one exact-debt settlement, and zero projected messages.
- A separate two-store PostgreSQL race proves the final transaction refuses publication and settlement when another process replaces the debt before its lock is acquired.
- These assertions mutation-kill a return to the previous one-write loop independently of the product export ceiling.

## Remaining concerns

- This change proves a fixed transaction-size ceiling but does not repeat the earlier 300,000-event or 2,000,000-event wall-clock benchmark.
- Nonblocking readers can observe partial projection rows by contract, while strict readers continue to require the final hydration marker.
