# Matrix Delivery Finalization Design

## Context

Restart recovery must repair and resume bot-owned responses in rooms that may be outside the current Sliding Sync window.
The existing hydration proof records encryption and joined membership before recovery work, but proof-bound delivery can perform additional awaited work before sending.
The current boundary does not retire an existing Megolm session when an authoritative joined-members refresh changes membership.
The current boundary also prepares large messages before rejecting a stale plaintext proof, so preparation can leave an unencrypted sidecar upload behind after the room becomes encrypted.
Nio's room-send path can independently refresh members, ignore a failed key-query response, and continue to encryption while application hydration is still failing.
Nio also stores invitees in `MatrixRoom.users`, which is the recipient collection used for Megolm session sharing.

## Goals

Proof-bound delivery must reject stale encryption state before any content upload.
Encrypted delivery must refresh authoritative joined membership at the final send boundary.
Membership changes must retire every existing outbound Megolm session before any later hydration await or nio encryption.
Device-key readiness must be re-established for the final authoritative joined set.
Recovery retries and stable Matrix transaction IDs must retain their current behavior.
The fix must remain owned by the Matrix delivery boundary instead of adding recovery-specific crypto logic to the coordinator.
Every application room send must use the same per-client, per-room delivery lock.
The runtime Matrix client must reject stale readiness and invited recipients at nio's final send boundary.
Every awaited membership, device-key, and encryption-state response must be ordered against newer local sync state.
One logical message delivery must retain one transaction ID across retries that follow an ambiguous transport result.
Payload uploads must retain the exact encryption mode established before preparation.

## Non-Goals

This change will not create a recovery-only Matrix client.
This change will not force every recovery room into the Sliding Sync window.
This change will not replace nio's encryption implementation or promise transactional ordering with concurrent homeserver membership writes.
This change will not redesign the restart-recovery coordinator.

## Delivery Invariants

A plaintext hydration proof is usable only while an authoritative encryption-state query still reports no encryption event.
An encrypted hydration proof is usable only while nio has an encrypted, member-synchronized room with complete device-key readiness.
Large-message preparation must not begin until the proof passes a preflight validation.
Encrypted finalization must fetch joined members again after content preparation and before the application calls nio's send operation.
The cached `MatrixRoom.users` set must exactly match the authoritative joined-member response and contain no invited users.
An unknown or invite-polluted prior recipient set and any joined-member change must retire the outbound Megolm session before full-state or device-key awaits.
Device-key work must keep a cached room marked membership-unsynchronized until every joined member's pending key query is clear.
No second application room send may overlap hydration or final delivery for the same client room.
Nio must fail closed instead of performing its own member or key refresh when encrypted readiness is stale at its send boundary.
The runtime client must remove invitees after joined-member and sync processing so Megolm session sharing sees joined recipients only.
Every runtime joined-members request must reject older responses before they mutate the cache, while delivery-owned generations remain valid through hidden-room full-state publication.
Success-typed joined-members responses attached to non-successful HTTP transports must be rejected before cache mutation.
Global key-query sequences and device-list generations must reject responses that finish after a newer query or invalidation without rejecting the query's own session retirement.
Recipient and membership generations must invalidate a prepared transport when its exact room state changes.
Nio's monotonic encrypted-room record must dominate a later negative state-event response.
Full room state and joined-members results must agree on the joined-only roster before hidden-room publication.
An unchanged authoritative joined roster must preserve its existing outbound session.
Proof rejection must return the existing retry signal without sending an event.

## Architecture

`mindroom.matrix.client_delivery` remains the single owner of delivery hydration, proof validation, large-message preparation, and application send sequencing.
One weakly client-owned map supplies an independent `asyncio.Lock` for each room, and both public hydration and every application event-send helper acquire that lock.
`RoomDeliveryHydrationProof` continues to describe the encryption mode and authoritative joined-member snapshot established by hydration.
Encrypted hydration will snapshot the cached encryption recipient roster before calling `joined_members`, retire the outbound session immediately when that roster is unknown, invite-polluted, or changed, and then replace it with exact authoritative joined membership.
The same hydration operation will refresh tracked users, query required device keys, and publish a complete hidden-room candidate only after validation succeeds.
Hidden-room candidates retain full non-membership state but intentionally omit invite membership from nio's encryption roster.
Weakly client-owned membership and device-list generations order hydration network responses against concurrent Classic Sync and Sliding Sync invalidations.
Sync processing preapplies membership events, encryption events, joined-count mismatches, and device-list changes before recovery dispatch or callbacks can await.
Each runtime joined-members request records whether its own response advanced the membership generation and refuses any response superseded by a later-started request before it can overwrite the cache.
The accepted joined-members generation remains bound across a hidden-room full-state await so an uncached candidate cannot publish a superseded roster.
Every runtime key query, including pinned-device discovery, uses one global request sequence at the client response boundary.
Superseded key queries restore their queried and current-room users to nio's pending key set before hydration fails closed.
Proof-bound sending will use a two-stage boundary.
The preflight stage will reject stale local encrypted state or stale authoritative plaintext state before `prepare_large_message` can upload content.
The preflight stage rechecks cache identity and encryption after the authoritative state-event await, and file, audio, large-message, and approval uploads receive the resulting mode explicitly.
File delivery reads local bytes before preflight and constructs the encrypted or plaintext upload body synchronously from the accepted mode before its network await.
The preflight stage also treats `encrypted_rooms` as monotonic proof and rejects endpoint disagreement between joined members and full state.
Local encryption enablement holds the room delivery lock across its state read and write, then publishes through the same monotonic fence and outbound-session retirement as sync-discovered encryption.
The final stage will repeat the authoritative plaintext check or perform full encrypted hydration, then call the existing prepared-message sender immediately.
Restart recovery will continue to request initial hydration before scanning history, while the final send boundary independently establishes the state required for the actual event.
Raw reactions, approval events, tool events, stop buttons, and call notices will use a typed raw-result helper at the same lock boundary.
`_MindRoomAsyncClient` will remove encrypted-room invitees after membership processing and reject room-send preparation when membership, device keys, or the joined-only roster is not ready.

## Data Flow

For encrypted rooms, initial recovery hydration obtains full room state when needed, obtains joined members, establishes device keys, and returns an encrypted proof.
Recovery may then scan history, repair messages, or evaluate resume freshness.
Before preparing a proof-bound event, delivery validates the proof without performing an upload.
After preparation, encrypted delivery obtains a new joined-members response, compares it with the cached pre-refresh joined set, retires the outbound session before further awaits when required, completes key queries behind nio's membership-synchronization send fence, and validates the refreshed room.
Delivery then passes the already-prepared payload and stable transaction ID to nio while retaining the room delivery lock.
Nio's final preparation guard rejects any readiness change that became visible after application hydration instead of running nio's unchecked key-query fallback.
The request-scoped transport guard validates again after asynchronous header preparation and before every HTTP retry.
For plaintext rooms, delivery authoritatively checks the absence of room encryption before preparation and repeats that check after preparation before using the raw cache-bypass send path.
Recovery backoff releases the room lock while sleeping and reacquires it for a fresh authoritative validation.
Every application retry reuses the same transaction ID so an ambiguous prior wire attempt cannot create a duplicate event.

## Failure And Cancellation Behavior

An unavailable membership response, room-state response, key query, or proof validation returns no delivered event so restart recovery retries through its existing policy.
Membership disagreement caused by a concurrent cache replacement also returns no delivered event.
An encrypted replacement that disagrees with the hydration snapshot is marked membership-unsynchronized so nio cannot send through incomplete keys before the retry.
An invite introduced during device-key work also rejects hydration, retires the affected session, and leaves the room fenced.
A joined-members response superseded before cache mutation or hidden-room publication is discarded, and a key-query response superseded by a newer query or device-list generation leaves the room fenced without reaching encryption or transport.
A stable joined-members refresh preserves the existing Megolm session rather than forcing a redundant session share.
Cancellation continues to propagate through hydration, preparation, and sending.
Session retirement is safe to repeat because removing a missing outbound session is a no-op.
Retirement deliberately discards partially distributed sessions because a departed device may already know their key even while nio still marks them not fully shared.
Large-message uploads that fail for ordinary transport reasons retain their existing fallback behavior.

## Tests

A regression test will use a real nio room and joined-members response handling to show that a changed joined set retires fully and partially distributed outbound sessions before delivery.
A blocked-then-failed key-query regression will show that session retirement and the membership send fence happen before the query await and survive its failure.
A real-nio concurrency regression will show that a same-room application send cannot reach encryption or the wire while hydration is blocked and failing.
A successful concurrency regression will show that a queued real-nio send reaches the wire only after key readiness succeeds.
A regression test will cover both joined and departed members so the test protects readability and post-departure confidentiality.
A regression test will show that invitees are absent from the exact Megolm recipient roster, including invite-to-join and concurrent-invite cases.
A static regression will reject every production `.room_send` call outside the private delivery transport boundary.
A regression test will show that a stale plaintext proof performs zero media uploads and zero event sends.
A regression test will show that encrypted membership is refreshed after message preparation.
A pair of deterministic concurrency regressions will block joined-members and key-query responses, apply newer sync invalidations, and prove that neither stale response can reach session sharing or transport.
A second joined-members ordering regression will prove under both completion orders that an older endpoint request cannot overwrite a newer endpoint request.
A Classic Sync and Sliding Sync regression pair will prove that membership events, encryption events, and joined-count mismatches fence transport before callbacks.
A regression test will prove that an unchanged authoritative roster preserves the existing shared session.
A regression test will prove that a stale plaintext state-event response cannot cause an unencrypted sidecar upload after the cache becomes encrypted.
A hidden-room regression will prove that full-state membership cannot be overwritten by an older joined-members snapshot.
A global ordering regression will prove that a newer background key query and a device-list invalidation both supersede an older response before nio mutates its device store.
A file regression will prove that an encryption transition during local file I/O occurs before upload-mode selection.
A local-encryption regression will prove that immediate confirmation delivery cannot reuse a pre-encryption roster or Megolm session.
A concurrency regression will prove that local encryption enablement cannot commit while a same-client plaintext delivery holds the room lock.
A retry regression will prove that a generated transaction ID remains stable across application retries.
Existing hydration concurrency, transaction-ID, stale-stream cleanup, and restart-recovery tests will remain green.
The room-lifecycle tests will assert that leaving unconfigured rooms uses the shared `desired_room_ids` policy.

## Documentation And Pull Request State

The runtime architecture documentation will describe preflight validation, final membership refresh, and outbound-session rotation without claiming stronger server-side atomicity than Matrix provides.
The duplicated configured-plus-invited room calculation will be replaced with `desired_room_ids` before adding the router space exception.
After verification, the pull-request description will be updated to the pushed head SHA and its exact verification evidence.
Two fresh native reviewers must approve the same pushed SHA before the review loop is complete.
