# Matrix Delivery Finalization Design

## Context

Restart recovery must repair and resume bot-owned responses in rooms that may be outside the current Sliding Sync window.
The existing hydration proof records encryption and joined membership before recovery work, but proof-bound delivery can perform additional awaited work before sending.
The current boundary does not retire an existing Megolm session when an authoritative joined-members refresh changes membership.
The current boundary also prepares large messages before rejecting a stale plaintext proof, so preparation can leave an unencrypted sidecar upload behind after the room becomes encrypted.

## Goals

Proof-bound delivery must reject stale encryption state before any content upload.
Encrypted delivery must refresh authoritative joined membership at the final send boundary.
Membership changes must retire every existing outbound Megolm session before any later hydration await or nio encryption.
Device-key readiness must be re-established for the final authoritative joined set.
Recovery retries and stable Matrix transaction IDs must retain their current behavior.
The fix must remain owned by the Matrix delivery boundary instead of adding recovery-specific crypto logic to the coordinator.

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
The cached joined-member set must exactly match the authoritative joined-member response, excluding invited users from the equality comparison.
An unknown prior joined-member set or any joined-member change must retire the outbound Megolm session before full-state or device-key awaits.
Device-key work must keep a cached room marked membership-unsynchronized until every joined member's pending key query is clear.
No unrelated application await may occur between final encrypted hydration and the call into nio's room-send operation.
Proof rejection must return the existing retry signal without sending an event.

## Architecture

`mindroom.matrix.client_delivery` remains the single owner of delivery hydration, proof validation, large-message preparation, and final send sequencing.
`RoomDeliveryHydrationProof` continues to describe the encryption mode and authoritative joined-member snapshot established by hydration.
Encrypted hydration will snapshot cached joined membership before calling `joined_members`, retire the outbound session immediately when the returned authoritative set is unknown or changed, and then validate exact authoritative membership.
The same hydration operation will refresh tracked users, query required device keys, and publish a complete hidden-room candidate only after validation succeeds.
Proof-bound sending will use a two-stage boundary.
The preflight stage will reject stale local encrypted state or stale authoritative plaintext state before `prepare_large_message` can upload content.
The final stage will repeat the authoritative plaintext check or perform full encrypted hydration, then call the existing prepared-message sender immediately.
Restart recovery will continue to request initial hydration before scanning history, while the final send boundary independently establishes the state required for the actual event.

## Data Flow

For encrypted rooms, initial recovery hydration obtains full room state when needed, obtains joined members, establishes device keys, and returns an encrypted proof.
Recovery may then scan history, repair messages, or evaluate resume freshness.
Before preparing a proof-bound event, delivery validates the proof without performing an upload.
After preparation, encrypted delivery obtains a new joined-members response, compares it with the cached pre-refresh joined set, retires the outbound session before further awaits when required, completes key queries behind nio's membership-synchronization send fence, and validates the refreshed room.
Delivery then passes the already-prepared payload and stable transaction ID to nio.
For plaintext rooms, delivery authoritatively checks the absence of room encryption before preparation and repeats that check after preparation before using the raw cache-bypass send path.

## Failure And Cancellation Behavior

An unavailable membership response, room-state response, key query, or proof validation returns no delivered event so restart recovery retries through its existing policy.
Membership disagreement caused by a concurrent cache replacement also returns no delivered event.
An encrypted replacement that disagrees with the hydration snapshot is marked membership-unsynchronized so nio cannot send through incomplete keys before the retry.
Cancellation continues to propagate through hydration, preparation, and sending.
Session retirement is safe to repeat because removing a missing outbound session is a no-op.
Retirement deliberately discards partially distributed sessions because a departed device may already know their key even while nio still marks them not fully shared.
Large-message uploads that fail for ordinary transport reasons retain their existing fallback behavior.

## Tests

A regression test will use a real nio room and joined-members response handling to show that a changed joined set retires fully and partially distributed outbound sessions before delivery.
A blocked-then-failed key-query regression will show that session retirement and the membership send fence happen before the query await and survive its failure.
A regression test will cover both joined and departed members so the test protects readability and post-departure confidentiality.
A regression test will show that a stale plaintext proof performs zero media uploads and zero event sends.
A regression test will show that encrypted membership is refreshed after message preparation.
Existing hydration concurrency, transaction-ID, stale-stream cleanup, and restart-recovery tests will remain green.
The room-lifecycle tests will assert that leaving unconfigured rooms uses the shared `desired_room_ids` policy.

## Documentation And Pull Request State

The runtime architecture documentation will describe preflight validation, final membership refresh, and outbound-session rotation without claiming stronger server-side atomicity than Matrix provides.
The duplicated configured-plus-invited room calculation will be replaced with `desired_room_ids` before adding the router space exception.
After verification, the pull-request description will be updated to the pushed head SHA and its exact verification evidence.
Two fresh native reviewers must approve the same pushed SHA before the review loop is complete.
