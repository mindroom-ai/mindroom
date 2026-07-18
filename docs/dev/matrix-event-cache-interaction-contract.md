# Matrix Event Cache Interaction Contract

This document defines the durable Matrix event-cache boundary and the reproducible evidence required to change it.

## Contract boundary

The cache retains conversation history observed in joined-room timeline events.

The point cache is intentionally broader than visible conversation history.

Every admitted joined-room timeline event with an event ID can be retained for point lookup, relation resolution, recent-event lookup, and redaction bookkeeping.

Visible thread history projects supported `m.room.message` events, collapses edits into their originals, and omits non-message events and still-opaque encrypted payloads.

Durable conversation history is distinct from ephemeral sync state.

Membership loss is a separate lifecycle boundary and does not currently purge previously retained joined-room history.

## Admitted joined-room timeline families

The following treatment is covered against both SQLite and PostgreSQL backends by `tests/test_matrix_cache_interaction_contract.py`.

| Interaction family | Point cache | Visible thread history | Indexes and invalidation |
| --- | --- | --- | --- |
| Text, notice, emote, and location messages | Retained | Visible when they belong to the thread | Thread and reply relations map to the root and invalidate only that thread |
| Valid file, image, audio, voice, and video messages | Retained | Visible when they belong to the thread | Thread and reply relations map to the root and invalidate only that thread |
| Explicit thread children and relation-less replies | Retained | Visible | Event-to-thread mappings are retained and the known thread is invalidated |
| Root, child, and reply edits | Retained | Applied to the original rather than shown separately | Edit and event-to-thread indexes are retained and the known thread is invalidated |
| Message references | Retained | Visible | Message references resolve through the relation target and affect its known thread |
| Reactions | Retained until redacted | Not visible | Reactions do not alter thread snapshots |
| Sticker, poll start, poll response, poll end, beacon info, and beacon | Retained | Not visible | These families remain room-level and do not invalidate thread snapshots |
| Generic state and timeline events | Retained | Not visible | These families remain room-level |
| Member, name, topic, avatar, power, join-rule, history-visibility, guest-access, alias, encryption, and pin state | Retained when delivered in the joined timeline | Not visible | These families remain room-level |
| Call invite, candidates, answer, select-answer, reject, negotiate, and hangup | Retained | Not visible | These families remain room-level |
| RTC membership, focus, and notification events | Retained | Not visible | These families remain room-level |
| Encrypted relation-bearing events | Retained as opaque events | Not visible until decryption supplies message content | Thread, reply, edit, and message-reference relations are indexed and invalidate the known thread |

Relation names such as `m.reference` and `m.thread` are reused by non-message families.

Only `m.room.message` and `m.room.encrypted` relations can enter event-to-thread bookkeeping.

Poll responses, poll ends, beacons, stickers, state, calls, and RTC events remain room-level even when they carry a relation-shaped payload.

## Redactions

Redaction envelope events are intentionally omitted from the point cache.

A redacted target is durably tombstoned so a later sync replay cannot resurrect it.

Redacting a visible message removes the message and its event-to-thread mapping.

Redacting an original also removes dependent edits and their edit and thread indexes.

Redacting only an edit removes that edit and restores the next applicable visible state of the original.

Redacting a reaction removes only the reaction and leaves the visible thread snapshot unchanged.

Redacting a poll response, poll end, beacon, or any other non-message event removes only that point event and leaves every thread snapshot unchanged.

## Deliberately excluded sync categories

Complete state under a joined-room sync response is excluded because it is a current-state snapshot rather than conversation history.

Invite-room and leave-room timelines are excluded because they are outside joined conversation history.

Typing and receipt events are excluded because they are ephemeral.

Presence is excluded because it is ephemeral and global.

Global and room account data are excluded because they are per-account state.

To-device events and device-list changes are excluded because they belong to encryption and device lifecycle rather than conversation history.

The exclusion tests prove that these categories cannot create point rows, thread rows, edit rows, or invalidation markers.

The exclusion tests also prove that a leave sync does not silently purge previously retained history.

## Thread snapshot reads

A durable thread snapshot is usable only when its state row exists, `validated_at` is set, and no thread or room invalidation is at least as new as that validation.

A snapshot without its thread root is rejected.

A rejected or absent snapshot causes an authoritative homeserver room-history scan and guarded cache refill.

A second unchanged read is served from cache and performs no homeserver scan.

The advisory read path may use a labelled stale-cache fallback when a required refill fails.

Dispatch reads reject stale fallback and propagate the refill failure.

Every completed read emits `matrix_cache_thread_history_refreshed` with `mode`, `cache_read_ms`, `homeserver_fetch_ms`, page and event counts, `cache_reject_reason`, `thread_read_source`, degradation state, and error state.

## Disposable live audit

`tests/manual/matrix_event_cache_live_audit.py` creates a new private room owned by the test agent and never accepts access tokens as command-line arguments.

The harness uses UUID transaction IDs for every idempotent write.

The harness generates and decodes a real PNG, WAV, and WebM fixture before upload, downloads each MXC through the authenticated client media API, and verifies its SHA-256 digest before sending media events.

The harness emits the interaction matrix, client-controllable ephemeral categories, redaction cases, and opaque encrypted relations through authenticated Matrix client APIs.

The harness can invite and explicitly join a second test agent when its token is supplied through a second environment variable.

The optional service-cache inspection opens SQLite with `mode=ro`, enables `PRAGMA query_only`, and records only IDs, counts, integrity state, hashes, and timings.

Strict thread reads use a separate new disposable SQLite database and refuse an existing path or the service-cache path.

The first strict read refills that isolated cache from the authenticated homeserver, the second proves a zero-fetch cache hit, and a redaction applied through the cache API proves rejection and refill without writing the live service database.

Every declared interaction expectation is compared with homeserver accounting, the read-only service snapshot, and the initial visible thread projection before evidence is written.

The evidence writer rejects secret-shaped keys and either access-token value before writing JSON.

Use this local form after loading credentials from a secret store into the environment.

```bash
uv run python tests/manual/matrix_event_cache_live_audit.py \
  --homeserver "$MATRIX_HOMESERVER" \
  --evidence /tmp/matrix-event-cache-audit.json \
  --cache-db "$MINDROOM_STORAGE_PATH/event_cache.db" \
  --strict-read-cache-db "/tmp/matrix-event-cache-strict-$(uuidgen).db" \
  --strict-thread-reads
```

Use `--invite-user-id`, `--invite-access-token-env`, and `--trigger-user-id` together to exercise a real running agent's thread-history read path.

For hosted evidence, reread the remote `AGENTS.md` files, run only against the `mindroom-chat` instance, keep both tokens inside the remote shell, and use a new disposable private room.

Do not point hosted evidence at local port 8008.

Do not print tokens, edit the live database, deploy, restart, or use a pre-existing room.

The sanitized harness JSON contains the exact fetch source, timings, scanned pages, scanned event counts, visible event IDs, cache rejection reason, and executable expectation-validation count for all three isolated strict reads.

Hosted evidence describes the deployed service as a production baseline.

PR-specific classification remains proven by the SQLite and PostgreSQL owning-seam tests unless the PR itself has been deployed through a separately authorized release.

## Known non-owned lifecycle and encryption gaps

Membership-loss cleanup is not owned by this contract track.

A deterministic reproduction is to cache a joined-room event, deliver the same room under `rooms.leave`, and observe that the point row remains while no new leave-timeline event is admitted.

Encrypted revalidation policy is not owned by this contract track.

A deterministic reproduction is to seed a validated thread snapshot, ingest an opaque `m.room.encrypted` child with a clear `m.thread` relation, and observe that the relation can append and revalidate while visible projection still omits the undecryptable child.

Those gaps require coordinated lifecycle or E2EE policy changes rather than an overlapping cache-contract workaround.

Thread snapshot replacement currently owns point-row deletion through the duplicated storage layout, which belongs to the storage normalization track.

A deterministic reproduction is to cache a thread child plus a relation-less reply, edit, and message reference, refill the snapshot, redact the child, and refill again.

The authoritative refill removes the now-unresolvable reply, reply edit, and reference from point storage even though the homeserver timeline still retains them.

This storage-owned reproduction is documented for coordination and is not inferred from any helper process writing the live service cache.

## Durable evidence

The checked-in live evidence records the disposable room and thread identifiers, fixture hashes, cache accounting, and three consecutive read outcomes: refill, verified cache hit, and rejection followed by refill.

The evidence must contain no credentials or message bodies.
