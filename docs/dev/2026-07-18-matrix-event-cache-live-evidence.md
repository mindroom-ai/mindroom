# Matrix Event Cache Live Evidence

This evidence was collected on 2026-07-18 against the production `mindroom-chat` service and `https://mindroom.chat`.

The service source was commit `7098e66daec37df4ea511060541db69aeebece97`.

No deployment, restart, direct database edit, or existing room was used.

All Matrix tokens remained inside the remote shell, and the durable artifact contains no credentials or message bodies.

The complete sanitized summary is stored in `docs/dev/evidence/2026-07-18-matrix-event-cache-live-summary.json`.

## Disposable room

The new private room was `!lIUGVbPQmXc3ZiQ9PI:mindroom.chat`.

The room was created by `@mindroom_code:mindroom.chat`, and authenticated membership verification returned only that user.

The primary thread root was `$rPFdvdy8jyHbR6xUldWQDvg2andeEvn3qE0zOel8PtA`.

The service remained active with `NRestarts=0`.

## Interaction evidence

The harness emitted 57 controlled interaction records through 75 authenticated requests.

The matrix covered member, name, topic, avatar, power, join, history, guest, alias, encryption, pin, generic state, and room creation state supplied by `createRoom`.

The matrix covered text, notice, emote, location, explicit thread, relation-less reply, root edit, child edit, reply edit, message reference, reaction, and reaction redaction.

The matrix covered valid file, PNG image, WAV voice audio, WebM video, sticker, poll start, poll response, poll end, beacon info, and beacon.

The matrix covered call invite, candidates, answer, select, reject, negotiate, hangup, RTC membership with focus data, and RTC notification.

The matrix covered message redaction, original redaction with dependent edit, edit-only redaction, custom timeline data, and four opaque encrypted thread, edit, reply, and reference relations.

Typing, receipt, presence, global account data, room account data, and to-device events were emitted but did not enter timeline accounting.

Complete joined state, invite and leave timelines, and device-list changes are covered by deterministic owning-seam tests rather than destructive hosted membership changes.

## Real media

The 68-byte PNG passed checksum and decompression validation.

The 364-byte WAV decoded as non-empty mono audio.

The 522-byte WebM was validated by `ffprobe`.

Each uploaded MXC was downloaded through the authenticated client media endpoint and matched its original SHA-256 digest.

## Strict thread reads

The first strict production read rejected a never-validated snapshot and completed a one-page homeserver refill in 541.872 ms.

The first homeserver fetch took 510.2 ms and scanned 24 events.

The second unchanged strict read completed in 2.795 ms from cache.

The second read recorded 2.4 ms of cache time, zero homeserver fetch time, zero scan pages, and zero scanned events.

A Matrix redaction then invalidated the thread with `live_redaction`.

The third strict read rejected the snapshot with `thread_invalidated_after_validation` and completed a one-page homeserver refill in 215.827 ms.

All three reads reported `degraded=false` and no error.

## Read-only cache validation

The final room snapshot contained 50 active events, seven tombstones, one edit index, six event-to-thread indexes, and one thread state row.

The read-only integrity query returned `ok`.

The room had zero orphan edit rows, zero orphan thread rows, and zero cache-only event IDs.

## Coordinated storage finding

The final accounting comparison found three homeserver events absent from point storage after the child-redaction refill: the relation-less reply, its edit, and its message reference.

The refill could no longer resolve these events through the redacted child and the current duplicated thread replacement path removed their point rows.

This is a deterministic storage-ownership finding rather than an interaction-classification fix.

The minimal reproduction and boundary are documented in `docs/dev/matrix-event-cache-interaction-contract.md` for coordination with the storage normalization track.
