# Matrix Event Cache Live Evidence

This evidence was collected on 2026-07-18 against the production `mindroom-chat` service and `https://mindroom.chat`.

The deployed service source was commit `7098e66daec37df4ea511060541db69aeebece97`.

The audit harness source was commit `18825bdc5141801e5e77d9d6c9df73772c142c43` from PR #1586.

The hosted result is production-baseline evidence rather than a claim that PR #1586 was deployed.

PR-specific interaction classification is proven by the owning-seam SQLite and PostgreSQL test matrix.

No deployment, restart, direct database edit, or existing room was used.

The live service cache was opened only through SQLite `mode=ro` with `PRAGMA query_only`.

The strict read sequence used a new disposable isolated SQLite database under `/tmp`.

All Matrix tokens remained inside the remote shell, and the durable artifact contains no credentials or message bodies.

The complete sanitized summary is stored in `docs/dev/evidence/2026-07-18-matrix-event-cache-live-summary.json`.

## Disposable room

The new private room was `!T1CFk4Gghzh22jaOde:mindroom.chat`.

The room was created by `@mindroom_code:mindroom.chat`, and authenticated membership verification returned only that user.

The primary thread root was `$uXDcyQ1XX1hEhFQ0yAzA2GBORapGWKPZXid4s_aM_ME`.

The service remained active with `NRestarts=0`.

## Interaction evidence

The harness emitted 58 controlled interaction records through 76 authenticated requests.

The executable validation compared all 58 records across 459 assertions and returned `passed`.

The matrix covered member, name, topic, avatar, power, join, history, guest, alias, encryption, pin, generic state, and room creation state supplied by `createRoom`.

The matrix covered text, notice, emote, location, explicit thread, relation-less reply, root edit, child edit, reply edit, message reference, reaction, and reaction redaction.

The matrix covered valid file, PNG image, WAV voice audio, WebM video, sticker, poll start, poll response, poll end, beacon info, and beacon.

The matrix covered call invite, candidates, answer, select, reject, negotiate, hangup, RTC membership with focus data, and RTC notification.

The matrix covered message redaction, original redaction with dependent edit, edit-only redaction, custom timeline data, and four opaque encrypted thread, edit, reply, and reference relations.

The matrix included a dedicated strict-read child and its redaction so rejection evidence could not disturb the relation-less reply, edit, or reference cases.

Typing, receipt, presence, global account data, room account data, and to-device events were emitted but did not enter timeline accounting.

Complete joined state, invite and leave timelines, and device-list changes are covered by deterministic owning-seam tests rather than destructive hosted membership changes.

## Real media

The 68-byte PNG passed checksum and decompression validation.

The 364-byte WAV decoded as non-empty mono audio.

The 522-byte WebM was validated by `ffprobe`.

Each uploaded MXC was downloaded through the authenticated client media endpoint and matched its original SHA-256 digest.

## Strict thread reads

The first strict read had no cache state and completed a one-page authenticated homeserver refill in 510.571 ms.

The first homeserver fetch took 503.1 ms and scanned 25 events.

The second unchanged strict read completed in 1.792 ms from the isolated cache.

The second read recorded 1.6 ms of cache time, zero homeserver fetch time, zero scan pages, and zero scanned events.

The harness then redacted the dedicated child through Matrix and applied the same target removal and stale marker through the isolated cache API.

The third strict read rejected the isolated snapshot with `thread_invalidated_after_validation` and completed a one-page homeserver refill in 250.353 ms.

All three reads reported `degraded=false` and no error.

## Read-only service-cache validation

The final room snapshot contained 55 active events, six tombstones, three edit indexes, 11 event-to-thread indexes, and one thread state row.

The read-only integrity query returned `ok`.

The room had zero orphan edit rows, zero orphan thread rows, zero cache-only event IDs, and zero accounting gaps.

The clean accounting result supersedes the earlier unsafe draft run, which is not used as evidence because its helper opened the service database read-write.

## Reproduction boundary

The hosted service database must remain read-only to the harness.

Strict thread reads must use `--strict-read-cache-db` with a new path that differs from `--cache-db`.

The harness rejects an existing strict-cache path, rejects the service-cache path, and fails before writing evidence when any declared interaction expectation disagrees with observed state.

The exact command and safety procedure are documented in `docs/dev/matrix-event-cache-interaction-contract.md`.
