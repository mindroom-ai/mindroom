# Matrix event-cache storage and maintenance

The SQLite cache schema is version 11 and the PostgreSQL cache schema is version 2.

## Source of truth

The active `events` table is the only full-JSON source for normal point lookups, edit projection, and thread snapshots.

The normalized `thread_events` rows store only membership, timestamp, and stable write order.

SQLite assigns active events and thread memberships from a persisted monotonic write sequence, so deleting an active row cannot let a later equal-timestamp row reuse its cold-history order.

PostgreSQL retains a nullable legacy `event_json` column so the shared physical version-1 table can be migrated without rewriting another namespace, but current writes and reads never use that column.

Superseded nonterminal MindRoom streaming edits move from the active tables into `compacted_streaming_edits` as one zlib-compressed JSON payload plus the minimal ordering, edit, and thread projections needed to preserve behavior.

The cold archive is part of the event source of truth because Matrix redaction of a newer terminal edit can make an earlier nonterminal edit visible again.

Point lookup, recent-room lookup, latest-edit selection, thread lookup, thread ordering, snapshot replacement, invalidation, redaction, and late-replay filtering therefore operate across active and cold storage.

Compaction never creates a redaction tombstone because compaction is not deletion from Matrix history.

Compaction requires a strictly newer terminal edit from the same room, original event, and sender, which avoids ambiguous equal-timestamp replacement races and cross-sender replacement.

Compaction selects, compresses, bulk-writes, and removes bounded batches inside one caller-owned transaction, so startup memory is bounded and cancellation rolls every batch back together.

PostgreSQL startup compaction acquires the same transaction-scoped room advisory lock as older runtimes and reselects candidates under that lock before archiving them.

## Startup maintenance

Startup maintenance runs inside the same transaction as schema migration.

It audits and repairs edit-index rows whose edit event is absent.

It audits and repairs event-to-thread rows whose event is absent while retaining root self-mappings that are proven by a surviving active child mapping, normalized thread membership, or cold child mapping.

It marks a thread stale before removing a membership row whose active source event is absent.

It compacts eligible streaming edits again so replayed or partially processed writes converge after restart.

The startup log includes backend bytes where available, namespace payload bytes for PostgreSQL, normalized legacy thread-payload rows, row counts for active tables and cold history, tombstone counts, stale marker counts, streaming categories, orphan counts before and after repair, and repair and compaction outcomes.

Runtime diagnostics preserve the immutable startup outcomes and overlay an exact, bounded-staleness storage snapshot with its age, dirty state, refresh state, and refresh-failure count.
SQLite diagnostics explicitly report when filesystem metadata makes byte size unavailable.

Committed mutations coalesce into a throttled background recount, and operators can force an exact recount through the cache diagnostics API.

Telemetry refresh failure preserves the last good snapshot and never rolls back a cache write, disables the cache, or weakens sync certification.

Diagnostics contain counts and sizes only and never contain event content or connection URLs.

## Migration and sync certification

SQLite version 10 is migrated to version 11 by transactionally rebuilding `thread_events` as normalized membership rows joined to active `events`.

A version-10 membership without an active source marks its thread stale instead of copying the duplicated legacy JSON into the new source of truth.

Every SQLite database and PostgreSQL namespace persists an opaque certification generation, and every certified sync checkpoint records the generation it covers.

Unsupported SQLite shapes still use a destructive reset, and that reset transactionally creates a new generation before it commits.

A token from the prior generation is rejected on every later process even if the resetting process crashes before any bot can clear its token file.

Unbound legacy token records also start cold because they cannot prove which durable cache generation they certify.

PostgreSQL migration takes a transaction-scoped global advisory lock, changes the legacy payload column to nullable, creates cold storage, normalizes only the initializing namespace, repairs only that namespace, and commits the schema version and maintenance result together.

PostgreSQL version-2 binary cutover requires an exclusive database-wide maintenance window because the schema version is global to the physical database.
Stop and drain every version-1 runtime sharing that database before starting the first version-2 runtime, and do not restart a version-1 runtime after migration.
Version-1 thread readers require the duplicated payload that version 2 intentionally removes, and version-1 thread mutations cannot preserve version-2 cold-storage invariants.

Row normalization remains namespace-scoped after the binary cutover.
Other PostgreSQL namespaces retain their legacy payload until their own version-2 runtime initializes, and each startup reports how many legacy payload rows it normalized, so one namespace never deletes another namespace's only pre-migration copy.

Cancellation or failure rolls back SQLite and PostgreSQL DDL, payload normalization, repair, compaction, metadata, and stale markers as one unit.

Cancellation of a background or forced metrics recount also rolls back its read transaction before the shared connection can serve another cache operation.

Migration tests use real version-10 and version-1 shapes on disposable storage and never access a production database.

SQLite version-10 migration adds and populates event write order in place instead of rebuilding the 1.31-GiB JSON-bearing `events` table observed by the audit.
The write-order update still rewrites event pages into the WAL, so operators must budget temporary free space at least comparable to the active events table plus safety margin and must take an offline backup before upgrade.
Dropping the legacy JSON-bearing `thread_events` table adds free pages to the database but does not shrink the physical file automatically.
After a successful upgrade and backup verification, an operator who needs immediate filesystem reclamation can stop MindRoom, run SQLite `VACUUM INTO` to a new file on storage with enough free space, verify the new database, atomically replace the old database while it is offline, and then restart.

## Retention blocker

This change deliberately does not add general age-based retention.

The cache currently has no certified lower-bound contract proving that an old active event is irrelevant to point queries, approval and scheduled-task consumers, a current thread snapshot, edit projection, saved sync certification, or a future redaction.

Redaction tombstones also have no safe expiry bound because an event replay after tombstone deletion could resurrect content.

The compressed streaming archive reduces duplicate and superseded payload cost but remains durable cold history rather than an age-retention policy.

A safe follow-up design must first persist a per-room certified sync lower bound, enumerate durable point-query consumers and their leases, prove that every retained thread snapshot and latest-edit projection is closed over the deletion set, and define a homeserver-backed anti-resurrection bound for tombstones.

Only rows older than every applicable bound and absent from every protected closure could then enter a bounded delete batch.

That future deletion must mark affected snapshots stale before removing membership, preserve the latest visible replacement per sender, retain all redaction fallbacks needed above the bound, record batch outcomes, and advance its watermark transactionally.
