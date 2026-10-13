# Matrix Event Cache Live Evidence

This records one audit run of the Matrix event cache on 2026-07-18 against the production `mindroom-chat` service and `https://mindroom.chat`, using the harness from PR #1586.
It is a record, not a procedure: the event cache, the contract document that specified the run, and the harness were removed when the event journal and the visible-message projection replaced the cache.

## Provenance and safety

- The deployed service source was commit `7098e66daec37df4ea511060541db69aeebece97`, and the audit harness was commit `686f983c0117b1764594fd402ae9834dae30f921` from PR #1586, which was squash-merged as `34fae3688`, so the harness and validator commits are not in this repository.
- The result is production-baseline evidence, not a claim that PR #1586 was deployed; PR #1586's interaction classification was proven by its owning-seam SQLite and PostgreSQL test matrix.
- No deployment, restart, direct database edit, or existing room was used, and the service stayed active with `NRestarts=0`.
- The live service cache was opened only through SQLite `mode=ro` with `PRAGMA query_only`, and every cache evidence query ran in one read transaction.
- Strict thread reads used a new disposable isolated SQLite database under `/tmp`; the harness refused an existing strict-cache path or the service-cache path.
- The harness failed before writing evidence on any declared expectation that disagreed with observed state, on unpaired invite identity and token configuration, and on missing or cache-only event accounting.
- All Matrix tokens stayed inside the remote shell, and the durable artifact contains no credentials or message bodies.
- The complete sanitized summary, including the interaction families, cache row counts, and raw-evidence SHA-256, is [`docs/dev/evidence/2026-07-18-matrix-event-cache-live-summary.json`](evidence/2026-07-18-matrix-event-cache-live-summary.json).

## Disposable room

The new private room was `!g178yDFmvMAT2iDiBn:mindroom.chat`, created by `@mindroom_code:mindroom.chat`, with primary thread root `$RLlVnt7Z04PV8IdyDQbwEZmk--UcNL_SCdU01fGiPi8`.
Authenticated `/joined_members` returned only that user.
The pinned harness did not query membership itself: the response was captured out-of-band alongside the harness output and copied into the summary, and the 76-request count below does not include it.

## Interactions

The harness emitted 58 controlled interaction records through 76 authenticated requests, and its validation passed 459 assertions across all 58.
The stricter validator from commit `d3e48585c59083874eb99998b8bc2df38ad1d6f6` replayed the sanitized raw artifact across 491 assertions and also passed.
That replay additionally required zero accounting gaps, identical first and second visible snapshots, zero second-read homeserver work, the exact third-read rejection reason, and removal of the redacted child.

The hosted run did not cover:

- Typing, receipt, presence, global and room account data, and to-device events, which were emitted but did not enter timeline accounting.
- Complete joined state, invite and leave timelines, and device-list changes, which were left to deterministic owning-seam tests rather than destructive hosted membership changes.
- A homeserver outage, which the run intentionally did not manufacture; refill failure and the stale-cache fallback were proven only by PR #1586's SQLite and PostgreSQL contract tests.

## Real media

The 68-byte PNG passed checksum and decompression validation, the 364-byte WAV decoded as non-empty mono audio, and the 522-byte WebM was validated by `ffprobe`.
Each uploaded MXC was downloaded through the authenticated client media endpoint and matched its original SHA-256 digest.

## Strict thread reads

A dedicated strict-read child and its redaction kept rejection evidence from disturbing the relation-less reply, edit, and reference cases.
Before the third read, the harness redacted that child through Matrix and applied the same target removal and stale marker through the isolated cache API.

| Read | Outcome | Elapsed | Breakdown |
| --- | --- | ---: | --- |
| 1, no cache state | One-page authenticated homeserver refill | 541.383 ms | 516.9 ms homeserver fetch, 25 events scanned |
| 2, unchanged | Served from the isolated cache | 1.895 ms | 1.6 ms cache time, no homeserver fetch, pages, or scanned events |
| 3, after redaction | Rejected with `thread_invalidated_after_validation`, then one-page homeserver refill | 213.323 ms | |

All three reads reported `degraded=false` and no error.

## Read-only service-cache validation

The read-only integrity query returned `ok`, and the room had zero orphan edit rows, orphan thread rows, cache-only event IDs, and accounting gaps.
This clean accounting supersedes an earlier unsafe draft run and a pre-transaction snapshot.
