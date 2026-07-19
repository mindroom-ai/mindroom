# Matrix event-cache storage and maintenance

The SQLite cache schema is version 11 and the PostgreSQL cache schema is version 2.

## Source of truth

The `events` table is the only full-JSON source for point lookups, edit projection, and thread snapshots.

The normalized `thread_events` rows store only membership, timestamp, and stable write order.

SQLite assigns events and thread memberships from a persisted monotonic write sequence so equal-timestamp ordering survives replacement and restart.

PostgreSQL retains a nullable legacy `event_json` column because the shared physical version-1 table must be migrated without rewriting another namespace.

Current PostgreSQL writes set that legacy column to `NULL`, and current reads join membership rows to `events`.

Thread snapshot replacement, invalidation, redaction, edit projection, and point lookup continue to update or read the same active event rows transactionally.

## Startup maintenance

Startup maintenance runs inside the same transaction as schema migration.

It removes edit-index rows whose edit payload is absent.

It removes event-to-thread rows whose event payload is absent while retaining learned root self-mappings proven by a surviving child event or normalized thread membership.

The startup log includes backend bytes where available, normalized legacy thread-payload rows, row counts for relevant tables, tombstone counts, stale marker counts, remaining orphan counts, and repair outcomes.

Runtime diagnostics expose the immutable startup maintenance report plus basic live backend, reconnection, and pending-invalidation state.

SQLite omits byte size when startup filesystem metadata is unavailable.

Diagnostics contain counts and sizes only and never contain event content or connection URLs.

## Migration and sync certification

SQLite version 10 is migrated to version 11 by transactionally rebuilding `thread_events` as normalized membership rows joined to active `events`.

A version-10 membership without an active source marks its thread stale instead of copying duplicated legacy JSON into the new source of truth.

Every SQLite database and PostgreSQL namespace persists an opaque certification generation, and every certified sync checkpoint records the generation it covers.

Unsupported SQLite shapes still use a destructive reset, and that reset transactionally creates a new generation before it commits.

A token from the prior generation is rejected on every later process even if the resetting process crashes before any bot can clear its token file.

Only version-2 generation-bound token records are accepted, so older token formats start cold once.

PostgreSQL migration takes a transaction-scoped global advisory lock, makes the legacy payload column nullable, normalizes only the initializing namespace, repairs only that namespace, and commits the schema version and maintenance result together.

PostgreSQL version-2 binary cutover requires an exclusive database-wide maintenance window because the schema version is global to the physical database.

Stop and drain every version-1 runtime sharing that database before starting the first version-2 runtime, and do not restart a version-1 runtime after migration.

Row normalization remains namespace-scoped after the binary cutover.

Other PostgreSQL namespaces retain their legacy payload until their own version-2 runtime initializes.

Cancellation or failure rolls back SQLite and PostgreSQL DDL, payload normalization, repair, metadata, and stale markers as one unit.

Migration tests use real version-10 and version-1 shapes on disposable storage and never access a production database.

SQLite version-10 migration adds and populates event write order in place instead of rebuilding the JSON-bearing `events` table.

The write-order update still rewrites event pages into the WAL, so operators must budget temporary free space at least comparable to the active events table plus safety margin and must take an offline backup before upgrade.

Dropping the legacy JSON-bearing `thread_events` table adds free pages to the database but does not shrink the physical file automatically.

After a successful upgrade and backup verification, an operator who needs immediate filesystem reclamation can stop MindRoom, run SQLite `VACUUM INTO` to a new file on storage with enough free space, verify the new database, atomically replace the old database while it is offline, and then restart.

## Production-copy measurements

A consistent SQLite backup copied from the production host was measured only on disposable local copies.

The source backup was 3,171,799,040 bytes with 137,931 events and 123,923 thread memberships.

The legacy `thread_events` table occupied 1,412,108,288 bytes because it duplicated full event JSON.

The pruned version-10 migration completed in 26.81 seconds, passed `PRAGMA quick_check`, and reduced the normalized `thread_events` table to 18,178,048 bytes.

SQLite exposed 1,424,539,648 bytes as reusable free pages after migration, but the physical file did not shrink automatically.

The production copy contained 69 orphan edit-index rows and 69 unsupported event-to-thread rows, and startup repair removed both sets while preserving valid learned roots.

The same migrated copy completed a later steady-state startup audit in 0.91 seconds.

## Retention blocker

This change deliberately does not add general age-based retention or streaming-edit deletion.

The previously implemented cold archive and compaction were removed after production-copy queries failed to return the first 500 candidates in more than 102 seconds, while the busiest-room path also ran for more than 90 seconds.

Those scans would have run during startup or while holding cache write locks, so their operational cost was not acceptable.

A partition-scoped direct delete was faster in read-only experiments, but it would intentionally turn valid point-query cache hits into misses and remove events from persisted thread snapshots.

That violates the retention requirement that point queries and certified snapshots must not be weakened, so the direct-delete proposal was rejected rather than shipped on performance evidence alone.

The cache currently has no certified lower-bound contract proving that an old active event is irrelevant to point queries, approval and scheduled-task consumers, a current thread snapshot, edit projection, saved sync certification, or a future redaction.

Redaction tombstones also have no safe expiry bound because an event replay after tombstone deletion could resurrect content.

A safe follow-up design must first persist a per-room certified sync lower bound, enumerate durable point-query consumers and their leases, prove that every retained thread snapshot and latest-edit projection is closed over the deletion set, and define a homeserver-backed anti-resurrection bound for tombstones.

Only rows older than every applicable bound and absent from every protected closure could then enter a bounded delete batch.
