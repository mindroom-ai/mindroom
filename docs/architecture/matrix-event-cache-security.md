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

Process-local plaintext entries include principal, room, event, and MXC identity, and durable-cache use revalidates ownership before every hit.

Hydration without that complete identity may return freshly downloaded content to the current call, but it cannot read or populate the process cache.

Redaction runs in the same database transaction as event, dependent-edit, thread-index, edit-index, and reference removal.

Candidate plaintext is deleted only when no surviving reference in the same principal and room remains.

Redaction tombstones prevent late event delivery or late hydration from recreating a removed event or plaintext row.

Thread replacement and invalidation use the same reference cleanup path, so non-redaction event deletion cannot orphan decrypted plaintext.

An authoritative sync leave, a live own-user leave or ban, and a successful proactive leave purge only the departed principal's rows for that room.

Another principal that remains joined keeps its events, references, plaintext, tombstones, and freshness state.

Each principal-bound view is a non-owning handle, so closing one bot cannot close the runtime-wide cache service used by another bot.

If durable leave or ban cleanup fails, the principal-room purge remains pending in the backend runtime, blocks cache certification, and is flushed transactionally before any later read or write in that room.

The operation that commits a pending room or principal purge is discarded, so its queued callback cannot recreate deleted rows in the same transaction.

Each principal keeps a runtime departed-room fence after purge commit, and every backend read or write rechecks that fence under the room lock until an authoritative rejoin finishes any pending cleanup.

Principal-scoped safety disables affect only that bot's SQLite or PostgreSQL view, while root-owned shared-service disables still stop every current and future principal.

Every authoritative leave invalidates both the in-memory and saved checkpoint before durable cleanup starts.

If saved-checkpoint deletion fails, the runtime disables cache reads and writes, leaves durable rows consistent with the older checkpoint, and poisons further certification so restart can replay the leave.

Sync-response leave cleanup commits before unrelated call reconciliation can suspend or fail.

Thread lookup indexes are rebuilt on event replacement, while root self-mappings survive only when a current batch or a surviving child still proves them.

If the process stops before cleanup commits, the next startup has no certified checkpoint and transactionally purges every row for that principal before restoring sync continuity or allowing cache reads.

That cold-start principal purge preserves rows owned by every other principal, and a failed attempt keeps the principal generation unavailable until a later operation commits the purge.

Process-local plaintext for the departed principal and room is removed immediately even when the durable backend is unavailable.

SQLite schema version 11 resets older advisory cache contents inside one rollback-safe transaction and creates a durable database-generation identifier.
Each SQLite principal view derives a stable checkpoint generation from that database generation and the full Matrix principal ID, so a retained agent token cannot cross an account or homeserver rebind.

PostgreSQL schema version 2 migrates under a global transaction-scoped advisory lock, preserves every namespace, expands event and plaintext keys with room scope, and quarantines legacy unscoped plaintext under an unreachable empty room ID.

Each PostgreSQL principal namespace stores a durable random cache-generation identifier that changes when that namespace metadata is recreated.

Certified sync-token records use version 2 and include the cache generation, so an old schema or a reset cache cannot skip the history required to rebuild ownership rows.
