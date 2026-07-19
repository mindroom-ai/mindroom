# Matrix event-cache security and plaintext lifecycle

The Matrix event cache is a runtime-wide storage service that exposes a principal-bound view for each authenticated Matrix account.

Room membership is not used as the plaintext authorization boundary because two joined bots can have different encryption keys and decryption results.

The stable principal is the full Matrix user ID, not the device ID or the configured entity name.

SQLite stores the principal ID and room ID in every event, index, tombstone, state, reference, and plaintext key.

PostgreSQL derives an opaque SHA-256 namespace from the configured base namespace and full Matrix user ID, and every row remains room-scoped inside that principal-exclusive namespace.

The default constructor principal exists for standalone cache consumers and tests, while the orchestrator, approval transport, startup prewarm, and thread exporter use explicit principal views.

An event lookup is keyed by principal, room, and event ID.

A decrypted sidecar row is keyed by principal, room, and MXC URL, and reads additionally require a surviving reference from the requested event ID.

Reference rows are derived from version 2 `io.mindroom.long_text` metadata in top-level content and `m.new_content`.

Both unencrypted `url` and encrypted `file.url` MXC representations are tracked.

Plaintext persistence succeeds only while the owning event and its reference are visible and not tombstoned.

Decrypted plaintext exists only in the durable principal-owned cache; there is no runtime-wide process-local plaintext cache.

Hydration without complete principal, room, event, and MXC identity may return freshly downloaded content to the current call, but it cannot read or populate the durable cache.

Every durable plaintext hit revalidates the requesting event's surviving room-scoped MXC reference.

Redaction runs in the same database transaction as event, dependent-edit, thread-index, edit-index, and reference removal.

Candidate plaintext is deleted only when no surviving reference in the same principal and room remains.

Redaction tombstones prevent late event delivery or late hydration from recreating a removed event or plaintext row.

Thread replacement installs the authoritative snapshot's surviving references before pruning removed-event references, while invalidation uses the same orphan cleanup path.

An authoritative sync leave, a live own-user leave or ban, and a successful proactive leave purge only the departed principal's rows for that room.

Another principal that remains joined keeps its events, references, plaintext, tombstones, and freshness state.

Each principal-bound view is a non-owning handle, so closing one bot cannot close the runtime-wide cache service used by another bot.

If durable leave or ban cleanup fails, the principal-room purge remains pending in the backend runtime, blocks cache certification, and is flushed transactionally before any later read or write in that room.

The operation that commits a pending room or principal purge is discarded, so its queued callback cannot recreate deleted rows in the same transaction.

Each principal keeps a runtime departed-room fence after purge commit, and every backend read or write rechecks that fence while the backend operation is serialized until an authoritative rejoin finishes any pending cleanup.

A proactive multi-room leave fences and durably purges each room immediately after its leave succeeds and before processing the next room.

Each leave request and its confirmed cleanup run as one shielded operation, so caller cancellation waits for the final leave outcome and purge before propagating.

Raising a room fence also records the durable purge synchronously, so cancellation before the queued purge coroutine starts cannot let a later rejoin expose pre-leave rows.

Reads recheck the fence after the backend callback and PostgreSQL transaction completes, so a result obtained before a leave cannot be returned after that leave is observed.

Each room fence has a monotonic runtime epoch, and queued rejoin work may clear the fence only when no newer departure changed that epoch.
The room-state row also stores a durable joined/departed state and transition epoch that every backend operation checks, with writes checking inside their transaction.
Thread snapshot and point-lookup refills certify the durable epoch before homeserver I/O, and replacement plus fetch-derived event, sidecar-ownership, and plaintext writes require the same still-joined transition.
Cached and stale thread reads also certify before loading rows and carry that epoch through sidecar hydration, so a held pre-purge snapshot cannot recreate ownership or plaintext after purge or rejoin.
If storage cannot certify an epoch, the authoritative homeserver read continues but uses an impossible epoch that suppresses fetch-derived writes and durable sidecar plaintext reuse.
Departure atomically purges room content and advances the durable state to departed, while an authoritative rejoin advances it to joined only after pending cleanup commits.
An authoritative rejoin also recovers a durable departed row after process restart.
If it observes a newer local departure inside the rejoin transaction, it purges the room and restores durable departed state before commit.
Generic thread invalidation preserves the room-state row, so it cannot erase the cross-process membership fence.

Per-turn event and thread memoization includes the runtime departure epoch in every key, so active turns cannot replay pre-leave cached content.

Principal-scoped safety disables affect only that bot's SQLite or PostgreSQL view, while root-owned shared-service disables still stop every current and future principal.

Every authoritative leave invalidates both the in-memory and saved checkpoint before durable cleanup starts.

If saved-checkpoint deletion fails, the runtime disables cache reads and writes, leaves durable rows consistent with the older checkpoint, and poisons further certification so restart can replay the leave.

Sync-response leave cleanup commits before unrelated call reconciliation can suspend or fail.

Thread lookup indexes are rebuilt on event replacement, while root self-mappings survive only when a current batch or a surviving child still proves them.

If the process stops before cleanup commits, the next startup has no certified checkpoint and transactionally purges every content row for that principal before restoring sync continuity or allowing cache reads.
Cold-start principal cleanup preserves certified room-state rows and advances their epochs, so another process cannot finish a refill certified before the cleanup.

That cold-start principal purge preserves rows owned by every other principal.

If cold-start cleanup is unavailable or fails, only that principal view is disabled for the rest of the runtime, no later sync checkpoint can certify the missing cache writes, and the next process retries cleanup before using cache continuity.

SQLite write operations begin with `BEGIN IMMEDIATE`, so tombstone and MXC-ownership authorization reads cannot race a second connection's redaction commit.

SQLite content writes establish a durable room-membership row, and reads use `BEGIN IMMEDIATE` so they are ordered strictly before or after departure, principal cleanup, redaction, and other content mutations committed through another connection.

SQLite write results are reauthorized after commit while the operation lock is still held, so a concurrent leave cannot expose plaintext written before the fence.

SQLite schema version 12 resets predecessor and older advisory cache contents inside one rollback-safe transaction because those rows have no provable principal owner, and it creates a new durable database-generation identifier.
Each SQLite principal view derives a stable checkpoint generation from that database generation and the full Matrix principal ID, so a retained agent token cannot cross an account or homeserver rebind.

PostgreSQL schema version 3 migrates under a global transaction-scoped advisory lock, preserves scoped rows from every namespace, expands event and plaintext keys with room scope, adds durable membership generations, and deletes legacy plaintext whose room and event ownership cannot be proven.

Every PostgreSQL operation holds the same exclusive transaction-scoped advisory lock for its principal namespace.

Different principals use different namespaces and locks, while all operations for one principal serialize with principal purge and leave cleanup across processes.

Each PostgreSQL principal namespace stores a durable random cache-generation identifier that changes when that namespace metadata is recreated.

Certified sync-token records must use version 2 and include the cache generation, so a legacy plaintext token, old schema, principal change, or reset cache starts cold instead of skipping the history required to rebuild ownership rows.
