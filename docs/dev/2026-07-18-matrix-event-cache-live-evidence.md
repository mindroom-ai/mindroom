# Matrix Event Cache Live Evidence

This evidence was collected on 2026-07-18 against the production `mindroom-chat` service and `https://mindroom.chat`.

The deployed service source was commit `7098e66daec37df4ea511060541db69aeebece97`.

The audit harness source was commit `686f983c0117b1764594fd402ae9834dae30f921` from PR #1586.

The hosted result is production-baseline evidence rather than a claim that PR #1586 was deployed.

PR-specific interaction classification is proven by the owning-seam SQLite and PostgreSQL test matrix.

No deployment, restart, direct database edit, or existing room was used.

The live service cache was opened only through SQLite `mode=ro` with `PRAGMA query_only`, and every cache evidence query ran in one read transaction.

The strict read sequence used a new disposable isolated SQLite database under `/tmp`.

All Matrix tokens remained inside the remote shell, and the durable artifact contains no credentials or message bodies.

The complete sanitized summary is stored in `docs/dev/evidence/2026-07-18-matrix-event-cache-live-summary.json`.

## Disposable room

The new private room was `!g178yDFmvMAT2iDiBn:mindroom.chat`.

The room was created by `@mindroom_code:mindroom.chat`, and authenticated membership verification returned only that user.

For the checked-in run, the authenticated `/joined_members` response was captured alongside the harness output and copied into the sanitized summary.

The current harness now performs the same authenticated query fail-closed and records the sorted joined-member IDs in its raw evidence.

The primary thread root was `$RLlVnt7Z04PV8IdyDQbwEZmk--UcNL_SCdU01fGiPi8`.

The service remained active with `NRestarts=0`.

## Interaction evidence

The harness emitted 58 controlled interaction records through 76 authenticated requests.

The executable validation compared all 58 records across 459 assertions and returned `passed`.

The stricter validator from commit `d3e48585c59083874eb99998b8bc2df38ad1d6f6` replayed the sanitized raw artifact across 491 assertions and also returned `passed`.

That replay additionally required zero accounting gaps, identical first and second visible snapshots, zero second-read homeserver work, the exact third-read rejection reason, and removal of the redacted child.

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

The first strict read had no cache state and completed a one-page authenticated homeserver refill in 541.383 ms.

The first homeserver fetch took 516.9 ms and scanned 25 events.

The second unchanged strict read completed in 1.895 ms from the isolated cache.

The second read recorded 1.6 ms of cache time, zero homeserver fetch time, zero scan pages, and zero scanned events.

The harness then redacted the dedicated child through Matrix and applied the same target removal and stale marker through the isolated cache API.

The third strict read rejected the isolated snapshot with `thread_invalidated_after_validation` and completed a one-page homeserver refill in 213.323 ms.

All three reads reported `degraded=false` and no error.

The hosted run intentionally did not manufacture a homeserver outage.

The owning-seam SQLite and PostgreSQL contract test `test_advisory_stale_fallback_is_labeled_and_dispatch_rejects_it` deterministically forces refill failure and proves that advisory reads return labeled degraded `stale_cache` history while dispatch reads reject that fallback and propagate the failure.

## Read-only service-cache validation

The final room snapshot contained 55 active events, six tombstones, three edit indexes, 11 event-to-thread indexes, and one thread state row.

The read-only integrity query returned `ok`.

The room had zero orphan edit rows, zero orphan thread rows, zero cache-only event IDs, and zero accounting gaps.

The clean accounting result supersedes both the earlier unsafe draft run and the superseded pre-transaction snapshot.

The sanitized raw evidence SHA-256 was `8e43c3ade26b3a2797b7a329614c08571994b40fc827214820022a5c30f5afd1`.

## Reproduction boundary

The hosted service database must remain read-only to the harness.

Strict thread reads must use `--strict-read-cache-db` with a new path that differs from `--cache-db`.

The harness rejects an existing strict-cache path, rejects the service-cache path, and fails before writing evidence when any declared interaction expectation disagrees with observed state.

The harness also rejects unpaired invite identity/token configuration and any missing or cache-only event accounting.

The exact command and safety procedure are documented in `docs/dev/matrix-event-cache-interaction-contract.md`.
