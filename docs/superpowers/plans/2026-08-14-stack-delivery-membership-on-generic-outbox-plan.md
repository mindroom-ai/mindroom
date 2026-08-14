# Stack delivery membership fencing on the generic outbox

**Goal:** Rebuild PR #1836 on PR #1837 so response and approval deliveries share one durable Matrix outbox and one membership-fencing protocol, while preserving every useful regression test from #1836.

**Architecture:** PR #1837's `matrix_delivery_outbox` and `MatrixDeliveryWorker` remain the sole delivery owners.
The restack adds frozen membership ownership, durable retirement, stable Matrix delivery markers, stale-event projection filtering, and exact migration policy to those generic seams.
Response- and approval-specific code supplies domain facts and consumes generic delivery outcomes; neither owns a parallel send/recovery protocol.

**Constraints:** Preserve the untracked `REVIEW.md` untouched.
Do not merge either PR.
Force-push only the rebuilt #1836 branch after complete verification, then retarget #1836 to the #1837 branch.

## Task 1: Freeze both histories and establish the test inventory

1. Record the exact #1836 head and create `backup/pr1836-before-1837-stack`.
2. Fetch the current GitHub head of #1837, verify it matches GitHub metadata, and create `backup/pr1837-stack-base`.
3. Inventory every production and test change in old #1836 against its original base.
4. Classify tests as generic invariants to port, adapter behavior to rename, or obsolete response-outbox implementation tests to replace with named generic equivalents.
5. Reset the working branch to the frozen #1837 head and cherry-pick these design and plan documents.
6. Run the focused #1837 baseline suites before implementation.

## Task 2: Put membership ownership in the generic outbox

1. Port store tests first for frozen membership epochs, stale acknowledgement without projection, retired identity tombstones, source-less ownership, lock ordering, and departure/rejoin behavior.
2. Extend the generic delivery model and schema with the minimum durable fields.
3. Implement enqueue, claim, acknowledgement, retirement, and event-ownership rules in the generic journal modules.
4. Keep membership-row then outbox-row locking consistent across admission, acknowledgement, retirement, and refetch.
5. Remove response-specific persistence concepts rather than retaining compatibility wrappers.

## Task 3: Make the generic worker own delivery identity and recovery

1. Port gateway and history-scan regressions first, including INITIAL/FINAL distinction, changed-device recovery, exact marker matching, sender and room scoping, scan termination, edits, compaction ambiguity, and retirement races.
2. Add the stable `io.mindroom.delivery_id` marker to the generic delivery envelope.
3. Move reconciliation, existing-event adoption, send, acknowledgement, and retirement behavior into `MatrixDeliveryWorker`.
4. Make response delivery and approval delivery thin callers of the same worker.
5. Preserve neutral reconciliation terminology rather than device-only wording.

## Task 4: Fence every projection installation path

1. Port admission, hydration, room-history recovery, point-refetch, and dropped-revision regressions first.
2. Validate marker ownership against principal, room, sender, turn, stage, and frozen membership epoch.
3. Ensure stale events are never installed into the current visible projection.
4. Reconcile already-projected events transactionally when delivery retirement wins a race.
5. Keep interactive prompt activation derived from the fenced visible projection.

## Task 5: Compose the migration inside #1837's migration boundary

1. Add both-backend migration tests first using the exact predecessor schemas.
2. Extend `event_journal/migrations.py`; do not create another migration framework.
3. Backfill approval deliveries from exact approval-card facts.
4. Backfill response deliveries only from exact same-room admitted-source facts.
5. Delete never-attempted unverifiable rows and reject attempted unacknowledged rows whose membership cannot be proven.
6. Preserve acknowledged legacy event IDs as stale-event tombstones where required.
7. Verify the final schema enforces the non-null steady-state invariant on SQLite and PostgreSQL.

## Task 6: Complete the test-port audit and simplify the combined design

1. Compare the final test tree with the inventory from Task 1.
2. Account explicitly for every removed #1836 test with a named generic replacement or a documented obsolete seam.
3. Search for `response_outbox`, `ResponseDelivery`, approval-specific recovery loops, old marker helpers, and stale migration names.
4. Remove duplicate branches, wrappers, and tests made obsolete by the generic boundary.
5. Run the full affected suites on SQLite and PostgreSQL.

## Task 7: Verify and publish the stacked PR

1. Run Ruff format/check, `git diff --check`, Tach dependency/interface checks, and affected pre-commit hooks.
2. Run the final focused and broad regression suites and record exact results.
3. Review the final diff against #1837 for correctness, simplification, and line-count regressions.
4. Force-push with lease to `fix/fence-delayed-delivery-projection`.
5. Change PR #1836's base to `refactor/unify-approval-delivery-outbox`.
6. Verify GitHub reports #1836 open with the intended head/base and no merge action taken.
